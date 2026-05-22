"""MCP server: agents inspect their own behavior."""

from mcp.server import Server
from mcp.types import Tool, TextContent
import sqlite3
import os
import json

DB_PATH = os.path.expanduser("~/.agentlog/agentlog.db")
server = Server("agentlog")


def _query(sql: str, params: tuple = ()) -> list[dict]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(sql, params).fetchall()]
    db.close()
    return rows


@server.tool()
async def find_sessions(
    days: int = 7,
    agent: str = "hermes",
    source: str = "",
    status: str = "",
    limit: int = 10,
) -> str:
    """Find recent agent sessions matching criteria.

    Args:
        days: How many days back to search
        agent: Agent type ('hermes', 'claude-code', etc.)
        source: Filter by source ('cli', 'cron', 'telegram', '')
        status: Filter by session end status ('success', 'error', 'interrupted')
        limit: Max results to return
    """
    sql = """SELECT id, datetime(started_at, 'unixepoch') AS dt, source, model,
             title, message_count, tool_call_count,
             input_tokens + output_tokens AS total_tokens,
             estimated_cost_usd
             FROM sessions
             WHERE agent = ? AND started_at > unixepoch('now', ? || ' days')"""
    params = [agent, str(-days)]

    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)

    rows = _query(sql, tuple(params))
    return json.dumps(rows, indent=2)


@server.tool()
async def replay_turn(
    session_id: str,
    turn_seq: int = -1,
    context_lines: int = 5,
) -> str:
    """Replay a specific message or full session with surrounding context.

    Args:
        session_id: Session ID (full or prefix)
        turn_seq: Message sequence number (-1 for full session)
        context_lines: How many messages before/after to include
    """
    if "%" not in session_id:
        session_id = f"{session_id}%"

    if turn_seq == -1:
        messages = _query(
            """SELECT seq, role, tool_name, tool_status,
               substr(content, 1, 300) AS content_preview,
               token_count, datetime(timestamp, 'unixepoch') AS ts
               FROM messages WHERE session_id LIKE ? ORDER BY seq""",
            (session_id,)
        )
        return json.dumps(messages, indent=2)

    # Single turn with context
    messages = _query(
        """SELECT seq, role, tool_name, tool_status, content,
           token_count, datetime(timestamp, 'unixepoch') AS ts
           FROM messages WHERE session_id LIKE ? AND seq BETWEEN ? AND ?
           ORDER BY seq""",
        (session_id, turn_seq - context_lines, turn_seq + context_lines)
    )
    return json.dumps(messages, indent=2)


@server.tool()
async def token_usage(days: int = 30) -> str:
    """Get token usage and cost breakdown by agent and model.

    Args:
        days: Days to analyze
    """
    rows = _query(
        """SELECT agent, model,
           SUM(input_tokens + output_tokens) AS total_tokens,
           SUM(estimated_cost_usd) AS total_cost,
           COUNT(*) AS sessions,
           ROUND(AVG(input_tokens + output_tokens)) AS avg_tokens_per_session
           FROM sessions
           WHERE started_at > unixepoch('now', ? || ' days')
           AND estimated_cost_usd > 0
           GROUP BY agent, model
           ORDER BY total_cost DESC""",
        (str(-days),)
    )
    return json.dumps(rows, indent=2)


@server.tool()
async def tool_reliability(days: int = 30, min_calls: int = 5) -> str:
    """Get tool reliability stats: which tools fail most often.

    Args:
        days: Days to analyze
        min_calls: Minimum call count to include
    """
    rows = _query(
        """SELECT tool_name,
           COUNT(*) AS calls,
           SUM(CASE WHEN tool_status = 'success' THEN 1 ELSE 0 END) AS successes,
           SUM(CASE WHEN tool_status = 'error' THEN 1 ELSE 0 END) AS errors,
           SUM(CASE WHEN tool_status = 'timeout' THEN 1 ELSE 0 END) AS timeouts,
           ROUND(100.0 * SUM(CASE WHEN tool_status = 'success' THEN 1 ELSE 0 END)
                 / COUNT(*), 1) AS success_rate
           FROM messages
           WHERE tool_name IS NOT NULL
           AND timestamp > unixepoch('now', ? || ' days')
           GROUP BY tool_name
           HAVING COUNT(*) >= ?
           ORDER BY success_rate ASC""",
        (str(-days), min_calls)
    )
    return json.dumps(rows, indent=2)


@server.tool()
async def find_errors(search: str = "", limit: int = 20) -> str:
    """Find tool call errors and timeouts, optionally filtering by tool name.

    Args:
        search: Filter by tool name (substring match, empty for all)
        limit: Max results
    """
    sql = """SELECT m.timestamp, m.tool_name, m.tool_status,
             substr(m.content, 1, 200) AS error_context,
             s.id AS session_id, s.model
             FROM messages m JOIN sessions s ON m.session_id = s.id
             WHERE m.tool_status IN ('error', 'timeout')"""
    params = []
    if search:
        sql += " AND m.tool_name LIKE ?"
        params.append(f"%{search}%")
    sql += " ORDER BY m.timestamp DESC LIMIT ?"
    params.append(limit)

    rows = _query(sql, tuple(params))
    return json.dumps(rows, indent=2)


def main():
    import asyncio
    from mcp.server.stdio import stdio_server

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
