"""VaultClient — Postgres-backed AES-256-GCM secret storage."""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_NONCE_BYTES = 12  # 96-bit nonce for AES-256-GCM


def _get_vault_key() -> bytes:
    from ..config import settings

    raw = settings.vault_key
    # Accept base64-encoded or raw 32-byte key
    try:
        key = base64.b64decode(raw)
    except Exception:
        key = raw.encode()
    if len(key) != 32:
        raise ValueError(
            f"VAULT_KEY must be 32 bytes (got {len(key)}). "
            "Generate with: openssl rand -base64 32"
        )
    return key


def _encrypt(plaintext: str) -> bytes:
    key = _get_vault_key()
    nonce = os.urandom(_NONCE_BYTES)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return nonce + ciphertext


def _decrypt(data: bytes) -> str:
    key = _get_vault_key()
    nonce = data[:_NONCE_BYTES]
    ciphertext = data[_NONCE_BYTES:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


class VaultClient:
    """Manages encrypted secrets in the user_secrets and agent_registrations tables."""

    async def get_user_secret(self, user_id: str, key: str) -> str | None:
        """Retrieve and decrypt a user secret."""
        try:
            from src.generated_client import Prisma

            db = Prisma()
            await db.connect()
            try:
                record = await db.usersecret.find_unique(
                    where={"user_id_key": {"user_id": user_id, "key": key}}
                )
                if record is None:
                    return None
                return _decrypt(record.encrypted_value.decode())
            finally:
                await db.disconnect()
        except Exception:
            logger.debug(
                "get_user_secret failed for %s/%s", user_id, key, exc_info=True
            )
            return None

    async def save_user_secret(self, user_id: str, key: str, value: str) -> None:
        """Encrypt and store a user secret."""
        encrypted = _encrypt(value)
        try:
            from src.generated_client import Prisma
            from src.generated_client import fields as prisma_fields

            b64_encrypted = prisma_fields.Base64.encode(encrypted)
            db = Prisma()
            await db.connect()
            try:
                await db.usersecret.upsert(
                    where={"user_id_key": {"user_id": user_id, "key": key}},
                    data={
                        "create": {
                            "user_id": user_id,
                            "key": key,
                            "encrypted_value": b64_encrypted,
                        },
                        "update": {"encrypted_value": b64_encrypted},
                    },
                )
            finally:
                await db.disconnect()
        except Exception:
            logger.exception("save_user_secret failed for %s/%s", user_id, key)
            raise

    async def get_agent_registration(
        self, agent_id: str, user_id: str
    ) -> dict[str, str] | None:
        """Retrieve decrypted OAuth client credentials for an agent."""
        try:
            from src.generated_client import Prisma

            db = Prisma()
            await db.connect()
            try:
                record = await db.agentregistration.find_unique(
                    where={
                        "agent_id_user_id": {
                            "agent_id": agent_id,
                            "user_id": user_id,
                        }
                    }
                )
                if record is None:
                    return None
                return {
                    "client_id": record.client_id,
                    "client_secret": _decrypt(record.encrypted_client_secret.decode()),
                }
            finally:
                await db.disconnect()
        except Exception:
            logger.debug("get_agent_registration failed", exc_info=True)
            return None

    async def save_agent_registration(
        self,
        agent_id: str,
        user_id: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        encrypted_secret = _encrypt(client_secret)
        try:
            from src.generated_client import Prisma
            from src.generated_client import fields as prisma_fields

            b64_secret = prisma_fields.Base64.encode(encrypted_secret)
            db = Prisma()
            await db.connect()
            try:
                await db.agentregistration.upsert(
                    where={
                        "agent_id_user_id": {
                            "agent_id": agent_id,
                            "user_id": user_id,
                        }
                    },
                    data={
                        "create": {
                            "agent_id": agent_id,
                            "user_id": user_id,
                            "client_id": client_id,
                            "encrypted_client_secret": b64_secret,
                        },
                        "update": {
                            "client_id": client_id,
                            "encrypted_client_secret": b64_secret,
                        },
                    },
                )
            finally:
                await db.disconnect()
        except Exception:
            logger.exception("save_agent_registration failed")
            raise

    async def get_global_provider_creds(self, provider: str) -> dict[str, str] | None:
        """
        Retrieve global (non-user-specific) provider credentials.

        Stored as user_id='__global__', key=provider.
        """
        secret = await self.get_user_secret("__global__", provider)
        if secret is None:
            return None
        return {"api_key": secret}

    # ── Agent env-var credentials ────────────────────────────────────────────
    # Key convention: "agent:{agent_id}:env:{VAR_NAME}"
    # This links the ${VAR_NAME} placeholders in emerge.yaml transport.env
    # to per-user encrypted values in the user_secrets table.

    async def get_agent_env(
        self, user_id: str, agent_id: str, var_name: str
    ) -> str | None:
        """Retrieve a stored env-var credential for a specific agent."""
        return await self.get_user_secret(user_id, f"agent:{agent_id}:env:{var_name}")

    async def save_agent_env(
        self, user_id: str, agent_id: str, var_name: str, value: str
    ) -> None:
        """Encrypt and store an env-var credential for a specific agent."""
        await self.save_user_secret(user_id, f"agent:{agent_id}:env:{var_name}", value)

    async def delete_agent_env(self, user_id: str, agent_id: str, var_name: str) -> int:
        """Delete a stored env-var credential for a specific agent.

        Returns the number of rows removed (0 when there was none). A database
        error propagates: a revoke that did not happen must not read as done
        (story 1.7 — the old ``delete`` swallowed every error, not only the
        missing row).
        """
        key = f"agent:{agent_id}:env:{var_name}"
        return await self._delete_secrets(
            {"user_id": user_id, "key": key}, f"{user_id} / {key}"
        )

    async def delete_all_agent_env(self, user_id: str, agent_id: str) -> int:
        """Delete every env-var credential this user holds for one agent.

        Removing a whole connection (story 1.7). Filtered on the caller's
        ``user_id`` and on the exact prefix ``agent:<DID>:env:`` — the
        ``:env:`` right after the DID means no other agent's rows match, and
        a bare legacy ``<VAR>`` row is never touched. Returns the count.
        """
        prefix = f"agent:{agent_id}:env:"
        return await self._delete_secrets(
            {"user_id": user_id, "key": {"startswith": prefix}},
            f"{user_id} / {prefix}*",
        )

    async def _delete_secrets(self, where: dict[str, Any], label: str) -> int:
        try:
            from src.generated_client import Prisma

            db = Prisma()
            await db.connect()
            try:
                return int(await db.usersecret.delete_many(where=where))
            finally:
                await db.disconnect()
        except Exception:
            # the label carries request values (user and agent ids): escape
            # line breaks so it cannot forge a log line
            logger.exception(
                "deleting user secrets failed for %s",
                label.replace("\r", "\\r").replace("\n", "\\n"),
            )
            raise

    async def list_agent_env_status(
        self, user_id: str, agent_id: str, var_names: list[str]
    ) -> dict[str, bool]:
        """
        Check which env-var names are configured for an agent.

        Returns a mapping of var_name → True (configured) / False (missing).
        Values are never returned — only existence is indicated.
        """
        return {
            var: (await self.get_agent_env(user_id, agent_id, var)) is not None
            for var in var_names
        }
