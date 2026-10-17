"""Agent registration and management endpoints."""

import logging
import math
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)

from common.database.src.generated_client import Prisma
from common.kafka.src import KafkaProducer, KafkaTopics

from ...models.api_responses import (
    AgentListItem,
    CapabilitiesHarvested,
    DeleteAgentResponse,
    ListAgentsData,
    ListAgentsResponse,
    PaginationInfo,
    RegisterAgentData,
    RegisterAgentResponse,
    UpdateAgentData,
    UpdateAgentResponse,
)
from ...services import ConflictError, RegistrationService, ValidationError
from ..dependencies import get_db, verify_token

# A connection's token, sent by the Gateway so the harvest can list a server
# that gates tools/list. Used as that harvest's Authorization and dropped.
HARVEST_AUTHORIZATION_HEADER = "X-Harvest-Authorization"


def _harvest_headers(value: str | None) -> dict[str, str] | None:
    return {"Authorization": value} if value else None


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


async def _build_manifest_payload(db: Prisma, agent_id: str) -> dict[str, Any]:
    """
    Fetch the full agent record from the DB and return a flat manifest dict
    suitable for the ``registry.agent.registered`` Kafka event payload.

    The structure mirrors what PnD's SemanticTemplateGenerator and
    TDWAEmbeddingGenerator expect.
    """
    agent = await db.agent.find_unique(
        where={"id": agent_id},
        include={"capabilities": True},
    )
    if agent is None:
        return {"id": agent_id}

    return {
        "id": agent.id,
        "name": agent.name,
        "version": agent.version,
        "description": agent.description or "",
        "provider": agent.provider or "",
        "tags": agent.tags or [],
        "protocol_type": agent.protocol_type.lower() if agent.protocol_type else "",
        "protocol_version": agent.protocol_version or "",
        "health_status": agent.health_status.lower()
        if agent.health_status
        else "unknown",
        "health_endpoint": agent.health_endpoint or "",
        "reliability_score": 100,  # default until runtime metrics are collected
        "capabilities": [
            {
                "name": cap.name,
                "description": cap.description or "",
                "type": cap.type.lower() if cap.type else "",
                "id": cap.capability_id,
                "input_schema": cap.input_schema or {},
                "output_schema": cap.output_schema or {},
            }
            for cap in (agent.capabilities or [])
        ],
    }


@router.post(
    "/register",
    response_model=RegisterAgentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_agent(
    request: Request,
    emerge_yaml: Annotated[
        UploadFile, File(description="emerge.yaml configuration file")
    ],
    user_id: Annotated[str, Depends(verify_token)],
    db: Annotated[Prisma, Depends(get_db)],
    harvest_authorization: Annotated[
        str | None, Header(alias=HARVEST_AUTHORIZATION_HEADER)
    ] = None,
):
    """
    Register a new agent.

    This endpoint:
    1. Validates the emerge.yaml configuration
    2. Harvests capabilities from the agent
    3. Stores the agent manifest in the database
    4. Creates a version snapshot
    5. Emits a ``registry.agent.registered`` Kafka event (if Kafka is configured)

    Returns:
        201 Created: Agent registered successfully
        400 Bad Request: Validation error
        401 Unauthorized: Invalid PAT token
        409 Conflict: Agent already exists
        503 Service Unavailable: Agent endpoint unreachable
    """
    try:
        # Read file content
        content = await emerge_yaml.read()
        emerge_yaml_str = content.decode("utf-8")

        # Register agent
        kafka_producer = getattr(request.app.state, "kafka_producer", None)
        registration_service = RegistrationService(db, kafka_producer=kafka_producer)
        result = await registration_service.register_agent(
            emerge_yaml_content=emerge_yaml_str,
            user_id=user_id,
            harvest_headers=_harvest_headers(harvest_authorization),
        )

        agent_id: str = result["agent_id"]

        # Emit Kafka event (best-effort — never block or fail the HTTP response)
        producer: KafkaProducer | None = getattr(
            request.app.state, "kafka_producer", None
        )
        if producer:
            try:
                manifest = await _build_manifest_payload(db, agent_id)
                await producer.publish(
                    topic=KafkaTopics.REGISTRY_AGENT_REGISTERED,
                    payload={"agent_id": agent_id, "manifest": manifest},
                    key=agent_id,
                )
                logger.debug("Emitted registry.agent.registered for agent %s", agent_id)
            except Exception:
                logger.exception(
                    "Failed to emit Kafka event for agent %s — registration still succeeded",
                    agent_id,
                )

        # Build response
        return RegisterAgentResponse(
            status="success",
            data=RegisterAgentData(
                agent_id=agent_id,
                name=result["name"],
                version=result["version"],
                registered_at=result["registered_at"],
                health_status=result["health_status"],
                capabilities_harvested=CapabilitiesHarvested(
                    **result["capabilities_harvested"]
                ),
            ),
        )

    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Invalid emerge.yaml configuration",
                "details": {"field": e.field, "reason": e.reason},
            },
        ) from e

    except ConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "AGENT_ALREADY_EXISTS",
                "message": e.reason,
                "details": {"resource": e.resource},
            },
        ) from e

    except ConnectionError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ENDPOINT_UNREACHABLE", "message": str(e), "details": {}},
        ) from e

    except Exception as e:
        logger.exception("Unexpected error during agent registration: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred",
                "details": {"error": str(e)},
            },
        ) from e


@router.get("/{agent_id}", response_model=dict)
async def get_agent_manifest(
    agent_id: str,
    user_id: Annotated[str, Depends(verify_token)],
    db: Annotated[Prisma, Depends(get_db)],
):
    """
    Get full Universal Agent Manifest for an agent.

    Returns:
        200 OK: Agent manifest
        401 Unauthorized: Invalid PAT token
        404 Not Found: Agent doesn't exist
    """
    agent = await db.agent.find_unique(
        where={"id": agent_id},
        include={
            "transport": True,
            "security": {"include": {"auth_strategies": True}},
            "payment": True,
            "capabilities": True,
        },
    )

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NOT_FOUND",
                "message": "Agent not found",
                "details": {"agent_id": agent_id},
            },
        )

    # Build manifest response
    return {
        "status": "success",
        "data": {
            "identity": {
                "id": agent.id,
                "name": agent.name,
                "version": agent.version,
                "provider": agent.provider,
                "owner_contact": agent.owner_contact,
                "description": agent.description,
                "tags": agent.tags,
            },
            "metadata": {
                "indexed_at": agent.indexed_at.isoformat(),
                "health_status": agent.health_status.lower(),
                "health_endpoint": agent.health_endpoint,
                # Story 1.7: a removed agent is soft-deleted, not erased, so
                # old receipts and runs still resolve it. Callers that must
                # stop on removal (the SuperAgent, for a connection) read this.
                "is_active": agent.is_active,
            },
            "protocol": {
                "type": agent.protocol_type.lower(),
                "version": agent.protocol_version,
                "transport": {
                    "type": agent.transport.type.lower() if agent.transport else None,
                    "endpoint": agent.transport.endpoint if agent.transport else None,
                    "command": agent.transport.command if agent.transport else None,
                    "args": agent.transport.args if agent.transport else None,
                    "env": agent.transport.env if agent.transport else None,
                },
            },
            "security": {
                "transport_layer": {
                    "type": (
                        agent.security.transport_layer_type.lower()
                        if agent.security
                        else None
                    ),
                },
                "auth_strategies": [
                    {
                        "id": auth.strategy_id,
                        "type": auth.type.lower(),
                        "config": auth.config,
                    }
                    for auth in (
                        agent.security.auth_strategies if agent.security else []
                    )
                ],
            },
            "payment": {
                "enabled": agent.payment.enabled if agent.payment else False,
                "base_fee": agent.payment.base_fee if agent.payment else None,
            },
            "authorized_scope": agent.authorized_scope,
            "capabilities": [
                {
                    "type": cap.type.lower(),
                    "id": cap.capability_id,
                    "name": cap.name,
                    "description": cap.description,
                    "input_schema": cap.input_schema,
                    "output_schema": cap.output_schema,
                    "uri_template": cap.uri_template,
                    "mime_type": cap.mime_type,
                    "arguments": cap.arguments,
                }
                for cap in agent.capabilities
            ],
        },
    }


@router.get("", response_model=ListAgentsResponse)
async def list_agents(
    user_id: Annotated[str, Depends(verify_token)],
    db: Annotated[Prisma, Depends(get_db)],
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    status: str | None = Query(None, description="Filter by health status"),
    protocol: str | None = Query(None, description="Filter by protocol type"),
):
    """
    List all agents registered by the authenticated user.

    Query Parameters:
        - page: Page number (default: 1)
        - limit: Items per page (default: 20, max: 100)
        - status: Filter by health status (healthy, unhealthy, unknown)
        - protocol: Filter by protocol type (mcp, a2a)

    Returns:
        200 OK: List of agents with pagination
        401 Unauthorized: Invalid PAT token
    """
    # Build filters
    filters = {"user_id": user_id, "is_active": True}

    if status:
        filters["health_status"] = status.upper()

    if protocol:
        filters["protocol_type"] = protocol.upper()

    # Get total count
    total = await db.agent.count(where=filters)

    # Calculate pagination
    skip = (page - 1) * limit
    total_pages = math.ceil(total / limit)

    # Get agents
    agents = await db.agent.find_many(
        where=filters, skip=skip, take=limit, order={"indexed_at": "desc"}
    )

    return ListAgentsResponse(
        status="success",
        data=ListAgentsData(
            agents=[
                AgentListItem(
                    id=agent.id,
                    name=agent.name,
                    version=agent.version,
                    health_status=agent.health_status.lower(),
                    protocol_type=agent.protocol_type.lower(),
                    indexed_at=agent.indexed_at,
                    execution_count=agent.execution_count,
                    success_rate=agent.success_rate,
                )
                for agent in agents
            ],
            pagination=PaginationInfo(
                page=page,
                limit=limit,
                total=total,
                total_pages=total_pages,
            ),
        ),
    )


@router.put("/{agent_id}", response_model=UpdateAgentResponse)
async def update_agent(
    agent_id: str,
    emerge_yaml: Annotated[
        UploadFile, File(description="Updated emerge.yaml configuration")
    ],
    user_id: Annotated[str, Depends(verify_token)],
    db: Annotated[Prisma, Depends(get_db)],
    harvest_authorization: Annotated[
        str | None, Header(alias=HARVEST_AUTHORIZATION_HEADER)
    ] = None,
):
    """
    Update an existing agent.

    If the version number changes, a new version record is created.

    Returns:
        200 OK: Updated existing version
        201 Created: New version created
        401 Unauthorized: Invalid PAT token
        403 Forbidden: Not owner of agent
        404 Not Found: Agent doesn't exist
    """
    try:
        # Read file content
        content = await emerge_yaml.read()
        emerge_yaml_str = content.decode("utf-8")

        # Update agent
        registration_service = RegistrationService(db)
        result = await registration_service.update_agent(
            agent_id=agent_id,
            emerge_yaml_content=emerge_yaml_str,
            user_id=user_id,
            harvest_headers=_harvest_headers(harvest_authorization),
        )

        return UpdateAgentResponse(status="success", data=UpdateAgentData(**result))

    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "VALIDATION_ERROR",
                "message": str(e),
                "details": {"field": e.field, "reason": e.reason},
            },
        ) from e

    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": str(e), "details": {}},
        ) from e


@router.delete("/{agent_id}", response_model=DeleteAgentResponse)
async def delete_agent(
    agent_id: str,
    user_id: Annotated[str, Depends(verify_token)],
    db: Annotated[Prisma, Depends(get_db)],
):
    """
    Soft-delete an agent (sets is_active = false).

    Returns:
        200 OK: Agent deleted
        401 Unauthorized: Invalid PAT token
        403 Forbidden: Not owner
        404 Not Found: Agent doesn't exist
    """
    agent = await db.agent.find_unique(where={"id": agent_id})

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NOT_FOUND",
                "message": "Agent not found",
                "details": {"agent_id": agent_id},
            },
        )

    if agent.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "You don't have permission to delete this agent",
                "details": {},
            },
        )

    # Soft delete
    await db.agent.update(where={"id": agent_id}, data={"is_active": False})

    return DeleteAgentResponse(status="success", message="Agent deleted successfully")
