"""Tests for the chat subprocess wrapper, the quote cache and logging."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from turtlebot.config import LogSettings
from turtlebot.dashboard.chat import ChatError, ClaudeCliChat
from turtlebot.dashboard.quotes import YahooQuotes
from turtlebot.logging_setup import configure_logging, log_order_event


def fake_cli(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def run(chat: ClaudeCliChat, session: str | None = None) -> tuple[str, str | None]:
    return asyncio.run(chat.ask("Which stop is closest?", {"positions": []}, session))


def test_chat_passes_context_and_parses_answer(tmp_path: Path) -> None:
    # The fake CLI echoes its arguments back as the answer.
    script = fake_cli(
        tmp_path,
        'printf \'{"result": "%s", "session_id": "abc"}\' '
        '"$(echo "$@" | tr -d \'"\\n\' | head -c 300)"',
    )
    answer, session = run(ClaudeCliChat(str(script), tmp_path, 5))
    assert session == "abc"
    assert "Which stop is closest?" in answer
    assert "--allowedTools Read,Grep,Glob" in answer


def test_chat_resumes_session(tmp_path: Path) -> None:
    script = fake_cli(
        tmp_path, 'case "$*" in *"--resume s-9"*) echo \'{"result":"ok"}\';; *) exit 3;; esac'
    )
    assert run(ClaudeCliChat(str(script), tmp_path, 5), session="s-9") == ("ok", "s-9")


@pytest.mark.parametrize(
    ("body", "message"),
    [("echo boom >&2; exit 1", "boom"), ("echo not-json", "format"), ("sleep 5", "too long")],
)
def test_chat_errors(tmp_path: Path, body: str, message: str) -> None:
    chat = ClaudeCliChat(str(fake_cli(tmp_path, body)), tmp_path, 0.5)
    with pytest.raises(ChatError, match=message):
        run(chat)


def test_chat_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(ChatError, match="not found"):
        run(ClaudeCliChat(str(tmp_path / "nope"), tmp_path, 5))


def test_quote_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_download(symbols: Sequence[str]) -> dict[str, float]:
        calls.append(list(symbols))
        return {s: 10.0 for s in symbols if s != "BAD"}

    monkeypatch.setattr(YahooQuotes, "_download", staticmethod(fake_download))
    quotes = YahooQuotes(ttl_s=60)
    assert quotes.quotes(["A", "BAD"]) == {"A": 10.0}
    assert quotes.quotes(["A"]) == {"A": 10.0}
    assert calls == [["A", "BAD"]]  # cached; BAD retried only with the next miss
    quotes.ttl_s = -1
    quotes.quotes(["A"])
    assert calls[-1] == ["A"]


def test_json_logs_and_order_audit(tmp_path: Path) -> None:
    configure_logging(LogSettings(level="DEBUG"), tmp_path)
    try:
        logging.getLogger("turtlebot.test").info("hello", extra={"ctx": {"symbol": "AAA"}})
        log_order_event("order.placed", "BUY 1 AAA", symbol="AAA", qty=1)
        for handler in (
            logging.getLogger().handlers + logging.getLogger("turtlebot.orders").handlers
        ):
            handler.flush()
        main = [
            json.loads(line) for line in (tmp_path / "turtlebot.jsonl").read_text().splitlines()
        ]
        orders = [json.loads(line) for line in (tmp_path / "orders.jsonl").read_text().splitlines()]
    finally:
        configure_logging(LogSettings(), None)
    assert main[0]["msg"] == "hello"
    assert main[0]["symbol"] == "AAA"
    assert orders[0]["event"] == "order.placed"
    assert orders[0]["qty"] == 1
