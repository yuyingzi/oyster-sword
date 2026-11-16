from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic


class RateLimiter:
    """Pace request starts and reserve estimated tokens in a 60-second window."""

    def __init__(self, rpm: int, tpm: int) -> None:
        self.spacing = 60 / rpm
        self.tpm = tpm
        self.next_start = 0.0
        self.cooldown_until = 0.0
        self.reservations: deque[tuple[float, int]] = deque()
        self.reserved = 0

    def cooldown(self, seconds: float) -> None:
        self.cooldown_until = max(self.cooldown_until, monotonic() + seconds)

    def admit_or_delay(self, tokens: int) -> float:
        """Reserve all budgets together, or return the delay before another check."""
        now = monotonic()
        delay = max(self.next_start, self.cooldown_until) - now
        if delay > 0:
            return delay

        if self.tpm:
            while self.reservations and self.reservations[0][0] <= now - 60:
                _, expired = self.reservations.popleft()
                self.reserved -= expired
            excess = self.reserved + tokens - self.tpm
            if excess > 0:
                for started, reserved in self.reservations:
                    excess -= reserved
                    if excess <= 0:
                        return (started + 60) - now
                raise ValueError("one request exceeds the TPM budget")
            self.reservations.append((now, tokens))
            self.reserved += tokens

        self.next_start = now + self.spacing
        return 0.0


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = int(value)
        if seconds >= 0:
            return float(seconds)
        return None
    except (ValueError, OverflowError):
        pass
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
