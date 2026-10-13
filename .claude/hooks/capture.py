#!/usr/bin/env python3
"""8x agent capture: append every prompt and final response to .agent-logs/.

Wired in .claude/settings.json to three Claude Code hook events:
  SessionStart      -> remember the session's model
  UserPromptSubmit  -> stamp the prompt (verbatim from stdin); written at Stop,
                       once the model is known
  Stop              -> append PROMPT + RESPONSE (final text of the turn, read from
                       the transcript whose path arrives on stdin)

Only the prompt and the final response are written - no thinking, tool calls or
intermediate text. Entries are append-only; the frontmatter counters are the
only thing rewritten. Per-session state lives in .git/ so it is never committed.

Also usable by hand to convert an existing transcript:
  python3 .claude/hooks/capture.py --backfill <transcript.jsonl>
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AUTHOR = "enigma49"
TOOL = "claude-code"
PROJECT = "higgsfield-rebuild"

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / ".agent-logs"
STATE_DIR = REPO / ".git" / "agent-capture"


def now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def load_state(session: str) -> dict:
    p = STATE_DIR / (session + ".json")
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_state(session: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / (session + ".json")).write_text(json.dumps(state))


def read_transcript(path: str | None) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def last_model(entries: list[dict]) -> str | None:
    for e in reversed(entries):
        m = e.get("message")
        if e.get("type") == "assistant" and isinstance(m, dict) and m.get("model"):
            if m["model"] != "<synthetic>":
                return m["model"]
    return None


def is_real_prompt(e: dict) -> bool:
    """A user entry the human typed (not a tool result or injected meta text)."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return True
    if isinstance(c, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    return False


def prompt_text(e: dict) -> str:
    c = e["message"]["content"]
    if isinstance(c, str):
        return c
    parts = []
    for b in c:
        if b.get("type") == "text":
            parts.append(b["text"])
        elif b.get("type") == "image":
            parts.append("[image attached]")
    return "\n\n".join(parts)


def final_response(entries: list[dict], start: int) -> tuple[str, str | None, str | None]:
    """Text after the last tool round-trip in the turn beginning at `start`."""
    texts: list[str] = []
    model = ts = None
    for e in entries[start + 1:]:
        if e.get("isSidechain"):
            continue
        if e.get("type") == "user":
            if is_real_prompt(e):
                break
            texts, ts = [], None  # a tool result arrived; earlier text was intermediate
            continue
        if e.get("type") != "assistant":
            continue
        m = e.get("message") or {}
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                texts, ts = [], None
            elif isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                texts.append(b["text"])
                ts = e.get("timestamp")
        if m.get("model") and m["model"] != "<synthetic>":
            model = m["model"]
    return "\n\n".join(texts), model, ts


def log_path(session: str, state: dict, first_ts: str) -> Path:
    if "file" not in state:
        dt = datetime.strptime(first_ts[:19], "%Y-%m-%dT%H:%M:%S")
        state["file"] = dt.strftime("%Y-%m-%d_%H-%M-%S") + "_" + session + ".md"
    return LOG_DIR / state["file"]


def write_entry(session: str, state: dict, kind: str, ts: str, model: str, body: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = log_path(session, state, ts)
    if kind == "PROMPT":
        state["num"] = state.get("num", 0) + 1
        state.setdefault("first", ts)
        state["last"] = ts
    num = state.get("num", 0)
    short = session[:8]
    frontmatter = (
        "---\n"
        "session_id: %s\ndate: %s\nauthor: %s\nmodel: %s\ntool: %s\nproject: %s\n"
        "total_exchanges: %d\nfirst_prompt_time: %s\nlast_prompt_time: %s\n"
        "---\n" % (session, state.get("first", ts)[:10], AUTHOR, model, TOOL, PROJECT,
                   num, state.get("first", ts), state.get("last", ts))
    )
    if path.exists():
        content = path.read_text(encoding="utf-8")
        content = re.sub(r"\A---\n.*?\n---\n", lambda _: frontmatter, content, count=1, flags=re.S)
    else:
        content = frontmatter + (
            "\n# Session Log - %s\n\nSession: `%s` | Project: `%s` | Author: `%s`\n\n---\n"
            % (state.get("first", ts)[:10], short, PROJECT, AUTHOR)
        )
    content += "\n[LOG_ENTRY type=%s num=%d session=%s]\ntimestamp: %s\nmodel: %s\n\n%s\n\n" % (
        kind, num, short, ts, model, body.rstrip())
    path.write_text(content, encoding="utf-8")


def handle(event: dict) -> None:
    name = event.get("hook_event_name")
    session = event.get("session_id") or "unknown"
    state = load_state(session)

    if name == "SessionStart":
        if event.get("model"):
            state["model"] = event["model"] if isinstance(event["model"], str) \
                else event["model"].get("id", str(event["model"]))
        save_state(session, state)
        return

    if name == "UserPromptSubmit":
        # The model is not known until the first response exists (SessionStart
        # carries none), so hold the prompt - stamped now - and write it at Stop.
        if state.get("pending"):  # previous turn never reached Stop: flush it
            p = state["pending"]
            model = state.get("model") or "unknown"
            write_entry(session, state, "PROMPT", p["ts"], model, p["text"])
            write_entry(session, state, "RESPONSE", now_iso(), model, "[turn interrupted - no response captured]")
        state["pending"] = {"ts": now_iso(), "text": event.get("prompt", "")}
        save_state(session, state)
        return

    if name == "Stop":
        if not state.get("pending"):
            return
        text, model, ts = "", None, None
        # The transcript can lag the Stop event by a moment; retry briefly.
        for _ in range(15):
            entries = read_transcript(event.get("transcript_path"))
            starts = [i for i, e in enumerate(entries) if is_real_prompt(e)]
            if starts:
                text, model, ts = final_response(entries, starts[-1])
            if text:
                break
            time.sleep(0.3)
        if not text and isinstance(event.get("last_assistant_message"), str):
            text = event["last_assistant_message"]
        model = model or state.get("model") or "unknown"
        state["model"] = model
        p = state.pop("pending")
        write_entry(session, state, "PROMPT", p["ts"], model, p["text"])
        write_entry(session, state, "RESPONSE", ts or now_iso(), model, text or "[no final text response]")
        save_state(session, state)


def backfill(transcript: str) -> None:
    entries = read_transcript(transcript)
    session = Path(transcript).stem
    state: dict = {}
    for i, e in enumerate(entries):
        if not is_real_prompt(e):
            continue
        text, model, ts = final_response(entries, i)
        model = model or "unknown"
        write_entry(session, state, "PROMPT", e["timestamp"], model, prompt_text(e))
        write_entry(session, state, "RESPONSE", ts or e["timestamp"], model, text or "[no final text response]")
    print(LOG_DIR / state["file"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--backfill":
        backfill(sys.argv[2])
        sys.exit(0)
    try:
        handle(json.load(sys.stdin))
    except Exception as exc:  # never block the agent; leave a trace instead
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "errors.log", "a") as f:
            f.write("%s %r\n" % (now_iso(), exc))
    sys.exit(0)
