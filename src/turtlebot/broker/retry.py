"""Retry with exponential backoff for transient broker errors."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from turtlebot.exceptions import BrokerConnectionError, NotPaperAccountError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Backoff:
    """Exponential backoff schedule.

    Attributes:
        initial: First delay in seconds.
        maximum: Upper bound of a single delay.
        factor: Multiplier between attempts.
    """

    initial: float = 5.0
    maximum: float = 300.0
    factor: float = 2.0

    def delays(self) -> Iterator[float]:
        """Infinite sequence of delays: initial, initial*factor, ... capped at ``maximum``."""
        delay = self.initial
        while True:
            yield min(delay, self.maximum)
            delay *= self.factor


def retry_transient[T](
    fn: Callable[[], T],
    *,
    backoff: Backoff,
    max_attempts: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    """Call ``fn`` until it succeeds, retrying :class:`BrokerConnectionError` with backoff.

    :class:`NotPaperAccountError` and all other exceptions propagate immediately.

    Args:
        fn: Operation to run.
        backoff: Delay schedule.
        max_attempts: Give up after this many attempts (``None`` = retry forever).
        sleep: Sleep function (injected for tests).
        on_retry: Called with ``(attempt, error, delay)`` before each wait.

    Returns:
        The result of ``fn``.

    Raises:
        BrokerConnectionError: If ``max_attempts`` is exhausted.
    """
    delays = backoff.delays()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except NotPaperAccountError:
            raise
        except BrokerConnectionError as exc:
            if max_attempts is not None and attempt >= max_attempts:
                raise
            delay = next(delays)
            log.warning("Attempt %d failed (%s), retrying in %.0f s", attempt, exc, delay)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
