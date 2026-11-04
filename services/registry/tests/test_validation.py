"""Tests for validation service."""

import yaml

from services.registry.src.models.emerge_config import EmergeConfig
from services.registry.src.services.validation import ValidationService


class TestValidationService:
    """Test suite for ValidationService."""

    def test_validate_valid_mcp_config(self, mcp_emerge_yaml):
        """Test validation of valid MCP configuration."""
        config_data = yaml.safe_load(mcp_emerge_yaml)
        config = EmergeConfig(**config_data)

        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is True
        assert error is None

    def test_validate_valid_a2a_config(self, a2a_emerge_yaml):
        """Test validation of valid A2A configuration."""
        config_data = yaml.safe_load(a2a_emerge_yaml)
        config = EmergeConfig(**config_data)

        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is True
        assert error is None

    def test_validate_invalid_did_format(self):
        """Test validation fails for invalid DID format."""
        config_data = {
            "identity": {
                "id": "invalid-did-format",  # Wrong format
                "name": "TestAgent",
                "version": "1.0.0",
                "description": "Test",
            },
            "protocol": {
                "type": "mcp",
                "version": "2025-11-25",
                "transport": {"type": "sse", "endpoint": "https://api.example.com"},
            },
            "health_endpoint": "https://api.example.com/health",
            "security": {"transport_layer": {"type": "tls"}, "auth_strategies": []},
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "identity.id"
        assert "did:orcha:agent:" in error.reason
        assert "did:orcha:system:" in error.reason

    def test_validate_valid_system_mcp_did(self):
        """Platform MCP manifests use did:orcha:system:* (same shape as agent MCP)."""
        config_data = {
            "identity": {
                "id": "did:orcha:system:web-search",
                "name": "Web Search",
                "version": "1.0.0",
                "description": "Test",
                "tags": ["system"],
            },
            "protocol": {
                "type": "mcp",
                "version": "2024-11-05",
                "transport": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]},
            },
            "health_endpoint": "http://localhost:0/health",
            "security": {"transport_layer": {"type": "none"}, "auth_strategies": []},
        }
        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)
        assert is_valid is True
        assert error is None

    def test_validate_invalid_protocol_type(self):
        """Test validation fails for unsupported protocol type."""
        config_data = {
            "identity": {
                "id": "did:orcha:agent:test",
                "name": "TestAgent",
                "version": "1.0.0",
                "description": "Test",
            },
            "protocol": {
                "type": "unknown",  # Invalid protocol
                "version": "1.0.0",
                "transport": {"type": "sse", "endpoint": "https://api.example.com"},
            },
            "health_endpoint": "https://api.example.com/health",
            "security": {"transport_layer": {"type": "tls"}, "auth_strategies": []},
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "protocol.type"

    def test_validate_invalid_transport_type(self):
        """Test validation fails for unsupported transport type."""
        config_data = {
            "identity": {
                "id": "did:orcha:agent:test",
                "name": "TestAgent",
                "version": "1.0.0",
                "description": "Test",
            },
            "protocol": {
                "type": "mcp",
                "version": "2025-11-25",
                "transport": {
                    "type": "websocket",  # Invalid transport
                    "endpoint": "https://api.example.com",
                },
            },
            "health_endpoint": "https://api.example.com/health",
            "security": {"transport_layer": {"type": "tls"}, "auth_strategies": []},
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "protocol.transport.type"

    def test_validate_missing_endpoint_for_sse(self):
        """Test validation fails when endpoint is missing for SSE transport."""
        config_data = {
            "identity": {
                "id": "did:orcha:agent:test",
                "name": "TestAgent",
                "version": "1.0.0",
                "description": "Test",
            },
            "protocol": {
                "type": "mcp",
                "version": "2025-11-25",
                "transport": {
                    "type": "sse"
                    # Missing endpoint
                },
            },
            "health_endpoint": "https://api.example.com/health",
            "security": {"transport_layer": {"type": "tls"}, "auth_strategies": []},
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "protocol.transport.endpoint"

    def test_validate_missing_command_for_stdio(self):
        """Test validation fails when command is missing for STDIO transport."""
        config_data = {
            "identity": {
                "id": "did:orcha:agent:test",
                "name": "TestAgent",
                "version": "1.0.0",
                "description": "Test",
            },
            "protocol": {
                "type": "mcp",
                "version": "2025-11-25",
                "transport": {
                    "type": "stdio"
                    # Missing command
                },
            },
            "health_endpoint": "https://api.example.com/health",
            "security": {"transport_layer": {"type": "tls"}, "auth_strategies": []},
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "protocol.transport.command"

    def test_validate_missing_mtls_config(self):
        """Test validation fails when mtls_config is missing for MTLS type."""
        config_data = {
            "identity": {
                "id": "did:orcha:agent:test",
                "name": "TestAgent",
                "version": "1.0.0",
                "description": "Test",
            },
            "protocol": {
                "type": "mcp",
                "version": "2025-11-25",
                "transport": {"type": "sse", "endpoint": "https://api.example.com"},
            },
            "health_endpoint": "https://api.example.com/health",
            "security": {
                "transport_layer": {
                    "type": "mtls"
                    # Missing mtls_config
                },
                "auth_strategies": [],
            },
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "security.transport_layer.mtls_config"

    def test_validate_invalid_health_endpoint_format(self):
        """Test validation fails for invalid health endpoint URL."""
        config_data = {
            "identity": {
                "id": "did:orcha:agent:test",
                "name": "TestAgent",
                "version": "1.0.0",
                "description": "Test",
            },
            "protocol": {
                "type": "mcp",
                "version": "2025-11-25",
                "transport": {"type": "sse", "endpoint": "https://api.example.com"},
            },
            "health_endpoint": "not-a-valid-url",  # Invalid URL
            "security": {"transport_layer": {"type": "tls"}, "auth_strategies": []},
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "health_endpoint"

    def test_validate_invalid_semantic_version(self):
        """Test validation fails for invalid semantic versioning."""
        config_data = {
            "identity": {
                "id": "did:orcha:agent:test",
                "name": "TestAgent",
                "version": "1.0",  # Invalid - should be x.y.z
                "description": "Test",
            },
            "protocol": {
                "type": "mcp",
                "version": "2025-11-25",
                "transport": {"type": "sse", "endpoint": "https://api.example.com"},
            },
            "health_endpoint": "https://api.example.com/health",
            "security": {"transport_layer": {"type": "tls"}, "auth_strategies": []},
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "identity.version"

    def test_validate_version_with_non_numeric_parts(self):
        """Test validation fails when version contains non-numeric parts."""
        config_data = {
            "identity": {
                "id": "did:orcha:agent:test",
                "name": "TestAgent",
                "version": "1.0.beta",  # Invalid - non-numeric
                "description": "Test",
            },
            "protocol": {
                "type": "mcp",
                "version": "2025-11-25",
                "transport": {"type": "sse", "endpoint": "https://api.example.com"},
            },
            "health_endpoint": "https://api.example.com/health",
            "security": {"transport_layer": {"type": "tls"}, "auth_strategies": []},
        }

        config = EmergeConfig(**config_data)
        is_valid, error = ValidationService.validate_emerge_config(config)

        assert is_valid is False
        assert error is not None
        assert error.field == "identity.version"


class TestCapabilityIdCharset:
    """AD-17: a capability id is one half of a recorded step's ``tool`` string."""

    @staticmethod
    def _cap(cap_id):
        from services.registry.src.adapters.base import CapabilityData

        return CapabilityData(type="tool", id=cap_id, name=cap_id, description="")

    def test_valid_ids_pass(self):
        caps = [self._cap(c) for c in ("search_repos", "notion-fetch", "a.b:c", "X1")]
        ok, error = ValidationService.validate_capability_ids(caps)
        assert ok is True
        assert error is None

    def test_hash_is_rejected_with_named_error(self):
        ok, error = ValidationService.validate_capability_ids([self._cap("did#cap")])
        assert ok is False
        assert error is not None
        assert error.field == "capabilities.id"
        assert "did#cap" in error.reason

    def test_whitespace_and_non_ascii_are_rejected(self):
        for bad in ("with space", "ünïcode", "a/b", ""):
            ok, _ = ValidationService.validate_capability_ids([self._cap(bad)])
            assert ok is False, bad

    def test_malformed_capability_object_never_raises(self):
        ok, error = ValidationService.validate_capability_ids([object(), None])
        assert ok is False
        assert error is not None
