# claude_chat.py
"""Chat-Anbindung ueber die lokale Claude-Code-CLI (laeuft mit deinem Claude-Abo,
keine separate API-Abrechnung). Ruft `claude -p` non-interaktiv auf, read-only
beschraenkt auf die Bot-Dateien im Arbeitsverzeichnis."""

import json
import os
import subprocess
from pathlib import Path

BOT_DIR = Path(__file__).parent.parent

SYSTEM_PROMPT = (
    "Du bist ein Assistent in einem persoenlichen Trading-Dashboard. Der Nutzer betreibt einen "
    "Turtle-Breakout-Trading-Bot (long-only, Donchian-Breakout mit Volumenbestaetigung) auf einem "
    "IBKR-Paper-Trading-Konto, aufgeteilt in Block 1 (Basis-Strategie) und Block 2 (gleiche Strategie, "
    "zusaetzlich gefiltert auf Mindest-Breakout-Strength). "
    "Fuer Fragen zu Positionen nutze live_turtle_state.json (aktuelle offene Positionen, Quelle der "
    "Wahrheit, kleine Datei). Fuer Fragen zu Signalen/Trades/Historie nutze live_turtle_journal.csv. "
    "live_trade_turtle.log existiert auch, ist aber sehr gross (~1 MB) -- lies es NUR, wenn der Nutzer "
    "explizit nach technischen Logs oder Fehlern fragt, und dann gezielt mit grep/tail-artigen Ausschnitten, "
    "nie die ganze Datei. Lies die relevanten Dateien selbst, bevor du antwortest -- rate nichts. Antworte "
    "knapp und konkret auf Deutsch. Du loest keine echten Order aus und gibst keine Anlageberatung im "
    "rechtlichen Sinne, sondern hilfst beim Verstehen der eigenen Strategie und Daten."
)


class ClaudeChatError(RuntimeError):
    pass


def ask_claude(prompt: str, session_id: str | None = None) -> tuple[str, str | None]:
    """Fragt Claude Code (CLI, non-interaktiv). Gibt (Antworttext, session_id) zurueck.
    session_id aus dem vorherigen Aufruf weiterreichen, um das Gespraech fortzusetzen."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # sicherstellen, dass das Abo genutzt wird, keine API-Abrechnung

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        # "dontAsk" ist kein gueltiger --permission-mode-Wert der Claude-Code-CLI (gueltig: default,
        # acceptEdits, bypassPermissions, plan) -- damit schlug jeder Chat-Aufruf fehl oder haengte bis
        # zum Timeout. --allowedTools reicht im non-interaktiven -p-Modus allein aus, um genau diese
        # drei Lesewerkzeuge ohne Rueckfrage zu erlauben (Fix 17.10.2026).
        "--allowedTools", "Read,Grep,Glob",
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    if session_id:
        cmd += ["--resume", session_id]

    try:
        result = subprocess.run(
            cmd, cwd=BOT_DIR, env=env, capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError:
        raise ClaudeChatError(
            "Der Befehl 'claude' wurde nicht gefunden. Ist Claude Code auf diesem Mac installiert und im PATH?"
        )
    except subprocess.TimeoutExpired:
        raise ClaudeChatError("Zeitüberschreitung -- die Anfrage hat zu lange gedauert.")

    if result.returncode != 0:
        raise ClaudeChatError(result.stderr.strip() or f"claude-Befehl fehlgeschlagen (Exit {result.returncode})")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ClaudeChatError(f"Unerwartete Antwort: {result.stdout[:300]}")

    answer = data.get("result", "")
    new_session_id = data.get("session_id", session_id)
    return answer, new_session_id
