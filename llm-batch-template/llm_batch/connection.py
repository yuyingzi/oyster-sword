import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


CHAT_COMPLETIONS = "chat-completions"
DEFAULT_OUTPUT_LIMIT_FIELD = "max_completion_tokens"


@dataclass(frozen=True)
class Connection:
    protocol: str
    base_url: str
    model: str
    api_key_env: str
    output_limit_field: str

    def __post_init__(self) -> None:
        if self.protocol != CHAT_COMPLETIONS:
            raise ValueError("protocol must be chat-completions")
        url = urlsplit(self.base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or url.password is not None or "?" in self.base_url or "#" in self.base_url):
            raise ValueError("base_url must be an HTTP(S) API root without credentials, query or fragment")
        if url.path.endswith("/chat/completions"):
            raise ValueError("base_url must exclude /chat/completions; the adapter appends it")
        if not self.model:
            raise ValueError("model must be set")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must be an environment variable name")
        if self.output_limit_field not in {DEFAULT_OUTPUT_LIMIT_FIELD, "max_tokens"}:
            raise ValueError("output_limit_field must be max_completion_tokens or max_tokens")

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ValueError(f"set {self.api_key_env} for the API adapter")
        return key


def load_connection(path: Path) -> Connection:
    with path.open("rb") as stream:
        try:
            values = tomllib.load(stream)
        except (UnicodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"{path}: invalid connection TOML") from error

    fields = {"protocol", "base_url", "model", "api_key_env", "output_limit_field"}
    if values.keys() != fields or any(not isinstance(value, str) for value in values.values()):
        raise ValueError(f"{path}: expected string fields {', '.join(sorted(fields))}")
    values = {key: value.strip() for key, value in values.items()}
    values["base_url"] = values["base_url"].rstrip("/")
    try:
        return Connection(**values)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error


def connection_from_env() -> Connection:
    return Connection(
        protocol=CHAT_COMPLETIONS,
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
        model=os.environ.get("LLM_MODEL", "").strip(),
        api_key_env="LLM_API_KEY",
        output_limit_field=DEFAULT_OUTPUT_LIMIT_FIELD,
    )
