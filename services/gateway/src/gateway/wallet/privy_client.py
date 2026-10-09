"""Privy embedded-wallet client — Gateway exclusive.

Uses Privy Embedded Wallets with server-side access (NOT custodial wallets).
Keys are stored in Privy's TEE; Gateway holds the authorization via app_secret.

Chain: Base Sepolia (testnet).  The Privy Transfer API natively supports
base_sepolia, ethereum_sepolia, arbitrum_sepolia, polygon_amoy.

SuperAgent never calls this module — it only reads/writes credits_usd and
arrears_* on the User row.  All on-chain operations live here.
"""

from __future__ import annotations

import os
import re

import httpx

# Wallet and action ids are path segments of the API URLs below, which carry
# the app's credentials. A transfer's action id arrives on the client's
# request, so an id is placed in a URL only when it is one plain segment.
_ID = r"[A-Za-z0-9_-]{1,128}"


def checked_id(value: str) -> str:
    """Return ``value`` if it is one plain path segment, else raise ValueError."""
    if re.fullmatch(_ID, value):
        return value
    raise ValueError(f"not a wallet or action id: {value!r}")


# ── Lazy singleton ────────────────────────────────────────────────────────────

_privy_app_id: str | None = None
_privy_app_secret: str | None = None


def _creds() -> tuple[str, str]:
    """Return (app_id, app_secret) — resolved once from env."""
    global _privy_app_id, _privy_app_secret
    if _privy_app_id is None:
        _privy_app_id = os.environ["PRIVY_APP_ID"]
        _privy_app_secret = os.environ["PRIVY_APP_SECRET"]
    return _privy_app_id, _privy_app_secret  # type: ignore[return-value]


def _auth_headers() -> dict[str, str]:
    app_id, _ = _creds()
    return {"privy-app-id": app_id}


# ── Wallet lifecycle ──────────────────────────────────────────────────────────


async def create_wallet(display_name: str | None = None) -> dict[str, str]:
    """
    Create an EVM embedded wallet via Privy REST API.

    Called by Gateway during user registration (POST /auth/register).

    Returns:
        {"privy_wallet_id": str, "wallet_address": str}
    """
    app_id, app_secret = _creds()
    body: dict = {"chain_type": "ethereum"}
    if display_name:
        body["display_name"] = display_name[:100]
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            "https://api.privy.io/v1/wallets",
            auth=(app_id, app_secret),
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"privy_wallet_id": data["id"], "wallet_address": data["address"]}


# ── Wallet info ──────────────────────────────────────────────────────────────


async def get_wallet_address(privy_wallet_id: str) -> str:
    """Fetch the on-chain EVM address for a Privy wallet ID."""
    app_id, app_secret = _creds()
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            f"https://api.privy.io/v1/wallets/{privy_wallet_id}",
            auth=(app_id, app_secret),
            headers=_auth_headers(),
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["address"]


# ── Balance ───────────────────────────────────────────────────────────────────


async def get_wallet_balance(privy_wallet_id: str) -> dict:
    """
    Fetch live on-chain balance via Privy REST API.

    Note: credits_usd in DB is the billing source of truth.
    This is for displaying on-chain wallet state in the UI.

    Returns the raw Privy response:
        {total: {value, currency}, assets: [{symbol, amount, price}]}
    """
    app_id, app_secret = _creds()
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            f"https://api.privy.io/v1/wallets/{privy_wallet_id}/balance",
            auth=(app_id, app_secret),
            headers=_auth_headers(),
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()


# ── Transfer ─────────────────────────────────────────────────────────────────


async def transfer_usdc(
    from_wallet_id: str,
    to_address: str,
    amount_usd: float,
    chain: str = "base_sepolia",
) -> dict:
    """
    Execute a USDC transfer via Privy Transfer API.

    Transfer is ASYNC — Privy processes it in background.
    Returns wallet action with status='pending'.
    Poll get_transfer_status() for completion.

    Supported chains: base, ethereum, arbitrum, polygon
    and testnets: base_sepolia, ethereum_sepolia, arbitrum_sepolia, polygon_amoy.

    Used by:
      - Daily settlement cron: platform wallet → developer wallet
      - Developer withdrawal: developer wallet → external address
    """
    app_id, app_secret = _creds()
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.privy.io/v1/wallets/{from_wallet_id}/transfer",
            auth=(app_id, app_secret),
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={
                "source": {
                    "asset": "usdc",
                    "amount": str(amount_usd),
                    "chain": chain,
                },
                "destination": {
                    "address": to_address,
                },
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "privy_client: transfer initiated wallet=%s to=%s amount=%s chain=%s response=%s",
            from_wallet_id,
            to_address,
            amount_usd,
            chain,
            data,
        )
        return data
        # Returns: {id, wallet_id, type: "transfer", status: "pending/succeeded/rejected/failed",
        #           source, destination, balance_changes, steps}


# ── Poll action status ────────────────────────────────────────────────────────


async def get_transfer_status(wallet_id: str, action_id: str) -> tuple[str, str | None]:
    """
    Poll after transfer_usdc() to confirm settlement.

    Returns: (status, tx_hash)
      status:  'pending' | 'succeeded' | 'rejected' | 'failed'
      tx_hash: on-chain transaction hash once succeeded, else None
    """
    wallet_id, action_id = checked_id(wallet_id), checked_id(action_id)

    import logging as _logging

    _log = _logging.getLogger(__name__)

    app_id, app_secret = _creds()
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            f"https://api.privy.io/v1/wallets/{wallet_id}/actions/{action_id}",
            auth=(app_id, app_secret),
            headers=_auth_headers(),
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "unknown")

        # Extract tx hash — Privy surfaces it in steps[].transaction_hash or
        # directly as transaction_hash on the action object.
        tx_hash: str | None = data.get("transaction_hash")
        if not tx_hash:
            for step in data.get("steps", []):
                tx_hash = step.get("transaction_hash")
                if tx_hash:
                    break

        if status in ("rejected", "failed"):
            _log.error(
                "privy_client: transfer %s wallet=%s status=%s full_response=%s",
                action_id,
                wallet_id,
                status,
                data,
            )
        return status, tx_hash
