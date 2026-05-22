"""
Parser: Claude Code → canonical schema.

Reads Claude Code session JSONL files (~/.claude/projects/<project>/<session-id>.jsonl)
and maps into agentlog's canonical tables.

Claude Code stores sessions as JSONL where each line is a SessionStoreEntry — 
a stream event following the Anthropic Agent SDK message types:
  - "system": session init (model, tools, session_id)
  - "user": user message with content blocks
  - "assistant": assistant response with content blocks (text, tool_use)
  - "result": session completion (usage stats, stop reason)

Content blocks within messages follow the Anthropic Messages API:
  - {type: "text", text: "..."}
  - {type: "tool_use", id: "toolu_...", name: "Bash", input: {...}}
  - {type: "tool_result", tool_use_id: "toolu_...", content: "..."}

The parser supports scanning a directory of JSONL files, or a single file.
"""

import json
import sqlite3
import hashlib
import os
import glob
from datetime import datetime, timezone


def _hash(s: str) -> str | None:
    return hashlib.sha256(s.encode()).hexdigest()[:16] if s else None


def _truncate(s: str, n: int = 2000) -> str | None:
    return s[:n] if s else None


def _extract_text(content_blocks: list) -> str:
    """Extract the concatenated text from all text blocks in a content array."""
    parts = []
    for block in content_blocks or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts) if parts else ""


def _extract_tool_calls(content_blocks: list) -> list[dict]:
    """Extract tool_use blocks from a content array, returning [{id, name, arguments}]."""
    result = []
    for block in content_blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            inp = block.get("input", {})
            args = json.dumps(inp) if isinstance(inp, dict) else str(inp) if inp else ""
            result.append({
                "id": block.get("id", ""),
                "name": block.get("name", "unknown"),
                "arguments": args,
            })
    return result


def _make_session_id(project: str, file_session_id: str) -> str:
    """Create a stable session ID from project path + session ID."""
    return f"cc:{project}:{file_session_id}"


def _parse_timestamp(ts) -> float | None:
    """Parse a timestamp from various formats to Unix epoch float."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            # ISO format
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            try:
                return float(ts)
            except (ValueError, TypeError):
                pass
    return None


def ingest(source: str, target_db: str) -> dict:
    """
    Ingest Claude Code JSONL session files into agentlog's canonical schema.

    Args:
        source: Path to a JSONL file, or a directory of JSONL files
                (e.g., ~/.claude/projects/ or a single session file)
        target_db: Path to agentlog SQLite database (created if missing)

    Returns:
        dict with counts: {sessions, messages, tool_calls, skipped, files_processed}
    """
    from agentlog import FULL_SCHEMA

    # Find JSONL files
    if os.path.isfile(source):
        jsonl_files = [source]
    elif os.path.isdir(source):
        jsonl_files = sorted(glob.glob(os.path.join(source, "**/*.jsonl"),
                                       recursive=True))
    else:
        raise FileNotFoundError(f"Source not found: {source}")

    dst = sqlite3.connect(target_db)
    dst.executescript(FULL_SCHEMA)

    stats = {
        "sessions": 0,
        "messages": 0,
        "tool_calls": 0,
        "skipped": 0,
        "files_processed": 0,
    }

    for jsonl_path in jsonl_files:
        # Derive project key from path (directory name containing the session file)
        parent = os.path.basename(os.path.dirname(jsonl_path))
        file_session_id = os.path.splitext(os.path.basename(jsonl_path))[0]

        try:
            with open(jsonl_path, "r") as f:
                lines = f.readlines()
        except (OSError, IOError):
            continue

        if not lines:
            continue

        stats["files_processed"] += 1

        # --- Parse JSONL into structured events ---
        events = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not events:
            continue

        # --- Extract metadata from system/result events ---
        model = None
        session_started = None
        session_ended = None
        input_tokens = 0
        output_tokens = 0
        reasoning_tokens = 0

        for ev in events:
            if not isinstance(ev, dict):
                continue
            t = ev.get("type", "")
            # Model from system or result events
            if t == "system":
                model = _extract_model(ev)
                if not session_started:
                    session_started = _parse_timestamp(ev.get("timestamp"))
            elif t == "result":
                if not model:
                    model = _extract_model(ev)
                usage = ev.get("usage", {})
                if isinstance(usage, dict):
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    reasoning_tokens = usage.get("reasoning_tokens", 0)
            # Extract timestamp from any event
            ts = _parse_timestamp(ev.get("timestamp"))
            if ts and not session_started:
                session_started = ts
            if ts:
                session_ended = max(session_ended or 0, ts)

        # Fallback: derive session_id from file path
        session_id = _make_session_id(parent, file_session_id)

        # Try to get real session_id from events
        for ev in events:
            if isinstance(ev, dict):
                sid = ev.get("session_id")
                if sid:
                    session_id = _make_session_id(parent, sid)
                    break

        # Skip if already ingested
        existing = dst.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if existing:
            stats["skipped"] += 1
            continue

        # --- Collect and classify all messages ---
        # Build a typed timeline. Each entry: {role, content, tools, text, timestamp}
        timeline = []
        total_tool_calls = 0

        for ev in events:
            if not isinstance(ev, dict):
                continue
            t = ev.get("type", "")
            ts = _parse_timestamp(ev.get("timestamp"))

            if t == "user":
                msg = ev.get("message", ev)
                content = msg.get("content", [])
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]

                # Split: text blocks → user text message, tool_result blocks → tool messages
                text_blocks = []
                for block in (content if isinstance(content, list) else [content]):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tr_text = block.get("content", "")
                        if isinstance(tr_text, (dict, list)):
                            tr_text = json.dumps(tr_text)
                        timeline.append({
                            "role": "tool",
                            "content": str(tr_text),
                            "tools": [],
                            "text": _truncate(str(tr_text)),
                            "tool_name": "tool_result",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "tool_status": "success",
                            "timestamp": ts,
                        })
                    else:
                        text_blocks.append(block)

                if text_blocks:
                    text = _extract_text(text_blocks)
                    if text.strip():
                        timeline.append({
                            "role": "user",
                            "content": text_blocks,
                            "tools": [],
                            "text": text,
                            "tool_name": None,
                            "tool_call_id": None,
                            "tool_status": None,
                            "timestamp": ts,
                        })

            elif t == "assistant":
                msg = ev.get("message", ev)
                content = msg.get("content", [])
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                tools = _extract_tool_calls(content)
                total_tool_calls += len(tools)
                text = _extract_text(content)

                # One timeline entry per assistant turn
                main_tool = tools[0]["name"] if tools else None
                main_tool_id = tools[0]["id"] if tools else None
                tool_calls_json = json.dumps(tools) if tools else None

                if text.strip() or tools:
                    timeline.append({
                        "role": "assistant",
                        "content": content,
                        "tools": tools,
                        "text": text,
                        "tool_name": main_tool,
                        "tool_call_id": main_tool_id,
                        "tool_calls_json": tool_calls_json,
                        "tool_status": "success" if tools else None,
                        "timestamp": ts,
                    })

        if not timeline:
            # No usable content — skip this file
            continue

        # --- Insert session ---
        msg_count = len(timeline)

        dst.execute(
            """INSERT INTO sessions
            (id, agent, source, model, model_config, started_at, ended_at,
             message_count, tool_call_count, input_tokens, output_tokens,
             reasoning_tokens, estimated_cost_usd)
            VALUES (?, 'claude-code', 'cli', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, model, None,
             session_started or 0, session_ended,
             msg_count, total_tool_calls,
             input_tokens, output_tokens,
             reasoning_tokens, 0.0)
        )
        stats["sessions"] += 1

        # --- Insert messages in chronological order ---
        timeline.sort(key=lambda m: m.get("timestamp") or float("inf"))

        for seq, m in enumerate(timeline):
            dst.execute(
                """INSERT INTO messages
                (session_id, seq, role, content, content_hash,
                 tool_calls_json, tool_name, tool_call_id,
                 tool_status, token_count, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, seq, m["role"],
                 _truncate(m.get("text", "")),
                 _hash(m.get("text", "") or ""),
                 m.get("tool_calls_json"),
                 m.get("tool_name"),
                 m.get("tool_call_id"),
                 m.get("tool_status"),
                 None,
                 m.get("timestamp") or 0)
            )
            stats["messages"] += 1

        stats["tool_calls"] += total_tool_calls

    dst.commit()
    dst.close()
    return stats


def _extract_model(ev: dict) -> str | None:
    """Extract model name from a system or result event."""
    # Check message.model first
    msg = ev.get("message", {})
    if isinstance(msg, dict):
        model = msg.get("model")
        if model:
            return model
    # Check top-level model
    model = ev.get("model")
    if model:
        return model
    # Check options
    opts = ev.get("options", {})
    if isinstance(opts, dict):
        model = opts.get("model")
        if model:
            return model
    return None


def get_latest_timestamp(db_path: str) -> float | None:
    """Get the timestamp of the most recent message already ingested."""
    db = sqlite3.connect(db_path)
    result = db.execute("SELECT MAX(timestamp) FROM messages").fetchone()
    db.close()
    return result[0] if result else None
