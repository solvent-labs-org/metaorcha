"""Wallet API routes — balance, fund info, withdraw, transaction history.

RBAC is enforced entirely here — Privy has no concept of User vs Developer roles.
All on-chain operations require Gateway's app_secret; user JWT is never sent to Privy.

DEV role proxy: user.is_dev_mode == True (maps to UserRole.DEV once schema adds role enum).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..auth.models import TokenPayload
from ..config import settings
from ..dependencies import require_auth

_MOCK_MODE = settings.payment_mode == "mock"

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wallet", tags=["wallet"])


# ── Request / Response models ─────────────────────────────────────────────────


class WithdrawRequest(BaseModel):
    amount: Decimal
    to_address: str | None = None  # overrides user.withdrawal_address if provided


class WalletBalanceResponse(BaseModel):
    credits_usd: float
    arrears_usd: float
    arrears_flag: bool
    wallet_address: str | None
    chain: str
    on_chain_usdc: (
        dict | None
    )  # raw Privy balance payload; None in mock mode or fetch error
    mock_mode: bool = False  # True when payment_mode=mock; UI can hide on-chain UI


class WalletFundResponse(BaseModel):
    wallet_address: str | None
    chain: str
    asset: str
    note: str
    mock_mode: bool = False


class TransactionResponse(BaseModel):
    id: str
    session_id: str
    agent_id: str
    base_fee: float
    platform_cut: float
    developer_payout: float
    status: str
    created_at: str
    settled_at: str | None
    chain_id: str | None
    tx_hash: str | None


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_user(user_id: str, db: Any) -> Any:
    user = await db.user.find_unique(where={"id": user_id})
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return user


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/balance", response_model=WalletBalanceResponse)
async def get_balance(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> WalletBalanceResponse:
    """
    Returns internal credits_usd (billing source of truth) plus
    live on-chain USDC balance from Privy (informational display).
    """
    db = request.app.state.db
    user = await _get_user(payload.user_id, db)

    on_chain: dict | None = None
    if not _MOCK_MODE and getattr(user, "privy_wallet_id", None):
        try:
            from .privy_client import get_wallet_balance

            on_chain = await get_wallet_balance(user.privy_wallet_id)
        except Exception:
            logger.warning("Failed to fetch on-chain balance for user=%s", user.id)

    return WalletBalanceResponse(
        credits_usd=float(user.credits_usd),
        arrears_usd=float(getattr(user, "arrears_usd", 0.0)),
        arrears_flag=bool(getattr(user, "arrears_flag", False)),
        wallet_address=getattr(user, "wallet_address", None),
        chain="Base Sepolia",
        on_chain_usdc=on_chain,
        mock_mode=_MOCK_MODE,
    )


@router.get("/fund", response_model=WalletFundResponse)
async def get_fund_info(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> WalletFundResponse:
    """
    Returns the user's unique smart wallet address for USDC deposit.
    Lazily provisions a wallet via wallet-service if the user does not have one yet.
    """
    if _MOCK_MODE:
        return WalletFundResponse(
            wallet_address=None,
            chain="Base Sepolia",
            asset="USDC",
            note="Wallet infra not configured (payment_mode=mock). Set PAYMENT_MODE=testnet and configure wallet-service to enable on-chain funding.",
            mock_mode=True,
        )

    db = request.app.state.db
    user = await _get_user(payload.user_id, db)
    smart_wallet_address: str | None = getattr(user, "wallet_address", None)

    if not smart_wallet_address:
        try:
            import httpx

            async with httpx.AsyncClient() as _http:
                _resp = await _http.post(
                    f"{settings.wallet_service_url}/internal/wallet/create",
                    json={
                        "chain": "base",
                        "display_name": getattr(user, "display_name", None)
                        or user.email,
                    },
                    timeout=30.0,
                )
                _resp.raise_for_status()
                _wallet = _resp.json()

            await db.user.update(
                where={"id": user.id},
                data={
                    "privy_wallet_id": _wallet["wallet_id"],
                    "eoa_wallet_address": _wallet["eoa_address"],
                    "wallet_address": _wallet["smart_wallet_address"],
                },
            )
            smart_wallet_address = _wallet["smart_wallet_address"]
            logger.info(
                "Smart wallet lazily provisioned for user=%s smart=%s",
                user.id,
                smart_wallet_address,
            )
        except Exception:
            logger.exception(
                "get_fund_info: failed to lazily provision wallet for user=%s", user.id
            )

    return WalletFundResponse(
        wallet_address=smart_wallet_address,
        chain="Base Sepolia",
        asset="USDC",
        note="Send USDC to your unique Smart Wallet address. Credits are applied automatically.",
        mock_mode=False,
    )


@router.post("/withdraw")
async def withdraw(
    body: WithdrawRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> dict:
    """
    Initiate USDC transfer from user's Privy wallet to an external address.
    Restricted to developer accounts (is_dev_mode == True).
    Transfer is async — poll /wallet/transfer/{action_id}/status for completion.
    """
    db = request.app.state.db
    user = await _get_user(payload.user_id, db)

    # ── Authorization (backend-enforced, never Privy-level) ───────────────────
    if not getattr(user, "is_dev_mode", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Withdrawals are restricted to developer accounts",
        )
    if getattr(user, "arrears_flag", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Clear outstanding arrears balance before withdrawing",
        )

    user_credits = Decimal(str(user.credits_usd))
    if user_credits < body.amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Insufficient credits",
        )

    destination = body.to_address or getattr(user, "withdrawal_address", None)
    if not destination:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No withdrawal address configured. Provide to_address or set withdrawal_address in profile.",
        )

    wallet_id = getattr(user, "privy_wallet_id", None)
    eoa_address = getattr(user, "eoa_wallet_address", None)
    if not wallet_id or not eoa_address:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No smart wallet provisioned for this account",
        )

    # ── Execute via wallet-service (gas-sponsored AA transfer) ───────────────
    if _MOCK_MODE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Withdrawals are disabled in mock mode. Set PAYMENT_MODE=testnet to enable.",
        )

    import httpx

    amount_wei = int(body.amount * 1_000_000)  # USDC has 6 decimals
    async with httpx.AsyncClient() as _http:
        _resp = await _http.post(
            f"{settings.wallet_service_url}/internal/wallet/transfer",
            json={
                "chain": "base",
                "wallet_id": wallet_id,
                "sender_address": eoa_address,
                "to_address": destination,
                "amount_usdc_base_units": str(amount_wei),
            },
            timeout=60.0,
        )
        _resp.raise_for_status()
        action = _resp.json()

    # Deduct credits immediately; on-chain confirmation is synchronous for AA
    new_credits = float(credits - body.amount)
    await db.user.update(
        where={"id": user.id},
        data={"credits_usd": new_credits},
    )

    logger.info(
        "Withdraw completed: user=%s amount=%s tx_hash=%s",
        user.id,
        body.amount,
        action.get("tx_hash"),
    )
    return {"tx_hash": action["tx_hash"], "status": action["status"]}


@router.get("/transfer/{action_id}/status")
async def get_action_status(
    action_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> dict:
    """Poll a pending transfer action for completion status."""
    db = request.app.state.db
    user = await _get_user(payload.user_id, db)

    wallet_id = getattr(user, "privy_wallet_id", None)
    if not wallet_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No wallet provisioned",
        )

    if _MOCK_MODE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Transfer status polling is disabled in mock mode.",
        )

    from .privy_client import checked_id, get_transfer_status

    try:
        checked_id(action_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not a transfer action id",
        ) from exc
    tx_status = await get_transfer_status(wallet_id, action_id)
    return {"action_id": action_id, "status": tx_status}


@router.get("/transactions")
async def get_transactions(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
    page: int = 1,
) -> dict:
    """
    Paginated transaction history for the authenticated user.
    Returns Transaction records ordered by created_at desc.
    """
    db = request.app.state.db

    page_size = 20
    skip = (page - 1) * page_size

    # Prisma raw query to paginate — Transaction model added in Step 2 migration.
    try:
        rows = await db.transaction.find_many(
            where={"user_id": payload.user_id},
            order={"created_at": "desc"},
            take=page_size,
            skip=skip,
        )
        total = await db.transaction.count(where={"user_id": payload.user_id})
    except Exception:
        # Transaction table doesn't exist yet (migration pending) — return empty list.
        logger.debug("Transaction table not available yet")
        return {"transactions": [], "page": page, "total": 0}

    return {
        "transactions": [
            {
                "id": r.id,
                "session_id": r.session_id,
                "agent_id": r.agent_id,
                "base_fee": float(r.base_fee),
                "platform_cut": float(r.platform_cut),
                "developer_payout": float(r.developer_payout),
                "status": r.status,
                "created_at": r.created_at.isoformat(),
                "settled_at": r.settled_at.isoformat() if r.settled_at else None,
                "chain_id": getattr(r, "chain_id", None),
                "tx_hash": getattr(r, "tx_hash", None),
            }
            for r in rows
        ],
        "page": page,
        "total": total,
    }
