# agentlog

**`git log` for your AI agent.** Ingest, query, replay — one pip install, zero infrastructure, your data stays on your disk.

```bash
pip install agentlog
agentlog ingest              # Hermes + Claude Code supported
agentlog query -p tools      # Which tool fails most?
agentlog query -p cost       # What model costs the most?
agentlog replay              # Replay any past session like a transcript
```

<p align="center">
  <i>asciicast demo — <code>asciinema play demo.cast</code> to watch</i>
</p>

## The Problem

Your AI agent runs hundreds of sessions a week. When something goes wrong, you're grepping raw JSON or scrolling through terminal scrollback. You have no idea which tools fail most, which models burn the most tokens, or what your agent did three days ago.

Existing tools (LangSmith, Langfuse, AgentOps) target enterprise teams running production LangChain pipelines. They require SDK instrumentation, cloud dashboards, and per-seat pricing.

If you run an AI agent on your own machine, you have nothing.

## What agentlog Gives You

| Command | What it does |
|---------|-------------|
| `agentlog ingest` | Reads your agent's log files → structured SQLite database |
| `agentlog query` | SQL queries for token usage, costs, tool reliability |
| `agentlog replay <id>` | Reconstructs any session as a human-readable timeline |
| `agentlog server` | MCP server so your agent can inspect ITSELF |

**Agent can query its own history:**
```
> What tools failed most this week?
[agent queries agentlog via MCP]
→ blender_execute_script: 16 failures (timeout), patch: 588 no-ops
```

## Quickstart

```bash
pip install agentlog

# Hermes: reads state.db directly
agentlog ingest

# Claude Code: reads ~/.claude/projects/ session files
agentlog ingest --agent claude-code

# See daily token usage
agentlog query -p tokens

# Which tools fail most often?
agentlog query -p errors

# Replay a session
agentlog replay
agentlog replay <session_id_prefix>
```

## Supported Agents

| Agent | Parser | Status |
|-------|--------|--------|
| Hermes | `state.db` direct read | ✅ Complete |
| Claude Code | JSONL session files | ✅ Complete |
| Codex CLI | Terminal output | 🔜 Planned |
| OpenClaw | Session store | 🔜 Planned |

## How It Works

```
Hermes state.db ──────┐
Claude Code JSONL ────┤── parsers/ ──→ agentlog.db (SQLite, ~5MB)
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
                agentlog         agentlog         agentlog
                ingest           query            replay
                                                      │
                                              agentlog server
                                              (MCP — agents self-inspect)
```

Each parser maps a native log format into a canonical schema: `sessions` → `messages` (with tool call metadata). Two SQL views (`daily_usage`, `tool_reliability`) are pre-built for the most common queries.

No SDK. No instrumentation. agentlog reads what your agent already writes to disk.

## What agentlog Is NOT

- **Not a monitoring dashboard** — no Grafana, no alerts. It's `git log`, not Datadog.
- **Not a cloud service** — your agent data never leaves your machine.
- **Not LangSmith** — no LangChain dependency, no per-seat pricing, no vendor lock-in.
- **Not a replacement for your agent's native logging** — it's a reader, not a writer.

## MCP Server

Let agents inspect their own behavior. Five tools: `find_sessions`, `replay_turn`, `token_usage`, `tool_reliability`, `find_errors`.

Wire into Hermes:
```yaml
mcp_servers:
  agentlog:
    command: agentlog
    args: ["server"]
```

## Philosophy

- **SQLite is the universal format.** Everything goes in one file. You own it.
- **CLI first.** Web dashboards are optional. The terminal is the primary interface.
- **Zero infrastructure.** `pip install` and you're done.
- **Privacy by default.** Your agent sessions contain private conversations and code. They stay on your disk.

Built in the spirit of Simon Willison's tools — composable, self-contained, and yours.

## License

MIT
