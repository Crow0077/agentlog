"""
Canonical SQLite schema for agentlog.

Every agent parser maps its native log format into these tables.
The schema targets queryability, not fidelity to any one agent's format.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,              -- 'hermes', 'claude-code', 'codex', 'openclaw', etc.
    source TEXT,                      -- 'cli', 'cron', 'telegram', 'api'
    model TEXT,                       -- 'deepseek-v4-pro', 'claude-sonnet-4'
    model_config TEXT,                -- JSON blob: provider, temp, etc.
    started_at REAL NOT NULL,         -- Unix timestamp
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    billing_provider TEXT,
    title TEXT,
    parent_session_id TEXT REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,             -- Order within session
    role TEXT NOT NULL,               -- 'user', 'assistant', 'tool', 'system'
    content TEXT,                     -- Truncated to 2000 chars for display
    content_hash TEXT,                -- SHA256 for dedup
    tool_calls_json TEXT,             -- JSON array of tool calls in this message
    tool_name TEXT,                   -- Single tool: 'terminal', 'web_search', etc.
    tool_call_id TEXT,                -- Provider-assigned tool call ID
    tool_duration_ms INTEGER,         -- How long the tool took
    tool_status TEXT,                 -- 'success', 'error', 'timeout', 'unknown'
    token_count INTEGER,
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_tool ON messages(tool_name, tool_status);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_model ON sessions(model);
"""

# View: daily token usage by agent
DAILY_USAGE_VIEW = """
CREATE VIEW IF NOT EXISTS daily_usage AS
SELECT
    date(started_at, 'unixepoch') AS day,
    agent,
    model,
    COUNT(*) AS session_count,
    SUM(input_tokens) AS input_tokens,
    SUM(output_tokens) AS output_tokens,
    SUM(input_tokens + output_tokens) AS total_tokens,
    SUM(estimated_cost_usd) AS estimated_cost_usd
FROM sessions
GROUP BY day, agent, model
ORDER BY day DESC;
"""

# View: tool reliability stats
TOOL_RELIABILITY_VIEW = """
CREATE VIEW IF NOT EXISTS tool_reliability AS
SELECT
    tool_name,
    COUNT(*) AS calls,
    SUM(CASE WHEN tool_status = 'success' THEN 1 ELSE 0 END) AS successes,
    SUM(CASE WHEN tool_status = 'error' THEN 1 ELSE 0 END) AS errors,
    SUM(CASE WHEN tool_status = 'timeout' THEN 1 ELSE 0 END) AS timeouts,
    ROUND(AVG(tool_duration_ms), 1) AS avg_duration_ms
FROM messages
WHERE tool_name IS NOT NULL
GROUP BY tool_name
ORDER BY calls DESC;
"""

FULL_SCHEMA = SCHEMA + DAILY_USAGE_VIEW + TOOL_RELIABILITY_VIEW
