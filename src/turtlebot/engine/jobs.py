"""Daily job bookkeeping with retries, and a watchdog for blocking work."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from turtlebot.config import ScheduleSettings
from turtlebot.exceptions import JobTimeoutError
from turtlebot.notify import Notifier
from turtlebot.persistence.store import StateStore
from turtlebot.timeutil import Clock

log = logging.getLogger(__name__)

FINISHED = frozenset({"done", "skipped"})


@dataclass(slots=True)
class _Failure:
    attempts: int
    next_try: dt.datetime


class JobRunner:
    """Run named once-per-day jobs at their due time with bounded retries.

    Job status is persisted in the state store, so a restart does not repeat a finished job and
    a crashed run is caught up within the grace window.
    """

    def __init__(
        self, store: StateStore, schedule: ScheduleSettings, notifier: Notifier, clock: Clock
    ) -> None:
        self.store = store
        self.schedule = schedule
        self.notifier = notifier
        self.clock = clock
        self._failures: dict[str, _Failure] = {}

    def is_due(self, name: str, when: dt.datetime, grace_min: int | None = None) -> bool:
        """Whether job ``name`` should run now.

        A job is due once ``when`` has passed and it has not finished today. If ``grace_min`` is
        set and the window has passed, the job is recorded as skipped and not run anymore.
        """
        now = self.clock()
        if self._finished(now.date(), name) or now < when:
            return False
        if grace_min is not None and now > when + dt.timedelta(minutes=grace_min):
            log.info("Job %s missed its window, skipped", name)
            self.store.set_job(now.date(), name, "skipped")
            return False
        return True

    def _finished(self, day: dt.date, name: str) -> bool:
        record = self.store.job(day, name)
        if record is None:
            return False
        if record.status in FINISHED:
            return True
        return record.status == "failed" and record.attempts >= self.schedule.job_max_attempts

    def mark_done(self, name: str) -> None:
        """Record ``name`` as done today."""
        self.store.set_job(self.clock().date(), name, "done")

    def run(self, name: str, fn: Callable[[], None]) -> bool:
        """Run a job; errors trigger a notification and a retry after ``job_retry_min``.

        Returns:
            ``True`` if the job ran successfully.
        """
        now = self.clock()
        failure = self._failures.get(name)
        if failure is not None and now < failure.next_try:
            return False
        if failure is None:
            record = self.store.job(now.date(), name)
            previous = record.attempts if record is not None and record.status == "failed" else 0
        else:
            previous = failure.attempts
        try:
            fn()
        except Exception as exc:
            attempts = previous + 1
            reason = f"{type(exc).__name__}: {exc}"[:150]
            log.exception(
                "Job %s failed (attempt %d/%d)", name, attempts, self.schedule.job_max_attempts
            )
            if attempts >= self.schedule.job_max_attempts:
                self._failures.pop(name, None)
                self.store.set_job(now.date(), name, "failed", attempts=attempts, error=reason)
                self.notifier.notify(
                    f"Turtle: job {name} failed for good", f"{reason} - no retry today"
                )
            else:
                retry = now + dt.timedelta(minutes=self.schedule.job_retry_min)
                self._failures[name] = _Failure(attempts, retry)
                self.store.set_job(now.date(), name, "failed", attempts=attempts, error=reason)
                self.notifier.notify(
                    f"Turtle: job {name} failed",
                    f"{reason} - retry in {self.schedule.job_retry_min} min",
                )
            return False
        self._failures.pop(name, None)
        self.mark_done(name)
        return True


def run_with_watchdog[T](
    fn: Callable[[], T], *, timeout_s: float, pump: Callable[[float], None], name: str = "job"
) -> T:
    """Run slow blocking work in a thread while the caller keeps processing broker events.

    Python cannot kill a hanging thread; after the timeout it keeps running as a daemon thread
    but no longer blocks the bot.

    Args:
        fn: Work to run.
        timeout_s: Watchdog timeout.
        pump: Called repeatedly while waiting (e.g. ``gateway.sleep``) to keep TWS events flowing.
        name: Thread name for diagnostics.

    Raises:
        JobTimeoutError: If ``fn`` does not finish within ``timeout_s``.
    """
    result: list[T] = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            result.append(fn())
        except BaseException as exc:
            error.append(exc)

    worker = threading.Thread(target=target, name=name, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout_s
    while worker.is_alive():
        if time.monotonic() > deadline:
            raise JobTimeoutError(f"{name} aborted by watchdog after {timeout_s / 60:.0f} min")
        pump(min(0.5, timeout_s))
    if error:
        raise error[0]
    return result[0]
