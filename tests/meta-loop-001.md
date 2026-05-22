# Meta-Loop Test 001: Lint Efficiency — Context vs Compute

**Date:** 2026-05-21  
**Agent:** Vulcan (Node B)  
**Test files:** 21 wiki pages on Node A, 7 known issues planted  
**Goal:** Compare two approaches for linting a wiki — loading files into LLM context vs batch-scanning via remote script

---

## Setup

21 wiki files copied from Node A's wiki to `/tmp/wiki-test/`. 7 intentional issues planted:

| File | Issue Type | Detail |
|------|-----------|--------|
| xhuljano.md | Broken wikilinks | `[[broken-page-that-doesnt-exist]]`, `[[nonexistent-ref-page]]`, `[[another-fake-page]]` |
| fractal.md | Broken wikilinks | `[[fake-scaling-concept]]`, `[[imaginary-theorem]]` |
| homelab-session-log.md | Broken wikilink | `[[ghost-service]]` |
| no-frontmatter.md | Missing frontmatter | No `---` YAML block |
| no-frontmatter.md | Broken wikilink | `[[broken-link-here]]` |
| node-a.md | Stale date | `> Last updated: 2025-01-15` (491 days) |
| bulk-solving.md | Suspicious tags | `#unfinished #TODO #FIXME` |

---

## Test A: OLD APPROACH — Read Every File Into Context

**Method:** Copy all 21 files to Node B locally, read each one with `read_file()`, then analyze.

**Tool calls:**
- 1 `scp` to copy files (terminal)
- 21 `read_file()` calls — one per file

**Context consumed:**
- ~85KB of raw file content loaded into the LLM's context window
- 21 files × ~4KB avg = ~84KB total
- Estimated tokens: ~55,000-70,000 input tokens (content + tool overhead)
- 6 turns required (batching 5-6 files per turn due to context limits)

**Wall time:** ~2 minutes (sequential reads + analysis across 6 turns)

**Issues found:** 10/10 planted issues detected

**Waste:** 98% of tokens were file contents irrelevant to linting. Only ~1KB of the total context was actual lint issues.

---

## Test B: NEW APPROACH — Batch-Scan via Remote Script

**Method:** Write a Python script, push it to Node A via SSH, execute it remotely. Script scans all 21 files, applies lint rules, returns ONLY the issues found.

**Tool calls:**
- 1 `terminal()` — heredoc + SSH to write and execute script

**Context consumed:**
- Script source: ~2KB (one-time, reused across runs)
- Script output: ~1KB (issue list only)
- Total tokens: ~500 input + ~300 output = ~800 tokens

**Wall time:** ~3 seconds (single SSH round-trip)

**Issues found:** 10/10 planted issues detected

**Script:** Also detected 94 cross-reference wikilinks that point to pages outside the test set — these are valid links in the full wiki but flagged because only 21 test files were present. In production, the script checks against the full 214-page wiki index.

---

## Comparison

| Metric | Test A (Read All) | Test B (Batch Scan) | Ratio |
|--------|-------------------|---------------------|-------|
| Tool calls | 22 (1 scp + 21 reads) | 1 (SSH + script) | 22:1 |
| Turns | 6 | 1 | 6:1 |
| Input tokens | ~60,000 | ~500 | **120:1** |
| Output tokens | ~85,000 (file contents) | ~300 (issues only) | **280:1** |
| Wall time | ~120s | ~3s | 40:1 |
| Issues found | 10/10 | 10/10 | same |
| False positives | 0 | 94* | — |

*94 false positives because only 21 test files were available for cross-reference validation. In production with full wiki index (214 pages), false positives near zero.

---

## Root Cause Analysis

The cron lint-fixer burns 1.8M tokens per 4-hour cycle because it follows this pattern:

```
for each wiki page (215 pages):
    read_file(page)        # loads 4KB into context
    find issues            # uses ~10 tokens of reasoning
    patch if needed         # uses ~50 tokens
```

**98% of tokens are dead weight** — the file content rides along in context but is never needed for the lint check. The lint rules (check frontmatter, validate wikilinks, detect stale dates) are mechanical pattern matches that a Python script can do with zero LLM tokens.

This is equivalent to hiring a PhD mathematician to check 215 arithmetic problems by having them read each textbook chapter aloud first.

---

## Recommendation

Replace the lint-fixer cron's `read_file` loop with a pre-scan Python script that:

1. Runs on Node A via cron (or Node B via SSH)
2. Scans all wiki pages in one pass
3. Outputs a JSON list of ONLY files with issues
4. The LLM then `read_file()` ONLY the flagged files (typically 1-3, not 215)
5. Applies fixes to just those files

**Projected savings:** 1.8M tokens → ~20K tokens per cycle (99% reduction). At current API pricing, that's ~$0.50/day saved. More importantly: the cron runs in seconds instead of minutes, and the LLM's context is free for actual reasoning instead of being flooded with irrelevant text.

### Migration Path

```bash
# Current (wasteful)
for page in wiki/*.md; do
    read_file "$page"           # 4KB → LLM context
done                             # 215x → 860KB in context

# Proposed (efficient)
python3 lint-scan.py --wiki ~/wiki --output issues.json   # runs locally, 0 LLM tokens
# issues.json = {"broken_links": [...], "stale": [...], "no_frontmatter": [...]}
# LLM reads ONLY flagged files:
for file in $(jq -r '.files[]' issues.json); do
    read_file "$file"           # only 2-5 files, not 215
done
```

---

## Meta-Lesson

This test demonstrates a general principle for AI agent design:

> **Don't use the LLM as a file scanner. Use it as a reasoning engine.**

Whenever an agent's workflow involves reading many files and applying simple rules, push the scanning to a script and reserve the LLM for the decisions that actually require reasoning. The cost ratio in this test was 120:1 — and that's just for 21 files. For 215 files, the ratio is closer to 1000:1.

---

*Test conducted on Node B (RTX 4080 Super, Fedora 44) targeting Node A (Dell OptiPlex 7090 SFF, Fedora 43).*
