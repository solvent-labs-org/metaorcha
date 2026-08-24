"""OpenRouter LLM provider implementation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from .provider import LLMProvider

if TYPE_CHECKING:
    from .config import LLMConfig

logger = logging.getLogger(__name__)

_OPENROUTER_CHAT_PATH = "/chat/completions"
_OPENROUTER_EMBED_PATH = "/embeddings"


class OpenRouterProvider(LLMProvider):
    """
    LLM provider backed by the OpenRouter API (https://openrouter.ai/api/v1).

    Supports both chat completions (including structured JSON output) and
    text embeddings. All requests are retried with exponential back-off on
    transient HTTP errors (5xx, timeout).
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/solvent-labs-org/metaorcha",
                "X-Title": "Orcha",
            },
            timeout=config.timeout,
        )

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3,
    ) -> str:
        if not model:
            raise ValueError(
                "OpenRouterProvider.complete() called with an empty model string. "
                "Set LLM_DECOMPOSITION_MODEL / LLM_FALLBACK_MODEL / LLM_VALIDATION_MODEL "
                "in your .env file."
            )

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            body["response_format"] = response_format

        try:
            response = await self._request_with_retry(
                "POST", _OPENROUTER_CHAT_PATH, json=body
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 and response_format:
                # Some model/provider combinations reject response_format even
                # though OpenRouter normally handles the translation.  Strip it
                # and retry — every caller prompt already asks for JSON output,
                # so the model will still return valid JSON.
                logger.warning(
                    "OpenRouter rejected response_format=%r for model=%r (400) — "
                    "retrying without structured-output param",
                    response_format.get("type"),
                    model,
                )
                body_no_fmt = {k: v for k, v in body.items() if k != "response_format"}
                response = await self._request_with_retry(
                    "POST", _OPENROUTER_CHAT_PATH, json=body_no_fmt
                )
            else:
                raise

        data = response.json()
        return data["choices"][0]["message"]["content"]

    async def embed(self, text: str, model: str) -> list[float]:
        body: dict[str, Any] = {"model": model, "input": text}
        # OpenRouter's OpenAI-compatible embeddings endpoint accepts "dimensions"
        # for models that support configurable output vector size.
        if self._config.embedding_dimension is not None:
            body["dimensions"] = self._config.embedding_dimension
        response = await self._request_with_retry(
            "POST", _OPENROUTER_EMBED_PATH, json=body
        )
        data = response.json()
        return data["data"][0]["embedding"]

    async def _request_with_retry(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        import asyncio

        last_exc: Exception | None = None
        delay = self._config.retry_delay

        for attempt in range(1, self._config.max_retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                ):
                    # 4xx — don't retry; log the response body to help debug
                    try:
                        error_body = exc.response.json()
                    except Exception:
                        error_body = exc.response.text
                    logger.error(
                        "OpenRouter %d error — body: %s",
                        exc.response.status_code,
                        error_body,
                    )
                    raise
                logger.warning(
                    "OpenRouter request failed (attempt %d/%d): %s",
                    attempt,
                    self._config.max_retries,
                    exc,
                )
                if attempt < self._config.max_retries:
                    await asyncio.sleep(delay)
                    delay *= 2

        raise RuntimeError(
            f"OpenRouter request failed after {self._config.max_retries} attempts"
        ) from last_exc

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
