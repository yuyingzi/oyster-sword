import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class Job:
    seq: int
    text: str


@contextmanager
def lock_run(db_path: Path) -> Iterator[None]:
    lock_path = db_path.with_name(db_path.name + ".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"another run owns {db_path}") from None
        yield


class Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialize(
        self, source: Path, task: dict[str, object], *, legacy_task: dict[str, object] | None = None,
    ) -> None:
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS run_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                signature TEXT NOT NULL,
                input_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                seq INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                CHECK (
                    (status = 'pending' AND result IS NULL AND error IS NULL) OR
                    (status = 'succeeded' AND result IS NOT NULL AND error IS NULL) OR
                    (status = 'failed' AND result IS NULL AND error IS NOT NULL)
                )
            );
        """)
        signature = json.dumps(task, sort_keys=True)
        existing = self.connection.execute(
            "SELECT signature, input_sha256 FROM run_metadata WHERE singleton=1"
        ).fetchone()
        if existing:
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            old_signature, old_digest = existing
            accepted = {signature}
            if legacy_task is not None:
                accepted.add(json.dumps(legacy_task, sort_keys=True))
            if old_digest != digest or old_signature not in accepted:
                raise ValueError("input or task settings changed; choose a new --db")
            return

        digest = hashlib.sha256()
        with source.open("rb") as stream, self.connection:
            for line_number, raw in enumerate(stream, start=1):
                digest.update(raw)
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                    job_id, text = row["id"], row["text"]
                    if not isinstance(job_id, str) or not job_id.strip() or not isinstance(text, str):
                        raise ValueError
                    job_id.encode("utf-8")
                    text.encode("utf-8")
                except (KeyError, TypeError, ValueError, RecursionError):
                    raise ValueError(f"input line {line_number}: expected string id and text") from None
                try:
                    self.connection.execute("INSERT INTO jobs(id, text) VALUES (?, ?)", (job_id, text))
                except sqlite3.IntegrityError:
                    raise ValueError(f"input line {line_number}: duplicate id") from None
            self.connection.execute(
                "INSERT INTO run_metadata VALUES (1, ?, ?)", (signature, digest.hexdigest())
            )

    def fetch(self, after: int, limit: int, retry_failed: bool) -> list[Job]:
        second_status = "failed" if retry_failed else "pending"
        rows = self.connection.execute(
            "SELECT seq, text FROM jobs WHERE seq > ? AND status IN ('pending', ?) ORDER BY seq LIMIT ?",
            (after, second_status, limit),
        )
        return [Job(seq, text) for seq, text in rows]

    def finish(self, job: Job, result: dict[str, object] | None, error: str | None, attempts: int) -> None:
        payload = json.dumps(result, ensure_ascii=False) if result is not None else None
        status = "succeeded" if result is not None else "failed"
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET status=?, result=?, error=?, attempts=attempts+? WHERE seq=?",
                (status, payload, error, attempts, job.seq),
            )

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("pending", "succeeded", "failed"), 0)
        counts.update(self.connection.execute("SELECT status, count(*) FROM jobs GROUP BY status"))
        return counts


class AsyncStore:
    """Serialize database operations on one thread while HTTP requests can advance."""

    def __init__(self, store: Store, executor: ThreadPoolExecutor) -> None:
        self.store = store
        self.executor = executor

    async def call(self, method: Callable[..., T], *args: object) -> T:
        return await asyncio.get_running_loop().run_in_executor(self.executor, method, *args)


def export_results(db_path: Path, output: Path) -> None:
    # Publish only a complete export, and never replace an existing path.
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    rows = "SELECT id, status, result, error, attempts FROM jobs WHERE status != 'pending' ORDER BY seq"
    temporary = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False)
    try:
        with temporary, closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as connection:
            for job_id, status, result, error, attempts in connection.execute(rows):
                record = {
                    "id": job_id, "status": status, "result": json.loads(result) if result else None,
                    "error": error, "attempts": attempts,
                }
                temporary.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.link(temporary.name, output)
    finally:
        Path(temporary.name).unlink(missing_ok=True)
