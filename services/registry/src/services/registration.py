"""Registration service for agent registration workflow."""

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import yaml

from common.database.src.generated_client.fields import Json as PrismaJson

from ..adapters import A2AAdapter, CapabilityData, HarvestResult, MCPAdapter
from ..config import settings
from ..health_probe import should_skip_health_probe
from ..models.emerge_config import EmergeConfig
from .health_check import HealthCheckService
from .validation import ConflictError, ValidationError, ValidationService
from .version_manager import VersionManager

logger = logging.getLogger(__name__)


_AUTH_TYPE_MAP: dict[str, str] = {
    "x_api_key": "X_API_KEY",
    "http_bearer": "HTTP_BEARER",
    "bearer_token": "HTTP_BEARER",
    "oauth2": "OAUTH2",
    "oauth2_dcr": "OAUTH2_DCR",
    "platform_env": "PLATFORM_ENV",
}


class RegistrationService:
    """Service for handling agent registration."""

    def __init__(
        self, db, kafka_producer=None
    ):  # kafka_producer unused — endpoint handles publish
        self.db = db
        self.validation_service = ValidationService()
        self.health_check_service = HealthCheckService()
        self.version_manager = VersionManager()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def register_agent(
        self,
        emerge_yaml_content: str,
        user_id: str,
    ) -> dict[str, Any]:
        """
        Register a new agent.

        Process:
        1. Parse and validate emerge.yaml
        2. Check for duplicate registration
        3. Harvest capabilities via adapter
        4. Persist to database
        5. Create version snapshot
        6. Return registration response

        Raises:
            ValidationError: Config is invalid
            ConflictError: Agent name+version already registered
            ConnectionError: Capability harvesting failed
        """
        emerge_config = self._parse_emerge_yaml(emerge_yaml_content)
        print(
            f"Parsed emerge.yaml for agent {emerge_config.identity.name} version {emerge_config.identity.version}"
        )
        print("emerge config for the agent is", emerge_config.model_dump_json(indent=2))

        is_valid, error = self.validation_service.validate_emerge_config(emerge_config)
        if not is_valid:
            raise error
        print(
            f"Validation passed for agent {emerge_config.identity.name} version {emerge_config.identity.version}"
        )

        # If a soft-deleted record with the same identity exists, remove it so
        # re-registration creates a fresh record (cascade handles child rows).
        await self._purge_soft_deleted_agent(emerge_config, user_id)

        await self._assert_agent_not_exists(emerge_config, user_id)

        # STDIO has no HTTP port. Remote MCP SSE/HTTP usually has no /health;
        # harvest is the connectivity check. A2A HTTP still probes.
        if not should_skip_health_probe(emerge_config):
            await self._verify_health_endpoint(emerge_config.health_endpoint)

        harvest_result = await self._harvest_capabilities(
            emerge_config, emerge_yaml_content
        )
        print(
            f"Harvested {len(harvest_result.capabilities)} capabilities for agent {emerge_config.identity.name} version {emerge_config.identity.version}"
        )

        is_stdio = emerge_config.protocol.transport.type.lower() == "stdio"
        agent = await self._save_agent_to_db(
            emerge_config, harvest_result, user_id, is_stdio=is_stdio
        )
        print(
            f"Saved agent {agent.name} version {agent.version} to database with ID {agent.id}"
        )

        await self._create_version_snapshot(agent, harvest_result)

        return self._build_registration_response(agent, harvest_result)

    async def update_agent(
        self,
        agent_id: str,
        emerge_yaml_content: str,
        user_id: str,
    ) -> dict[str, Any]:
        """
        Update an existing agent.

        Raises:
            ValidationError: Config is invalid or agent not found
            PermissionError: User does not own the agent
        """
        emerge_config = self._parse_emerge_yaml(emerge_yaml_content)

        is_valid, error = self.validation_service.validate_emerge_config(emerge_config)
        if not is_valid:
            raise error

        existing_agent = await self.db.agent.find_unique(
            where={"id": agent_id},
            include={
                "capabilities": True,
                "security": {"include": {"auth_strategies": True}},
                "payment": True,
            },
        )

        if not existing_agent or not existing_agent.is_active:
            raise ValidationError("agent_id", "Agent not found")

        if existing_agent.user_id != user_id:
            raise PermissionError("You don't have permission to update this agent")

        version_changed = existing_agent.version != emerge_config.identity.version
        harvest_result = await self._harvest_capabilities(
            emerge_config, emerge_yaml_content
        )

        if version_changed:
            await self._save_agent_to_db(emerge_config, harvest_result, user_id)
        else:
            await self._update_existing_agent(
                existing_agent, emerge_config, harvest_result
            )

        return {
            "agent_id": agent_id,
            "version": emerge_config.identity.version,
            "updated_at": datetime.now(UTC),
            "version_created": version_changed,
        }

    # -------------------------------------------------------------------------
    # Parse / validate helpers
    # -------------------------------------------------------------------------

    def _parse_emerge_yaml(self, content: str) -> EmergeConfig:
        """Parse emerge.yaml content into EmergeConfig model."""
        try:
            data = yaml.safe_load(content)
            raw_transport = data.get("protocol", {}).get("transport", {})
            logger.debug("YAML raw transport section: %s", raw_transport)
            config = EmergeConfig(**data)
            logger.debug(
                "Parsed TransportConfig: type=%s command=%r env=%s",
                config.protocol.transport.type,
                config.protocol.transport.command,
                config.protocol.transport.env,
            )
            return config
        except yaml.YAMLError as e:
            raise ValidationError("emerge.yaml", f"Invalid YAML: {str(e)}") from e
        except Exception as e:
            raise ValidationError("emerge.yaml", f"Parse error: {str(e)}") from e

    async def _verify_health_endpoint(self, health_endpoint: str) -> None:
        """Probe the agent's health endpoint and reject registration if it doesn't respond."""
        is_healthy, error_msg = await self.health_check_service.check_agent_health(
            health_endpoint
        )
        if not is_healthy:
            raise ConnectionError(
                f"Agent health endpoint '{health_endpoint}' is not reachable: {error_msg}"
            )

    async def _purge_soft_deleted_agent(
        self, config: EmergeConfig, user_id: str
    ) -> None:
        """Hard-delete a soft-deleted agent so re-registration creates a fresh record.

        All child tables (Transport, Security, Capability, Payment, etc.) define
        onDelete: Cascade, so a single agent delete cleans up everything.
        """
        existing = await self.db.agent.find_first(
            where={
                "user_id": user_id,
                "name": config.identity.name,
                "version": config.identity.version,
                "is_active": False,
            }
        )
        if existing:
            await self.db.agent.delete(where={"id": existing.id})
            logger.info(
                "Purged soft-deleted agent %s (%s v%s) — re-registration allowed",
                existing.id,
                config.identity.name,
                config.identity.version,
            )

    async def _assert_agent_not_exists(
        self, config: EmergeConfig, user_id: str
    ) -> None:
        """Raise ConflictError if an active agent with the same name+version already exists."""
        existing = await self.db.agent.find_first(
            where={
                "user_id": user_id,
                "name": config.identity.name,
                "version": config.identity.version,
                "is_active": True,
            }
        )
        if existing:
            raise ConflictError(
                "agent",
                f"Agent {config.identity.name} version {config.identity.version} already exists",
            )

    # -------------------------------------------------------------------------
    # Capability harvesting
    # -------------------------------------------------------------------------

    async def _harvest_capabilities(
        self, config: EmergeConfig, raw_yaml_content: str = ""
    ) -> HarvestResult:
        """Select adapter by protocol and harvest capabilities.

        STDIO and SSE MCP agents cannot be harvested via a simple HTTP POST
        (stdio has no HTTP interface; SSE requires a stateful stream + handshake).
        STDIO agents fall back to the ``capabilities`` block in emerge.yaml.
        SSE agents use the MCP SDK's sse_client for a proper stateful handshake.
        HTTP/streamable-http agents use raw JSON-RPC POST via MCPAdapter.
        """
        transport_type = config.protocol.transport.type.lower()
        protocol_type = config.protocol.type.lower()

        # STDIO agents have no HTTP interface — read capabilities from emerge.yaml.
        # SSE agents use the MCP SDK's sse_client (handled by MCPAdapter).
        if transport_type == "stdio":
            raw_data = yaml.safe_load(raw_yaml_content) if raw_yaml_content else {}
            return self._harvest_from_yaml(raw_data)

        endpoint = config.protocol.transport.endpoint or ""

        if protocol_type == "mcp":
            adapter = MCPAdapter(
                endpoint=endpoint,
                transport_type=transport_type,
                timeout=settings.adapter_timeout,
                max_retries=settings.adapter_max_retries,
            )
            return await adapter.harvest()

        if protocol_type == "a2a":
            adapter = A2AAdapter(
                endpoint=endpoint,
                timeout=settings.adapter_timeout,
                max_retries=settings.adapter_max_retries,
            )
            result = await adapter.harvest()
            if not result.capabilities and raw_yaml_content:
                raw_data = yaml.safe_load(raw_yaml_content)
                if isinstance(raw_data, dict):
                    yaml_caps = self._harvest_a2a_skills_from_yaml(raw_data)
                    if yaml_caps:
                        logger.warning(
                            "A2A: Agent Card yielded 0 capabilities; "
                            "using %d skill(s) from emerge.yaml (adapter_errors=%s)",
                            len(yaml_caps),
                            result.errors,
                        )
                        meta = dict(result.metadata or {})
                        meta["skills_source"] = "emerge.yaml"
                        return HarvestResult(
                            capabilities=yaml_caps,
                            errors=list(result.errors),
                            metadata=meta,
                        )
            return result

        raise ValidationError("protocol.type", f"Unsupported protocol: {protocol_type}")

    @staticmethod
    def _harvest_from_yaml(raw_data: dict[str, Any]) -> HarvestResult:
        """Build a HarvestResult from the ``capabilities`` block in emerge.yaml.

        Used for STDIO MCP agents that have no discoverable HTTP endpoint.
        The emerge.yaml schema uses ``inputSchema`` / ``outputSchema`` (camelCase)
        to match the MCP spec — we store them as-is.
        """
        caps_block = raw_data.get("capabilities") or {}
        tools = caps_block.get("tools") or []
        resources = caps_block.get("resources") or []
        prompts = caps_block.get("prompts") or []

        capabilities: list[CapabilityData] = []

        for tool in tools:
            if not tool:
                continue
            capabilities.append(
                CapabilityData(
                    type="tool",
                    id=tool.get("name", ""),
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema"),
                    output_schema=tool.get("outputSchema"),
                )
            )

        for resource in resources:
            if not resource:
                continue
            capabilities.append(
                CapabilityData(
                    type="resource",
                    id=resource.get("name", ""),
                    name=resource.get("name", ""),
                    description=resource.get("description", ""),
                    uri_template=resource.get("uri"),
                    mime_type=resource.get("mimeType"),
                )
            )

        for prompt in prompts:
            if not prompt:
                continue
            raw_args = prompt.get("arguments") or []
            arguments = (
                [
                    {
                        "name": a.get("name"),
                        "description": a.get("description", ""),
                        "required": a.get("required", False),
                    }
                    for a in raw_args
                ]
                if raw_args
                else None
            )
            capabilities.append(
                CapabilityData(
                    type="prompt",
                    id=prompt.get("name", ""),
                    name=prompt.get("name", ""),
                    description=prompt.get("description", ""),
                    arguments=arguments,
                )
            )

        return HarvestResult(
            capabilities=capabilities,
            metadata={"source": "emerge.yaml", "transport": "stdio"},
        )

    @staticmethod
    def _harvest_a2a_skills_from_yaml(raw_data: dict[str, Any]) -> list[CapabilityData]:
        """Map top-level ``skills:`` in emerge.yaml to capability rows (A2A)."""
        skills = raw_data.get("skills") or []
        if not isinstance(skills, list):
            return []

        capabilities: list[CapabilityData] = []
        for item in skills:
            if not isinstance(item, dict):
                continue
            sid = item.get("id")
            name = item.get("name")
            if not sid or not name:
                continue
            capabilities.append(
                CapabilityData(
                    type="tool",
                    id=str(sid),
                    name=str(name),
                    description=str(item.get("description", "")),
                    input_schema=item.get("input_schema") or item.get("inputSchema"),
                    output_schema=item.get("output_schema") or item.get("outputSchema"),
                )
            )
        return capabilities

    # -------------------------------------------------------------------------
    # Database persistence — orchestrator
    # -------------------------------------------------------------------------

    async def _save_agent_to_db(
        self,
        config: EmergeConfig,
        harvest_result: HarvestResult,
        user_id: str,
        is_stdio: bool = False,
    ) -> Any:
        """Persist agent and all related rows. Returns the created agent record."""
        print("DB step 1: resolving owner contact")
        owner_contact = await self._resolve_owner_contact(user_id)
        print(f"DB step 2: creating agent record (owner_contact={owner_contact})")
        agent = await self._create_agent_record(
            config, harvest_result, user_id, owner_contact, health_status="HEALTHY"
        )
        print(f"DB step 3: creating transport for agent {agent.id}")
        await self._create_transport(agent.id, config)
        print("DB step 4: creating security")
        security = await self._create_security(agent.id, config)
        print("DB step 5: creating auth strategies")
        await self._create_auth_strategies(security.id, config)
        print("DB step 6: creating payment")
        await self._create_payment(agent.id, config)
        print(f"DB step 7: creating {len(harvest_result.capabilities)} capabilities")
        await self._create_capabilities(agent.id, harvest_result.capabilities)
        print("DB step 8: all records saved")
        return agent

    # -------------------------------------------------------------------------
    # Database persistence — individual record creators
    # -------------------------------------------------------------------------

    async def _resolve_owner_contact(self, user_id: str) -> str:
        """Return the user's email, auto-creating a dev user when auth is disabled."""
        user = await self.db.user.find_unique(where={"id": user_id})
        if (
            not user
            and user_id == "dev_user"
            and os.getenv("DISABLE_AUTH", "false").lower() == "true"
        ):
            user = await self.db.user.create(
                data={
                    "id": user_id,
                    "email": f"{user_id}@dev.local",
                    "is_active": True,
                }
            )
        return user.email if user else user_id

    async def _create_agent_record(
        self,
        config: EmergeConfig,
        harvest_result: HarvestResult,
        user_id: str,
        owner_contact: str,
        health_status: str = "UNKNOWN",
    ) -> Any:
        # Harvest metadata often sets ``provider`` explicitly to JSON null when the
        # Agent Card omits it — ``dict.get("provider", "Unknown")`` then returns
        # None and Prisma rejects the create (MissingRequiredValueError).
        raw_provider = harvest_result.metadata.get("provider")
        provider = (
            raw_provider.strip()
            if isinstance(raw_provider, str) and raw_provider.strip()
            else "Unknown"
        )

        if not user_id or not isinstance(user_id, str):
            raise ValidationError(
                "user_id", "A valid user id is required to register an agent"
            )

        return await self.db.agent.create(
            data={
                "id": config.identity.id,
                "user_id": user_id,
                "name": config.identity.name,
                "version": config.identity.version,
                "description": config.identity.description,
                "provider": provider,
                "owner_contact": owner_contact,
                "tags": config.identity.tags,
                "protocol_type": config.protocol.type.upper(),
                "protocol_version": config.protocol.version,
                "health_endpoint": config.health_endpoint,
                "health_status": health_status,
                "last_health_check": datetime.now(UTC),
                **(
                    {
                        "authorized_scope": PrismaJson(
                            config.authorized_scope.model_dump()
                        )
                    }
                    if config.authorized_scope
                    else {}
                ),
            }
        )

    async def _create_transport(self, agent_id: str, config: EmergeConfig) -> None:
        env_value = config.protocol.transport.env or None
        logger.debug(
            "_create_transport: agent=%s type=%s env=%s",
            agent_id,
            config.protocol.transport.type,
            env_value,
        )
        transport_data: dict[str, Any] = {
            "agent_id": agent_id,
            "type": config.protocol.transport.type.upper(),
            "args": config.protocol.transport.args or [],
        }
        # Omit optional String? fields when None — Prisma treats explicit None
        # differently from an absent key on some client versions.
        if config.protocol.transport.endpoint is not None:
            transport_data["endpoint"] = config.protocol.transport.endpoint
        if config.protocol.transport.command is not None:
            transport_data["command"] = config.protocol.transport.command
        if env_value is not None:
            transport_data["env"] = PrismaJson(env_value)
        await self.db.transport.create(data=transport_data)
        await self._create_agent_secrets(agent_id, env_value)

    async def _create_agent_secrets(
        self, agent_id: str, env: dict[str, str] | None
    ) -> None:
        """Record each env-var key from emerge.yaml as an AgentSecret row."""
        if not env:
            return
        for var_name in env:
            await self.db.agentsecret.upsert(
                where={
                    "agent_id_var_name": {"agent_id": agent_id, "var_name": var_name}
                },
                data={
                    "create": {"agent_id": agent_id, "var_name": var_name},
                    "update": {},
                },
            )

    async def _create_security(self, agent_id: str, config: EmergeConfig) -> Any:
        mtls = config.security.transport_layer.mtls_config
        try:
            return await self.db.security.create(
                data={
                    "agent_id": agent_id,
                    "transport_layer_type": config.security.transport_layer.type.upper(),
                    "mtls_cert_vault_key": mtls.cert_vault_key if mtls else None,
                    "mtls_key_vault_key": mtls.key_vault_key if mtls else None,
                    "mtls_ca_vault_key": mtls.ca_vault_key if mtls else None,
                }
            )
        except Exception as e:
            raise ValidationError(
                "security", f"Failed to create security config: {str(e)}"
            ) from e

    async def _create_auth_strategies(
        self, security_id: str, config: EmergeConfig
    ) -> None:
        for strategy in config.security.auth_strategies:
            auth_type = self._normalize_auth_type(strategy.type)
            config_data = self._to_json_safe_dict(strategy.config)
            try:
                await self.db.authstrategy.create(
                    data={
                        "security_id": security_id,
                        "strategy_id": strategy.id,
                        "type": auth_type,
                        "config": PrismaJson(config_data),
                        "capability_ids": strategy.capability_ids,
                    }
                )
            except Exception as e:
                raise ValidationError(
                    "security.auth_strategies",
                    f"Failed to create auth strategy '{strategy.id}': {str(e)}",
                ) from e

    async def _create_payment(self, agent_id: str, config: EmergeConfig) -> None:
        if not config.payment:
            return
        await self.db.payment.create(
            data={
                "agent_id": agent_id,
                "enabled": config.payment.enabled,
                "base_fee": config.payment.base_fee,
            }
        )

    async def _create_capabilities(
        self, agent_id: str, capabilities: list[Any]
    ) -> None:
        for cap in capabilities:
            # Passing None for a Json? field is ambiguous in Prisma's wire
            # protocol (SQL NULL vs JSON null), so we omit optional JSON fields
            # entirely and let Prisma default them to NULL.
            data: dict[str, Any] = {
                "agent_id": agent_id,
                "type": cap.type.upper(),
                "capability_id": cap.id,
                "name": cap.name,
                "description": cap.description,
                "uri_template": cap.uri_template,
                "mime_type": cap.mime_type,
            }
            if isinstance(cap.input_schema, dict):
                data["input_schema"] = PrismaJson(cap.input_schema)
            if isinstance(cap.output_schema, dict):
                data["output_schema"] = PrismaJson(cap.output_schema)
            if isinstance(cap.arguments, list):
                data["arguments"] = PrismaJson(cap.arguments)

            await self.db.capability.create(data=data)

    # -------------------------------------------------------------------------
    # Version management
    # -------------------------------------------------------------------------

    async def _create_version_snapshot(
        self, agent: Any, harvest_result: HarvestResult
    ) -> None:
        snapshot = self.version_manager.create_manifest_snapshot(
            agent_data={
                "id": agent.id,
                "name": agent.name,
                "version": agent.version,
                "description": agent.description,
                "tags": agent.tags,
                "protocol_type": agent.protocol_type,
                "protocol_version": agent.protocol_version,
            },
            capabilities=[cap.dict() for cap in harvest_result.capabilities],
            security={},
            payment={},
        )

        previous = await self.db.agentversion.find_first(
            where={"agent_id": agent.id},
            order={"created_at": "desc"},
        )
        change_summary = self.version_manager.generate_change_summary(
            previous.manifest_snapshot if previous else None,
            snapshot,
        )

        await self.db.agentversion.create(
            data={
                "agent_id": agent.id,
                "version": agent.version,
                "manifest_snapshot": PrismaJson(snapshot),
                "change_summary": change_summary,
            }
        )

    async def _update_existing_agent(
        self,
        agent: Any,
        config: EmergeConfig,
        harvest_result: HarvestResult,
    ) -> None:
        await self.db.agent.update(
            where={"id": agent.id},
            data={
                "description": config.identity.description,
                "tags": config.identity.tags,
                "health_endpoint": config.health_endpoint,
                "authorized_scope": (
                    PrismaJson(config.authorized_scope.model_dump())
                    if config.authorized_scope
                    else None
                ),
            },
        )
        await self.db.capability.delete_many(where={"agent_id": agent.id})
        await self._create_capabilities(agent.id, harvest_result.capabilities)

        if agent.security:
            await self.db.authstrategy.delete_many(
                where={"security_id": agent.security.id}
            )
            await self._create_auth_strategies(agent.security.id, config)

    # -------------------------------------------------------------------------
    # Static helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_auth_type(raw_type: str) -> str:
        """Map YAML auth type string to Prisma enum value."""
        normalized = _AUTH_TYPE_MAP.get(raw_type.lower())
        if not normalized:
            raise ValidationError(
                "security.auth_strategies",
                f"Invalid auth type '{raw_type}'. Valid types: {', '.join(_AUTH_TYPE_MAP)}",
            )
        return normalized

    @staticmethod
    def _to_json_safe_dict(data: Any) -> dict:
        """Coerce a value to a JSON-safe plain dict, stripping None values."""
        if hasattr(data, "model_dump"):
            data = data.model_dump()
        elif hasattr(data, "dict"):
            data = data.dict()
        if not isinstance(data, dict):
            return {}
        try:
            return json.loads(
                json.dumps({k: v for k, v in data.items() if v is not None})
            )
        except (TypeError, ValueError) as e:
            raise ValidationError(
                "config", f"Config values must be JSON-serializable: {str(e)}"
            ) from e

    def _build_registration_response(
        self,
        agent: Any,
        harvest_result: HarvestResult,
    ) -> dict[str, Any]:
        tools = sum(1 for cap in harvest_result.capabilities if cap.type == "tool")
        resources = sum(
            1 for cap in harvest_result.capabilities if cap.type == "resource"
        )
        prompts = sum(1 for cap in harvest_result.capabilities if cap.type == "prompt")
        return {
            "agent_id": agent.id,
            "name": agent.name,
            "version": agent.version,
            "registered_at": agent.indexed_at,
            "health_status": agent.health_status,
            "capabilities_harvested": {
                "tools": tools,
                "resources": resources,
                "prompts": prompts,
            },
        }
