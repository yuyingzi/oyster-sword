import argparse
import asyncio
import logging
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from .connection import CHAT_COMPLETIONS, DEFAULT_OUTPUT_LIMIT_FIELD, Connection, connection_from_env, load_connection
from .pipeline import Settings, run
from .provider import FatalProviderError, OpenAICompatible, mock_complete
from .store import AsyncStore, Store, export_results, lock_run
from .task import Task, load_task


log = logging.getLogger(__name__)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be an integer above 0")
    return number


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number above 0")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch-process JSONL text through an LLM")
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("run", help="start or resume a batch")
    batch.add_argument("--input", type=Path, required=True, help="JSONL with string id and text fields")
    batch.add_argument("--db", type=Path, required=True, help="SQLite checkpoint path")
    connection = batch.add_mutually_exclusive_group()
    connection.add_argument("--provider", choices=("mock", "openai-compatible"), default="mock")
    connection.add_argument("--llm", type=Path, help="TOML connection for a real LLM endpoint")
    batch.add_argument("--task", type=Path, default=Path("task.json"), help="task JSON for the API adapter")
    batch.add_argument("--workers", type=positive_int, default=8)
    batch.add_argument("--queue-size", type=positive_int, default=64)
    batch.add_argument("--concurrency", type=positive_int, default=4, help="maximum in-flight requests")
    batch.add_argument("--rpm", type=positive_int, default=60, help="paced request starts per minute")
    batch.add_argument("--tpm", type=int, default=0, help="estimated tokens per 60 seconds, 0 disables")
    batch.add_argument("--max-output-tokens", type=positive_int, default=128)
    batch.add_argument("--max-attempts", type=positive_int, default=3, help="includes the first request")
    batch.add_argument("--request-timeout", type=positive_seconds, default=45.0, metavar="SECONDS")
    batch.add_argument("--job-timeout", type=positive_seconds, default=180.0, metavar="SECONDS")
    batch.add_argument("--progress-interval", type=positive_seconds, default=5.0, metavar="SECONDS")
    batch.add_argument("--retry-failed", action="store_true", help="include previously failed rows")
    export = commands.add_parser("export", help="export committed outcomes in input order")
    export.add_argument("--db", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True, help="new JSONL file")
    args = parser.parse_args()
    if args.command == "run" and args.tpm < 0:
        parser.error("--tpm must be 0 or positive")
    return args


async def run_provider(
    database: AsyncStore, settings: Settings, task: Task, connection: Connection, key: str,
) -> dict[str, int]:
    import httpx

    limits = httpx.Limits(max_connections=settings.concurrency, max_keepalive_connections=settings.concurrency)
    timeouts = httpx.Timeout(settings.request_timeout, connect=min(10.0, settings.request_timeout))
    async with httpx.AsyncClient(
        base_url=connection.base_url + "/", headers={"Authorization": f"Bearer {key}"}, limits=limits, timeout=timeouts,
    ) as client:
        adapter = OpenAICompatible(
            client, connection.model, settings.max_output_tokens, task.system_prompt,
            output_limit_field=connection.output_limit_field,
        )

        async def complete(text: str) -> dict[str, object]:
            return task.decode_result(await adapter.complete(text))

        return await run(database, complete, settings, system_prompt=task.system_prompt)


def start(args: argparse.Namespace) -> int:
    source, db_path = args.input.resolve(), args.db.resolve()
    if source == db_path or source == db_path.with_name(db_path.name + ".lock"):
        raise ValueError("input and checkpoint paths must be distinct")
    connection = None
    task = None
    key = ""
    if args.llm is not None or args.provider == "openai-compatible":
        if args.llm is not None:
            connection = load_connection(args.llm)
        task = load_task(args.task)
        try:
            import httpx  # noqa: F401
        except ModuleNotFoundError as error:
            raise ValueError("install the API adapter dependency with: python -m pip install -r requirements.txt") from error
        connection = connection or connection_from_env()
        key = connection.api_key()

    settings = Settings(**{name: getattr(args, name) for name in Settings.__annotations__})
    signature = {
        "task": task.definition if task else None,
        "provider": "openai-compatible" if connection else "mock",
        "endpoint": connection.base_url if connection else "",
        "model": connection.model if connection else "",
        "max_output_tokens": settings.max_output_tokens,
    }
    legacy_task = None
    if connection:
        if connection.protocol == CHAT_COMPLETIONS and connection.output_limit_field == DEFAULT_OUTPUT_LIMIT_FIELD:
            legacy_task = signature.copy()
        signature.update(protocol=connection.protocol, output_limit_field=connection.output_limit_field)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_run(db_path), closing(sqlite3.connect(db_path, check_same_thread=False)) as db_connection:
        store = Store(db_connection)
        log.info("loading or verifying input for %s", db_path)
        store.initialize(source, signature, legacy_task=legacy_task)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite") as executor:
            database = AsyncStore(store, executor)
            try:
                if task is not None and connection is not None:
                    counts = asyncio.run(run_provider(database, settings, task, connection, key))
                else:
                    counts = asyncio.run(run(database, mock_complete, settings))
            except* FatalProviderError as group:
                raise FatalProviderError(str(group.exceptions[0])) from None
    log.info("done pending=%d succeeded=%d failed=%d", counts["pending"], counts["succeeded"], counts["failed"])
    return 2 if counts["failed"] else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args()
    try:
        if args.command == "export":
            export_results(args.db.resolve(), args.output.absolute())
            return 0
        return start(args)
    except KeyboardInterrupt:
        log.info("interrupted; committed outcomes are available for resume")
        return 130
    except (ValueError, OSError, sqlite3.Error, FatalProviderError) as error:
        log.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
