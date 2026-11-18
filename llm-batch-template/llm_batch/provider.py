import asyncio
from typing import TYPE_CHECKING

from .connection import DEFAULT_OUTPUT_LIMIT_FIELD
from .control import retry_after_seconds

if TYPE_CHECKING:
    import httpx

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
QUOTA_ERROR_CODES = {"insufficient_quota", "billing_hard_limit_reached"}


class RowError(Exception):
    """A known error that applies to an individual input."""


class FatalProviderError(Exception):
    """An API setup or access problem that should stop the batch."""


class RetryableError(Exception):
    def __init__(self, reason: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(reason)
        self.status = status
        self.retry_after = retry_after


def estimate_tokens(text: str, max_output_tokens: int, system_prompt: str = "") -> int:
    # Byte count is a coarse reservation. Use the provider's accounting if TPM binds.
    return len((system_prompt + text).encode("utf-8")) + max_output_tokens + 32


async def mock_complete(text: str) -> dict[str, object]:
    await asyncio.sleep(0.05)
    if text == "[mock:invalid]":
        raise RowError("invalid_output_schema")
    if text == "[mock:429]":
        raise RetryableError("http_429", status=429, retry_after=0.1)
    if text == "[mock:timeout]":
        await asyncio.sleep(60)
    return {}


class OpenAICompatible:
    def __init__(
        self, client: "httpx.AsyncClient", model: str, max_output_tokens: int, system_prompt: str,
        *, output_limit_field: str = DEFAULT_OUTPUT_LIMIT_FIELD,
    ) -> None:
        self.client = client
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.system_prompt = system_prompt
        self.output_limit_field = output_limit_field

    async def complete(self, text: str) -> str:
        import httpx

        try:
            response = await self.client.post("chat/completions", json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": text},
                ],
                self.output_limit_field: self.max_output_tokens,
            })
        except httpx.TimeoutException as error:
            raise RetryableError("http_timeout") from error
        except (httpx.NetworkError, httpx.RemoteProtocolError) as error:
            raise RetryableError("network_error") from error
        except httpx.RequestError as error:
            raise FatalProviderError(f"transport_setup_error: {type(error).__name__}") from error

        status = response.status_code
        if status in {401, 403, 404} or 300 <= status < 400:
            raise FatalProviderError(f"provider_setup_http_{status}")
        if status == 429:
            try:
                code = response.json()["error"]["code"]
            except (KeyError, TypeError, ValueError, RecursionError):
                code = None
            if isinstance(code, str) and code in QUOTA_ERROR_CODES:
                raise FatalProviderError("provider_quota_exhausted")
        if status in RETRYABLE_STATUS:
            delay = retry_after_seconds(response.headers.get("Retry-After"))
            raise RetryableError(f"http_{status}", status, delay)
        if not response.is_success:
            raise RowError(f"http_{status}")

        try:
            choice = response.json()["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") == "length":
                raise RowError("output_truncated")
            if not isinstance(message, dict):
                raise RowError("invalid_provider_response")
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise RowError("provider_refusal")
            raw = message["content"]
            if not isinstance(raw, str):
                raise RowError("invalid_provider_response")
        except (KeyError, IndexError, TypeError, ValueError, RecursionError) as error:
            raise RowError("invalid_provider_response") from error
        return raw
