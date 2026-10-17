"""User notifications (macOS Notification Center or log only)."""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import Protocol

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """Channel for alerts that need the user's attention."""

    def notify(self, title: str, message: str) -> None:
        """Send an alert (must never raise)."""


class LogNotifier:
    """Notifier that only writes a WARNING log line."""

    def notify(self, title: str, message: str) -> None:  # noqa: D102 - see protocol
        log.warning("NOTIFY %s: %s", title, message)


class MacOSNotifier:
    """Notification Center via ``osascript``; text is passed as argv so quotes are harmless."""

    def notify(self, title: str, message: str) -> None:  # noqa: D102 - see protocol
        log.info("NOTIFY %s: %s", title, message)
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    "on run argv",
                    "-e",
                    "display notification (item 1 of argv) with title (item 2 of argv)",
                    "-e",
                    "end run",
                    message[:240],
                    title,
                ],
                check=True,
                timeout=10,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Notification failed: %s", exc)


def default_notifier() -> Notifier:
    """MacOS notifier on Darwin, log-only elsewhere."""
    return MacOSNotifier() if platform.system() == "Darwin" else LogNotifier()
