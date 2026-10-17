"""Chat assistant backed by the local Claude Code CLI.

Each question is answered by ``claude -p`` (the user's own Claude subscription, no API billing).
The backend prepends a compact JSON snapshot of the bot's current state, so the assistant can
answer most questions without reading files; for code or log questions it may use read-only
tools (``Read``, ``Grep``, ``Glob``) inside the repository.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Protocol

from turtlebot.exceptions import TurtleBotError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the assistant inside a personal trading dashboard. The user runs a long-only "
    "Turtle/Donchian breakout bot (20-day high breakout with volume confirmation, stop at the "
    "10-day low, trailed upwards) on an Interactive Brokers PAPER account, split into block 1 "
    "(base strategy) and block 2 (same strategy, only breakout strength >= 10 %). "
    "Each question starts with a JSON snapshot of the current state - prefer it over reading "
    "files. The SQLite state lives in var/turtlebot.db, the source code in src/turtlebot. "
    "Never place orders or suggest commands that change state. This is not investment advice; "
    "help the user understand their own strategy and data. Answer concisely in the language "
    "of the question."
)


class ChatError(TurtleBotError):
    """The chat backend failed."""


class ChatService(Protocol):
    """Answers questions about the bot."""

    async def ask(
        self, message: str, context: dict[str, object], session_id: str | None
    ) -> tuple[str, str | None]:
        """Return ``(answer, session_id)``; pass the session id back to continue a conversation."""


class ClaudeCliChat:
    """Chat via ``claude -p`` (non-interactive, read-only tools).

    Args:
        command: Executable name or path of the Claude Code CLI.
        workdir: Directory the CLI may read (the repository root).
        timeout_s: Maximum answer time.
    """

    def __init__(self, command: str, workdir: Path, timeout_s: float) -> None:
        self.command = command
        self.workdir = workdir
        self.timeout_s = timeout_s

    async def ask(  # noqa: D102 - see protocol
        self, message: str, context: dict[str, object], session_id: str | None
    ) -> tuple[str, str | None]:
        prompt = (
            "Current bot state (JSON):\n"
            + json.dumps(context, default=str, separators=(",", ":"))
            + "\n\nQuestion:\n"
            + message
        )
        argv = [
            self.command,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read,Grep,Glob",
            "--append-system-prompt",
            SYSTEM_PROMPT,
        ]
        if session_id:
            argv += ["--resume", session_id]
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # use the subscription, never API billing by accident
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ChatError(f"'{self.command}' not found - is Claude Code installed?") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except TimeoutError as exc:
            proc.kill()
            await proc.communicate()  # reap the process and close its pipes
            raise ChatError("the assistant took too long to answer") from exc
        if proc.returncode != 0:
            raise ChatError(stderr.decode(errors="replace").strip()[:300] or "claude CLI failed")
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ChatError("unexpected answer format from claude CLI") from exc
        return str(data.get("result", "")), data.get("session_id", session_id)


class UnavailableChat:
    """Placeholder when the chat is disabled."""

    async def ask(  # noqa: D102 - see protocol
        self, message: str, context: dict[str, object], session_id: str | None
    ) -> tuple[str, str | None]:
        raise ChatError("chat is disabled (TURTLE_DASHBOARD__CHAT_ENABLED=false)")
