import asyncio
import logging
import math
import random
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic

from .control import RateLimiter
from .provider import RetryableError, RowError, estimate_tokens
from .store import AsyncStore, Job


log = logging.getLogger(__name__)
ModelCall = Callable[[str], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class Settings:
    workers: int
    queue_size: int
    concurrency: int
    rpm: int
    tpm: int
    max_output_tokens: int
    max_attempts: int
    request_timeout: float
    job_timeout: float
    progress_interval: float
    retry_failed: bool


@dataclass
class Progress:
    started: float = field(default_factory=monotonic)
    succeeded: int = 0
    failed: int = 0
    attempts: int = 0
    retries: int = 0
    rate_limited: int = 0
    request_timeouts: int = 0
    job_timeouts: int = 0
    in_flight: int = 0
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=1024))

    def report(self, queue: asyncio.Queue, target: int) -> None:
        sample = sorted(self.latencies)
        p95 = sample[math.ceil(len(sample) * 0.95) - 1] if sample else 0.0
        log.info(
            "committed=%d/%d ok=%d failed=%d queue=%d/%d in_flight=%d "
            "attempts=%d retries=%d http_429=%d request_timeouts=%d job_timeouts=%d "
            "goodput=%.2f rows/s request_p95=%.3fs",
            self.succeeded + self.failed, target, self.succeeded, self.failed,
            queue.qsize(), queue.maxsize, self.in_flight, self.attempts, self.retries,
            self.rate_limited, self.request_timeouts, self.job_timeouts,
            self.succeeded / max(monotonic() - self.started, 0.001), p95,
        )


async def process(
    job: Job, complete: ModelCall, settings: Settings,
    limiter: RateLimiter, slots: asyncio.Semaphore, progress: Progress,
    system_prompt: str,
) -> tuple[dict[str, object] | None, str | None, int]:
    tokens = estimate_tokens(job.text, settings.max_output_tokens, system_prompt) if settings.tpm else 0
    if settings.tpm and tokens > settings.tpm:
        return None, "token_estimate_exceeds_tpm", 0

    attempts = 0
    try:
        async with asyncio.timeout(settings.job_timeout):
            while True:
                try:
                    while True:
                        async with slots:
                            delay = limiter.admit_or_delay(tokens)
                            if delay <= 0:
                                attempts += 1
                                progress.attempts += 1
                                progress.retries += attempts > 1
                                result = await request(complete, job.text, settings.request_timeout, progress)
                                return result, None, attempts
                        await asyncio.sleep(delay)
                except RetryableError as error:
                    progress.rate_limited += error.status == 429
                    progress.request_timeouts += str(error) in {"http_timeout", "request_deadline"}
                    backoff = random.uniform(0.0, 0.5 * 2 ** min(attempts - 1, 6))
                    delay = max(error.retry_after or 0.0, backoff)
                    if error.status == 429 or error.retry_after is not None:
                        limiter.cooldown(delay)
                    if attempts >= settings.max_attempts:
                        return None, str(error), attempts
                    await asyncio.sleep(delay)
    except TimeoutError:
        progress.job_timeouts += 1
        return None, "job_deadline", attempts
    except RowError as error:
        return None, str(error), attempts


async def request(complete: ModelCall, text: str, timeout: float, progress: Progress) -> dict[str, object]:
    progress.in_flight += 1
    started = monotonic()
    try:
        async with asyncio.timeout(timeout):
            return await complete(text)
    except TimeoutError as error:
        raise RetryableError("request_deadline") from error
    finally:
        progress.in_flight -= 1
        progress.latencies.append(monotonic() - started)


async def run(
    database: AsyncStore, complete: ModelCall, settings: Settings, *, system_prompt: str = "",
) -> dict[str, int]:
    store = database.store
    initial = await database.call(store.counts)
    target = initial["pending"] + (initial["failed"] if settings.retry_failed else 0)
    log.info("start pending=%d succeeded=%d failed=%d scheduled=%d",
             initial["pending"], initial["succeeded"], initial["failed"], target)
    queue: asyncio.Queue[Job | None] = asyncio.Queue(maxsize=settings.queue_size)
    slots = asyncio.Semaphore(settings.concurrency)
    limiter = RateLimiter(settings.rpm, settings.tpm)
    progress = Progress()
    finished = asyncio.Event()

    async def worker() -> None:
        while True:
            job = await queue.get()
            try:
                if job is None:
                    return
                result, error, attempts = await process(job, complete, settings, limiter, slots, progress, system_prompt)
                await database.call(store.finish, job, result, error, attempts)
                if error:
                    progress.failed += 1
                else:
                    progress.succeeded += 1
            finally:
                queue.task_done()

    async def reporter() -> None:
        try:
            while not finished.is_set():
                try:
                    await asyncio.wait_for(finished.wait(), timeout=settings.progress_interval)
                except TimeoutError:
                    progress.report(queue, target)
        finally:
            progress.report(queue, target)

    async with asyncio.TaskGroup() as group:
        for _ in range(settings.workers):
            group.create_task(worker())
        group.create_task(reporter())

        after = 0
        while jobs := await database.call(store.fetch, after, min(256, settings.queue_size), settings.retry_failed):
            for job in jobs:
                await queue.put(job)
            after = jobs[-1].seq

        await queue.join()
        for _ in range(settings.workers):
            await queue.put(None)
        finished.set()

    return await database.call(store.counts)
