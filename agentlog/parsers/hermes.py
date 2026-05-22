"""
Parser: Hermes Agent → canonical schema.

Reads Hermes state.db (SQLite) and maps into agentlog's canonical tables.
Handles tool_calls JSON extraction, cost estimation, and session deduplication.
"""

import json
import sqlite3
import hashlib
import os
from datetime import datetime, timezone

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16] if s else None

def _truncate(s: str, n: int = 2000) -> str:
    return s[:n] if s else None

def _parse_tool_calls(tool_calls_json: str | None) -> list[dict]:
    """Parse Hermes tool_calls JSON into list of {name, args, id}."""
    if not tool_calls_json:
        return []
    try:
        raw = json.loads(tool_calls_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    result = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {})
        result.append({
            "id": tc.get("call_id", tc.get("id", "")),
            "name": fn.get("name", "unknown"),
            "arguments": fn.get("arguments", ""),
        })
    return result

def ingest(source_db: str, target_db: str) -> dict:
    """
    Ingest Hermes state.db into agentlog's canonical schema.

    Args:
        source_db: Path to Hermes state.db
        target_db: Path to agentlog SQLite database (created if missing)

    Returns:
        dict with counts: {sessions, messages, tool_calls, skipped}
    """
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    dst = sqlite3.connect(target_db)
    from agentlog import FULL_SCHEMA
    dst.executescript(FULL_SCHEMA)

    stats = {"sessions": 0, "messages": 0, "tool_calls": 0, "skipped": 0}

    # Ingest sessions
    sessions = src.execute(
        "SELECT id, source, model, model_config, system_prompt, "
        "started_at, ended_at, message_count, tool_call_count, "
        "input_tokens, output_tokens, reasoning_tokens, "
        "billing_provider, estimated_cost_usd, title, parent_session_id "
        "FROM sessions ORDER BY started_at"
    ).fetchall()

    for s in sessions:
        # Skip if already ingested
        existing = dst.execute("SELECT 1 FROM sessions WHERE id = ?", (s["id"],)).fetchone()
        if existing:
            stats["skipped"] += 1
            continue

        dst.execute(
            """INSERT INTO sessions
            (id, agent, source, model, model_config, started_at, ended_at,
             message_count, tool_call_count, input_tokens, output_tokens,
             reasoning_tokens, estimated_cost_usd, billing_provider, title,
             parent_session_id)
            VALUES (?, 'hermes', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s["id"], s["source"], s["model"], s["model_config"],
             s["started_at"], s["ended_at"], s["message_count"],
             s["tool_call_count"], s["input_tokens"], s["output_tokens"],
             s["reasoning_tokens"], s["estimated_cost_usd"],
             s["billing_provider"], s["title"], s["parent_session_id"])
        )
        stats["sessions"] += 1

    # Ingest messages
    messages = src.execute(
        "SELECT id, session_id, role, content, tool_calls, tool_name, "
        "tool_call_id, timestamp, token_count, finish_reason "
        "FROM messages ORDER BY timestamp"
    ).fetchall()

    # Track sequence per session
    seq_map = {}

    for m in messages:
        sid = m["session_id"]
        seq_map.setdefault(sid, 0)
        seq = seq_map[sid]
        seq_map[sid] += 1

        content = m["content"]
        parsed_tools = _parse_tool_calls(m["tool_calls"])

        # Determine tool status from finish_reason
        tool_status = "unknown"
        if parsed_tools:
            finish = m["finish_reason"] if m["finish_reason"] else ""
            if finish == "tool_calls":
                tool_status = "success"
            elif finish == "error":
                tool_status = "error"

        dst.execute(
            """INSERT INTO messages
            (session_id, seq, role, content, content_hash,
             tool_calls_json, tool_name, tool_call_id,
             tool_status, token_count, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, seq, m["role"], _truncate(content), _hash(content or ""),
             json.dumps(parsed_tools) if parsed_tools else None,
             m["tool_name"] or (parsed_tools[0]["name"] if parsed_tools else None),
             m["tool_call_id"],
             tool_status, m["token_count"], m["timestamp"])
        )
        stats["messages"] += 1

        # Count individual tool calls
        stats["tool_calls"] += len(parsed_tools)

    src.close()
    dst.commit()
    dst.close()

    return stats


def get_latest_timestamp(db_path: str) -> float | None:
    """Get the timestamp of the most recent message already ingested."""
    db = sqlite3.connect(db_path)
    result = db.execute("SELECT MAX(timestamp) FROM messages").fetchone()
    db.close()
    return result[0] if result else None
