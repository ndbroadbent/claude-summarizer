# claude-summarizer

> **Archived (23 Nov 2026).** claude-summarizer now lives on as the session summaries in
> [Quietdeck](https://github.com/QuietdeckAI/quietdeck): the same prompts, filter and limits, running inside the
> Quietdeck node, triggered when a session goes idle rather than hourly, with the summaries kept on the deck. Coming
> from claude-summarizer, `quietdeck-node import-summaries <vault>` brings your history across. This repository is
> no longer maintained.

Rolling, tiered summaries of every Claude Code (and Codex) session on your Mac: what each one is for, what it did
recently and what it's waiting on. For people who run many long-lived agent sessions at once and lose track of which
one was doing what.

An hourly job reads what each transcript gained since its last run, throws away the noise (tool output, watcher
wake-ups, compactions), and asks headless `claude -p` on your own subscription to rewrite that session's summary in
three sizes:

- **page**: up to about one page: what the session is for, what happened recently, what's open;
- **paragraph**: one or two paragraphs;
- **sentence**: one sentence.

Each update is a Markdown file (an Obsidian vault works well), a local web UI browses them with a timeline per
session, and `claude-summarizer digest` prints a briefing another agent can load: paragraphs for sessions active in the
last 30 days, sentences for the 6 months before.

Python 3.9+ standard library only. macOS (launchd); the code itself runs anywhere Claude Code does.

## Install

```sh
git clone https://github.com/ndbroadbent/claude-summarizer ~/code/claude-summarizer
cd ~/code/claude-summarizer
bin/claude-summarizer bootstrap --dry-run   # counts the calls the first pass would make
bin/claude-summarizer bootstrap             # the first pass, newest sessions first, 5 s between calls
bin/install                                 # hourly LaunchAgent + web UI on http://localhost:8790
```

`bin/install` links `~/.local/bin/claude-summarizer`. Logs go to `~/Library/Logs/claude-summarizer*.log`, state to
`~/.local/state/claude-summarizer/` (`state.json` holds the byte offsets; `calls.jsonl` has one line per call with its
tokens, seconds and notional cost).

## Settings

Optional, in `~/.config/claude-summarizer/config.json`:

```json
{
  "vault_dir": "~/Notes/Sessions",
  "user_name": "Sam",
  "model": "sonnet",
  "port": 8790
}
```

`vault_dir` defaults to `~/.local/share/claude-summarizer`. Keep it private: summaries describe whatever your sessions
worked on. `user_name` is how summaries refer to you.

## Commands

```
claude-summarizer digest [--json]          # the briefing: paragraphs (last 30 days), sentences (30 days to 6 months)
claude-summarizer page <tab|session-id>    # one session's latest page; --all lists every update's sentence first
claude-summarizer status                   # last run, sessions, which account the next call would use
claude-summarizer run [--dry-run] [--max-calls N] [--only TEXT]
claude-summarizer bootstrap [...]          # the same run, pausing between calls
claude-summarizer serve [--port 8790]      # the web UI (the LaunchAgent runs it)
```

The web UI lists every session with its last activity, a copy button for its resume command
(`cd <dir> && claude --resume <id>`) and its sentence. A row expands to the paragraph; "Full page" opens the latest
page with Prev/Next through its earlier updates, and a Timeline view shows one sentence per update, in order, so you
can see where a session's focus moved.

## Files

```
<vault_dir>/index.json                      every session: name, folder, directory, resume command, agent,
                                            last activity, latest sentence and paragraph, latest page
<vault_dir>/<name>/<YYYY-MM-DDTHH>.md       one update: front matter, then Sentence / Paragraph / Page
```

A file is named by the local hour of the activity it covers, floored. A session is named after its
[herdr](https://github.com/herdrdev/herdr) tab if it has one, else `<directory name>-<session ID prefix>`.

## How it decides what to summarize

Nothing is sent to a model unless something happened. Dropped first: tool output, background-task and watcher
notifications, plugin messages, usage-limit auto-continuations, compaction summaries, meta records, built-in slash
commands and their output, interruptions. Whole transcripts are skipped when they come from headless runs (Claude
Code's `entrypoint` other than `cli`, which includes this tool's own calls) or `codex exec`. Subagent transcripts
aren't read: the parent already has each subagent's task and relays its result.

What remains is your messages, messages from other sessions, the agent's text and one line per tool call (quiet tools
such as Read and Grep left out). A chunk of activity needs a message from you or another session, 300+ characters of
agent text, or a file edit, before it's worth a call; otherwise its offset moves on for free. The prompt also lets the
model answer `no_change` for anything the filter missed. Input is capped at 48,000 characters: tool lines go first,
then older agent text, then older messages from you.

Activity is chunked by local day for the last 30 days and by ISO week before that. Each chunk is one call and one
point on the session's timeline. Offsets move only after a chunk's call succeeds; a chunk that fails three runs in a
row is skipped.

## Which account it uses

With [claude-account-rotator](https://github.com/ndbroadbent/claude-account-rotator)'s coordinator running, each call
uses its "soonest reset" rule: of the signed-in accounts that aren't spent, the one whose week resets first, so room
that's about to refill gets used. If every account is spent, the run stops and the next hour tries again. Without the
coordinator, calls use Claude Code's own login.

Calls are lean: `--setting-sources ""` (no settings, hooks, plugins or CLAUDE.md), `--tools ""`, `--strict-mcp-config`,
`--disable-slash-commands`, `--no-session-persistence` and `--json-schema` for the answer. A call's fixed context is
about 3,000 tokens and it takes 10 to 15 seconds on Sonnet.
