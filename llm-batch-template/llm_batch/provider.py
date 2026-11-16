import asyncio
import json
from typing import TYPE_CHECKING

from .control import retry_after_seconds

if TYPE_CHECKING:
    import httpx


TASK_VERSION = "sentiment-v1"
SYSTEM_PROMPT = (
    'Classify the sentiment of the user text as positive, neutral, or negative. '
    'Return exactly one JSON object with a single string field named "label".'
)
LABELS = {"positive", "neutral", "negative"}
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


def validate(raw: str) -> dict[str, str]:
    try:
        result = json.loads(raw)
        if isinstance(result, dict) and set(result) == {"label"}:
            label = result["label"]
            if isinstance(label, str) and label in LABELS:
                return {"label": label}
    except (TypeError, ValueError, RecursionError):
        pass
    raise RowError("invalid_output_schema")


def estimate_tokens(text: str, max_output_tokens: int) -> int:
    # Byte count is a coarse reservation. Use the provider's accounting if TPM binds.
    return len((SYSTEM_PROMPT + text).encode("utf-8")) + max_output_tokens + 32


async def mock_complete(text: str) -> dict[str, str]:
    await asyncio.sleep(0.05)
    if text == "[mock:invalid]":
        return validate("not JSON")
    if text == "[mock:429]":
        raise RetryableError("http_429", status=429, retry_after=0.1)
    if text == "[mock:timeout]":
        await asyncio.sleep(60)
    if any(word in text.lower() for word in ("great", "很好", "喜欢")):
        return {"label": "positive"}
    if any(word in text.lower() for word in ("awful", "太差")):
        return {"label": "negative"}
    return {"label": "neutral"}


class OpenAICompatible:
    def __init__(self, client: "httpx.AsyncClient", model: str, max_output_tokens: int) -> None:
        self.client = client
        self.model = model
        self.max_output_tokens = max_output_tokens

    async def complete(self, text: str) -> dict[str, str]:
        import httpx

        try:
            response = await self.client.post("chat/completions", json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "max_completion_tokens": self.max_output_tokens,
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
            raw = message["content"]
            if choice.get("finish_reason") == "length":
                raise RowError("output_truncated")
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise RowError("provider_refusal")
            if not isinstance(raw, str):
                raise RowError("invalid_provider_response")
        except (KeyError, IndexError, TypeError, ValueError, RecursionError) as error:
            raise RowError("invalid_provider_response") from error
        return validate(raw)
