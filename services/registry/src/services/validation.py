"""Validation service for emerge.yaml and agent data."""

import re
from collections.abc import Iterable
from typing import Any

from ..models.emerge_config import EmergeConfig

# AD-17: a capability id becomes the ``<connection DID>#<capability>`` half of
# a recorded step's ``tool`` string, so it may never contain ``#`` or anything
# outside this charset. Mirrors superagent.middleware.scope_classes.
CAPABILITY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


class ValidationError(Exception):
    """Validation error with field and reason."""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"Validation error in {field}: {reason}")


class ConflictError(Exception):
    """Error raised when a resource already exists."""

    def __init__(self, resource: str, reason: str):
        self.resource = resource
        self.reason = reason
        super().__init__(f"Conflict on {resource}: {reason}")


class ValidationService:
    """Service for validating agent registration data."""

    @staticmethod
    def validate_emerge_config(
        config: EmergeConfig,
    ) -> tuple[bool, ValidationError | None]:
        """
        Validate emerge.yaml configuration.

        Args:
            config: Parsed EmergeConfig

        Returns:
            Tuple of (is_valid, error)
        """
        # Validate DID format
        if not config.validate_did_format():
            return False, ValidationError(
                "identity.id",
                "Must start with 'did:orcha:agent:' or 'did:orcha:system:'",
            )

        # Validate protocol type
        if not config.validate_protocol_type():
            return False, ValidationError(
                "protocol.type", "Must be 'mcp', 'a2a', or 'computer_use'"
            )

        # Validate transport type
        if not config.validate_transport_type():
            return False, ValidationError(
                "protocol.transport.type", "Must be 'sse', 'stdio', or 'http'"
            )

        # Validate transport endpoint for SSE/HTTP
        if (
            config.protocol.transport.type.lower() in ["sse", "http"]
            and not config.protocol.transport.endpoint
        ):
            return False, ValidationError(
                "protocol.transport.endpoint",
                f"Required for {config.protocol.transport.type} transport",
            )

        # Validate STDIO command
        if (
            config.protocol.transport.type.lower() == "stdio"
            and not config.protocol.transport.command
        ):
            return False, ValidationError(
                "protocol.transport.command", "Required for stdio transport"
            )

        # Validate mTLS config if transport layer is mtls
        if (
            config.security.transport_layer.type.lower() == "mtls"
            and not config.security.transport_layer.mtls_config
        ):
            return False, ValidationError(
                "security.transport_layer.mtls_config",
                "Required when transport_layer.type is 'mtls'",
            )

        # Validate health endpoint
        if not config.health_endpoint:
            return False, ValidationError(
                "health_endpoint", "Health endpoint is required"
            )

        # Validate health endpoint format (basic URL validation)
        if not (
            config.health_endpoint.startswith("http://")
            or config.health_endpoint.startswith("https://")
        ):
            return False, ValidationError(
                "health_endpoint", "Must be a valid HTTP/HTTPS URL"
            )

        # Validate version format (semantic versioning)
        version_parts = config.identity.version.split(".")
        if len(version_parts) != 3:
            return False, ValidationError(
                "identity.version", "Must follow semantic versioning (e.g., 1.0.0)"
            )

        try:
            for part in version_parts:
                int(part)
        except ValueError:
            return False, ValidationError(
                "identity.version", "Version parts must be integers"
            )

        return True, None

    @staticmethod
    def validate_capability_ids(
        capabilities: Iterable[Any],
    ) -> tuple[bool, ValidationError | None]:
        """Every harvested capability id must fit ``[A-Za-z0-9_.:-]+`` (AD-17).

        Rejects an id containing ``#``, whitespace or non-ASCII before the
        agent is saved, so no recorded step can ever carry an ambiguous
        ``tool`` string. Never raises on a malformed capability object.
        """
        for cap in capabilities:
            cap_id = getattr(cap, "id", None)
            if not isinstance(cap_id, str) or not CAPABILITY_ID_PATTERN.match(cap_id):
                return False, ValidationError(
                    "capabilities.id",
                    f"Capability id {cap_id!r} must match [A-Za-z0-9_.:-]+ "
                    "(no '#', whitespace or non-ASCII)",
                )
        return True, None

    @staticmethod
    def validate_agent_id_uniqueness(agent_id: str, user_id: str, version: str) -> bool:
        """
        Validate that agent ID is unique for this user and version.

        This would typically query the database, but that's handled in the
        registration service. This is a placeholder for additional validation.

        Args:
            agent_id: Agent DID
            user_id: User ID
            version: Agent version

        Returns:
            True if valid
        """
        # Basic validation - actual uniqueness check is in registration service
        return bool(agent_id and user_id and version)
