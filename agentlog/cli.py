"""agentlog CLI — ingest, query, and replay AI agent sessions."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/.agentlog/agentlog.db")


def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def cmd_ingest(args):
    """Ingest agent logs into the database."""
    _ensure_db()

    if args.agent == "hermes":
        source = args.source or os.path.expanduser("~/.hermes/state.db")
        if not os.path.exists(source):
            print(f"Error: Hermes state.db not found at {source}")
            sys.exit(1)
        from agentlog.parsers.hermes import ingest
        stats = ingest(source, DB_PATH)
    else:
        print(f"Error: unsupported agent type '{args.agent}'. Supported: hermes")
        sys.exit(1)

    print(f"Ingested: {stats['sessions']} sessions, {stats['messages']} messages, "
          f"{stats['tool_calls']} tool calls"
          + (f" ({stats['skipped']} already present)" if stats['skipped'] else ""))


def cmd_query(args):
    """Run a SQL query against the database."""
    if not os.path.exists(DB_PATH):
        print("No data yet. Run 'agentlog ingest' first.")
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    if args.sql:
        query = args.sql
    elif args.preset == "tokens":
        query = """SELECT day, agent, model, total_tokens, estimated_cost_usd
                   FROM daily_usage ORDER BY day DESC LIMIT 30"""
    elif args.preset == "tools":
        query = "SELECT * FROM tool_reliability"
    elif args.preset == "sessions":
        query = """SELECT started_at, agent, source, model, message_count,
                   tool_call_count, input_tokens + output_tokens AS total_tokens
                   FROM sessions ORDER BY started_at DESC LIMIT 30"""
    elif args.preset == "errors":
        query = """SELECT m.timestamp, m.tool_name, m.tool_status, m.content,
                   s.id AS session_id
                   FROM messages m JOIN sessions s ON m.session_id = s.id
                   WHERE m.tool_status IN ('error', 'timeout')
                   ORDER BY m.timestamp DESC LIMIT 30"""
    elif args.preset == "cost":
        query = """SELECT agent, model,
                   SUM(estimated_cost_usd) AS total_cost,
                   SUM(input_tokens + output_tokens) AS total_tokens,
                   COUNT(*) AS sessions
                   FROM sessions
                   WHERE estimated_cost_usd > 0
                   GROUP BY agent, model
                   ORDER BY total_cost DESC"""
    else:
        query = "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 10"

    try:
        rows = db.execute(query).fetchall()
    except Exception as e:
        print(f"Query error: {e}")
        sys.exit(1)

    if not rows:
        print("No results.")
        return

    # Pretty-print as table
    cols = rows[0].keys()
    widths = {c: len(c) for c in cols}
    for row in rows:
        for c in cols:
            val = str(row[c])[:80] if row[c] is not None else "NULL"
            widths[c] = max(widths[c], len(val))

    # Header
    header = " │ ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("─" * len(header))

    for row in rows:
        vals = [str(row[c])[:80].ljust(widths[c]) if row[c] is not None
                else "NULL".ljust(widths[c]) for c in cols]
        print(" │ ".join(vals))

    print(f"\n{len(rows)} rows")


def cmd_replay(args):
    """Replay a session timeline."""
    if not os.path.exists(DB_PATH):
        print("No data yet. Run 'agentlog ingest' first.")
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # If session_id not provided, list recent sessions
    if not args.session_id:
        sessions = db.execute(
            "SELECT id, datetime(started_at, 'unixepoch') AS dt, source, model, "
            "title, message_count "
            "FROM sessions ORDER BY started_at DESC LIMIT 20"
        ).fetchall()
        print("Recent sessions:")
        for s in sessions:
            title = s["title"] or "(no title)"
            print(f"  {s['id'][:8]}...  {s['dt']}  [{s['source']}]  "
                  f"{s['model'] or '?'}  {title[:60]}")
        print("\nRun: agentlog replay <session_id>")
        return

    # Get session info
    session = db.execute(
        "SELECT *, datetime(started_at, 'unixepoch') AS dt FROM sessions WHERE id = ?",
        (args.session_id,)
    ).fetchone()

    if not session:
        # Try prefix match
        session = db.execute(
            "SELECT *, datetime(started_at, 'unixepoch') AS dt FROM sessions "
            "WHERE id LIKE ? ORDER BY started_at DESC LIMIT 1",
            (args.session_id + "%",)
        ).fetchone()
        if not session:
            print(f"No session found matching '{args.session_id}'")
            sys.exit(1)

    print(f"═══ Session: {session['id']}")
    print(f"═══ Date:    {session['dt']}")
    print(f"═══ Agent:   {session['agent']} | Model: {session['model']}")
    print(f"═══ Source:  {session['source']} | Messages: {session['message_count']} | "
          f"Tools: {session['tool_call_count']}")
    print(f"═══ Tokens:  {session['input_tokens']:,} in / "
          f"{session['output_tokens']:,} out")
    if session["title"]:
        print(f"═══ Title:   {session['title']}")
    print()

    # Get messages
    messages = db.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY seq",
        (session["id"],)
    ).fetchall()

    for m in messages:
        role_icon = {"user": "👤", "assistant": "🤖", "tool": "🔧", "system": "⚙️"}.get(m["role"], "  ")
        ts = datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).strftime("%H:%M:%S")

        if m["role"] == "tool" or m["tool_name"]:
            status_icon = {"success": "✓", "error": "✗", "timeout": "⏰"}.get(m["tool_status"], "?")
            duration = f" ({m['tool_duration_ms']}ms)" if m["tool_duration_ms"] else ""
            print(f"[{ts}] 🔧 {status_icon} {m['tool_name']}{duration}")

            # Show tool arguments summary
            tool_calls_json = m["tool_calls_json"] if m["tool_calls_json"] else None
            if tool_calls_json:
                try:
                    tcs = json.loads(m["tool_calls_json"])
                    for tc in tcs:
                        args = tc.get("arguments", "")
                        if len(args) > 120:
                            args = args[:117] + "..."
                        print(f"      → {tc['name']}({args})")
                except (json.JSONDecodeError, KeyError):
                    pass
        elif m["role"] == "user":
            content = (m["content"] or "")[:200]
            print(f"[{ts}] {role_icon} {content}")
        elif m["role"] == "assistant":
            content = (m["content"] or "")[:300]
            # Show first line only for long assistant messages
            first_line = content.split("\n")[0]
            if len(first_line) > 150:
                first_line = first_line[:147] + "..."
            print(f"[{ts}] {role_icon} {first_line}")
            if m["token_count"]:
                print(f"      ({m['token_count']} tokens)")

    print(f"\n── {len(messages)} messages ──")


def main():
    parser = argparse.ArgumentParser(
        description="agentlog — observability for personal AI agents"
    )
    sub = parser.add_subparsers(dest="command")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Ingest agent logs")
    p_ingest.add_argument("--agent", "-a", default="hermes",
                          choices=["hermes"], help="Agent type")
    p_ingest.add_argument("--source", "-s", help="Path to agent log file")
    p_ingest.set_defaults(func=cmd_ingest)

    # query
    p_query = sub.add_parser("query", help="Query sessions and tool calls")
    p_query.add_argument("--sql", help="Raw SQL query")
    p_query.add_argument("--preset", "-p",
                         choices=["tokens", "tools", "sessions", "errors", "cost"],
                         default="tokens", help="Pre-built query")
    p_query.set_defaults(func=cmd_query)

    # replay
    p_replay = sub.add_parser("replay", help="Replay a session timeline")
    p_replay.add_argument("session_id", nargs="?", help="Session ID (or prefix)")
    p_replay.add_argument("--search", help="Find sessions containing text")
    p_replay.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
