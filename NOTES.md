# Build notes

## The problem

~60 long-running Claude Code sessions (and some Codex) in herdr tabs, one per area of work. The email-triage session
("planning") needs to know which session owns what, and Nathan needs to find "the session that was doing X" weeks
later. Raw transcripts are huge (one session reached 433 MB of JSONL; all Claude projects together are 23 GB) and
mostly tool output.

## What already existed (checked 21 Nov 2026; Talira found nothing that does it all)

- maleta/claude-sessions (MIT): Stop/PreCompact/SessionEnd hooks run `claude -p` after the first message and every 5
  more, merging into one summary, with a static HTML page. Good ideas (merge into previous summary, copyable resume
  commands). Gaps: runs inside every session, filters only by message count (watcher wake-ups and compactions trigger
  it), one summary with no tiers or ageing, no idea of herdr tabs.
- Viewers without rolling summaries: kmizzi/claude-code-sessions, jpatel3/claude-dashboard,
  wannabemrrobot/claude-sessions-dashboard, jhlee0409/claude-code-history-viewer, ccboard.

## Decisions and why

- **One central hourly job, not hooks in every session.** 60 sessions each running a summariser would race and burn
  limits; a single LaunchAgent with a lock does one call at a time.
- **Byte offsets per transcript.** Each run reads only the new lines. A line still being written (no newline yet) is
  left for next time.
- **A cheap filter before any model call.** Most hourly growth is noise: CI watchers waking up, background task
  notifications, auto-compactions, tool output. Claude Code's records carry enough metadata to drop it without
  guessing: `origin.kind` (`human`, `peer`, `task-notification`, `plugin`, `auto-continuation`), `isMeta`,
  `isCompactSummary`, `entrypoint` (`cli` vs `sdk-cli` for headless runs). Codex's `session_meta.originator`
  separates the interactive TUI from `codex exec` runs, which here are reviews that Claude sessions launch.
- **Three tiers written every time; the reader chooses.** No re-summarising as things age: the paragraph serves the
  last 30 days, the sentence 30 days to 6 months.
- **Chunk by day (week for old history).** One call per day of activity gives a timeline of how each session's focus
  moved. Older than 30 days, weekly chunks keep the bootstrap to 1,134 calls instead of 1,469.
- **Account choice via the existing account-rotator coordinator.** Same "soonest reset" rule (use the week that
  refills first). Found that the rotator already had a switch to keep its mod out of headless runs
  (`CLAUDE_ACCOUNT_ROTATOR_REFRESH=1`, made for its own token-refresh runs), and that the Claude in Chrome shuffle only
  runs in interactive sessions, so nothing in the rotator needed changing.
- **Lean headless calls.** `--setting-sources ""` drops settings, hooks, plugins and CLAUDE.md; `--tools ""`,
  `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence` (so the summariser's own runs never
  become transcripts to summarise). Fixed context about 3k tokens; `--json-schema` gives structured output.
- **Python stdlib only**, like the triage scripts: no dependencies to break; runs on macOS's /usr/bin/python3 (3.9).
- **Personal vault**, not the shared company vault: some sessions discuss money.
- **Timestamped files, one per update** (`<tab>/2026-11-21T15.md`), so Obsidian shows the history and the web UI can
  step through it.

## Numbers

- Discovery: 760 transcripts modified in the last 6 months → 389 sessions with real activity (headless runs and
  `codex exec` reviews skipped).
- Test calls: 10 to 13 s each, 4k to 24k input tokens, about $0.07 notional (Sonnet API prices; billed to the
  subscription).
- Condensed input per day-chunk: median 25k characters, p90 54k (capped at 48k).
- Bootstrap and steady-state hourly numbers: see below once measured.
