"""Find transcripts, read their new records from a byte offset, and turn them into events.

An event is one thing worth telling the summariser: a message from the user (`human`), a message forwarded from another
session (`peer`), the agent's own text (`assistant`) or one line per tool call (`tool`). Everything else is dropped
here, before any model sees it: tool output, background task and watcher notifications, scheduled wake-ups,
compaction summaries, slash-command chatter, plugin messages.

Subagent transcripts (`<session>/subagents/*.jsonl`) aren't read: the parent transcript already has each subagent's
task (the Agent call's description) and the parent's own text relays what came back.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import config


@dataclass
class Source:
    path: str
    agent: str  # 'claude' or 'codex'


@dataclass
class Event:
    kind: str  # human | peer | assistant | tool
    text: str
    ts: float  # epoch seconds
    end: int  # byte offset just past the record's line


@dataclass
class ReadResult:
    session_id: str | None
    cwd: str | None
    events: list[Event] = field(default_factory=list)
    end: int = 0  # byte offset just past the last complete line read
    skip: str | None = None  # why the whole transcript is ignored (headless run, codex exec)


def discover(max_age_days: int = config.MAX_AGE_DAYS) -> list[Source]:
    cutoff = time.time() - max_age_days * 86400
    out = []
    for path in glob.glob(os.path.join(config.CLAUDE_PROJECTS, '*', '*.jsonl')):
        if os.path.getmtime(path) >= cutoff:
            out.append(Source(path, 'claude'))
    for path in glob.glob(os.path.join(config.CODEX_SESSIONS, '*', '*', '*', '*.jsonl')):
        if os.path.getmtime(path) >= cutoff:
            out.append(Source(path, 'codex'))
    return out


def parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def read(source: Source, offset: int) -> ReadResult:
    """Read complete lines from `offset` on. The session ID and cwd come from the file's first records, so a read from
    a later offset still peeks at the head for them."""
    parse = _claude_record if source.agent == 'claude' else _codex_record
    meta = _head_meta(source)
    result = ReadResult(session_id=meta['session_id'], cwd=meta['cwd'], skip=meta['skip'], end=offset)
    if result.skip:
        result.end = os.path.getsize(source.path)
        return result
    last_ts = None
    with open(source.path, 'rb') as f:
        f.seek(offset)
        pos = offset
        for raw in f:
            if not raw.endswith(b'\n'):
                break  # a line still being written
            pos += len(raw)
            result.end = pos
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            ts = parse_ts(record.get('timestamp')) or last_ts or os.path.getmtime(source.path)
            last_ts = ts
            for kind, text in parse(record):
                text = text.strip()
                if text:
                    result.events.append(Event(kind, text, ts, pos))
    return result


def _head_meta(source: Source) -> dict:
    meta = {'session_id': None, 'cwd': None, 'skip': None}
    if source.agent == 'codex':
        with open(source.path, 'rb') as f:
            first = f.readline()
        try:
            payload = json.loads(first).get('payload') or {}
        except json.JSONDecodeError:
            payload = {}
        meta['session_id'] = payload.get('id') or payload.get('session_id')
        meta['cwd'] = payload.get('cwd')
        if payload.get('originator') == 'codex_exec' or payload.get('source') not in ('cli', 'vscode'):
            meta['skip'] = 'codex exec run (a review a Claude session started)'
        return meta
    meta['session_id'] = os.path.basename(source.path)[:-len('.jsonl')]
    with open(source.path, 'rb') as f:
        for _ in range(200):
            raw = f.readline()
            if not raw:
                break
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get('cwd') and not meta['cwd']:
                meta['cwd'] = record['cwd']
            if record.get('entrypoint'):
                if record['entrypoint'] != 'cli':
                    meta['skip'] = f"headless run ({record['entrypoint']})"
                break
    return meta


# ---------------------------------------------------------------- Claude Code

SKIP_ORIGINS = {'task-notification', 'plugin', 'auto-continuation'}
NOISE_COMMANDS = {
    '/mcp', '/model', '/login', '/logout', '/compact', '/clear', '/list-agents', '/feedback', '/effort', '/usage',
    '/usage-credits', '/autocompact', '/config', '/status', '/cost', '/resume', '/tui', '/reload-plugins', '/plugin',
    '/permissions', '/doctor', '/help', '/exit', '/fast', '/context', '/memory', '/hooks', '/ide', '/terminal-setup',
    '/theme', '/vim', '/statusline', '/agents', '/rename', '/export', '/release-notes', '/upgrade', '/btw',
}
SYSTEM_REMINDER = re.compile(r'<system-reminder>.*?</system-reminder>', re.S)
COMMAND = re.compile(r'<command-name>(.*?)</command-name>', re.S)
COMMAND_ARGS = re.compile(r'<command-args>(.*?)</command-args>', re.S)
PEER_BODY = re.compile(r'<(?:cross-session-message|agent-message)[^>]*>(.*?)</(?:cross-session-message|agent-message)>', re.S)
QUIET_TOOLS = {'Read', 'Grep', 'Glob', 'ToolSearch', 'TaskOutput', 'TodoWrite', 'TaskList', 'TaskGet', 'LSP'}


def _user_text(text: str) -> str | None:
    text = SYSTEM_REMINDER.sub('', text).strip()
    if not text or text.startswith('This session is being continued from a previous conversation'):
        return None
    if text.startswith(('<local-command-', '<artifact-content-authored-by-others', '<task-notification')):
        return None
    if text.startswith('[Request interrupted by user'):
        return None
    m = COMMAND.search(text)
    if m:
        name = m.group(1).strip()
        if not name.startswith('/'):
            name = '/' + name
        if name in NOISE_COMMANDS:
            return None
        args = COMMAND_ARGS.search(text)
        return f"{name} {args.group(1).strip() if args else ''}".strip()
    return text


def _claude_record(record: dict):
    kind = record.get('type')
    message = record.get('message') or {}
    content = message.get('content')
    if kind == 'user':
        if record.get('isCompactSummary') or record.get('isMeta'):
            return
        origin = (record.get('origin') or {}).get('kind')
        if origin in SKIP_ORIGINS:
            return
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                return
            texts = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text']
            if any(isinstance(b, dict) and b.get('type') == 'image' for b in content):
                texts.append('[image attached]')
        else:
            return
        joined = '\n'.join(t for t in texts if t)
        if origin == 'peer':
            m = PEER_BODY.search(joined)
            body = SYSTEM_REMINDER.sub('', m.group(1) if m else joined).strip()
            if body:
                yield 'peer', body
            return
        text = _user_text(joined)
        if text:
            yield 'human', text
    elif kind == 'assistant' and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text':
                yield 'assistant', block.get('text', '')
            elif block.get('type') == 'tool_use':
                line = _tool_line(block.get('name', '?'), block.get('input') or {})
                if line:
                    yield 'tool', line


def _short(value, n: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = ' '.join(text.split())
    return text if len(text) <= n else text[: n - 1] + '…'


def _tool_line(name: str, args: dict) -> str | None:
    if name in QUIET_TOOLS:
        return None
    home = config.HOME
    if name == 'Bash':
        return f"Bash: {_short(args.get('description') or args.get('command', ''), 140)}"
    if name in ('Edit', 'Write', 'NotebookEdit', 'MultiEdit'):
        return f"{name} {str(args.get('file_path') or args.get('notebook_path') or '').replace(home, '~')}"
    if name in ('Agent', 'Task'):
        return f"Agent: {_short(args.get('description') or args.get('prompt', ''), 140)}"
    if name == 'Skill':
        return f"Skill {args.get('skill')} {_short(args.get('args') or '', 80)}".strip()
    if name in ('SendMessage',):
        return f"SendMessage to {args.get('to')}: {_short(args.get('message', ''), 120)}"
    if name in ('WebSearch', 'WebFetch'):
        return f"{name}: {_short(args.get('query') or args.get('url') or '', 120)}"
    return f"{name}: {_short(args, 120)}"


# ---------------------------------------------------------------- Codex

CODEX_SKIP_PREFIXES = (
    '# AGENTS.md instructions', '<environment_context', '<user_instructions', '<turn_aborted', '<permissions',
    '<model_switch', '<multi_agent', '<user_shell_command',
)


def _codex_record(record: dict):
    if record.get('type') != 'response_item':
        return
    p = record.get('payload') or {}
    kind = p.get('type')
    if kind == 'message':
        role = p.get('role')
        parts = p.get('content') or []
        text = '\n'.join(c.get('text', '') for c in parts if isinstance(c, dict))
        if role == 'user':
            text = text.strip()
            if text and not text.startswith(CODEX_SKIP_PREFIXES):
                yield 'human', text
        elif role == 'assistant':
            yield 'assistant', text
    elif kind in ('function_call', 'custom_tool_call', 'local_shell_call'):
        args = p.get('arguments') or p.get('input') or p.get('action') or ''
        yield 'tool', f"{p.get('name') or kind}: {_short(args, 140)}"


# ---------------------------------------------------------------- filter + condense

def is_real(events: list[Event]) -> bool:
    if any(e.kind in ('human', 'peer') for e in events):
        return True
    if sum(len(e.text) for e in events if e.kind == 'assistant') >= config.MIN_ASSISTANT_CHARS:
        return True
    return any(e.kind == 'tool' and e.text.startswith(('Edit ', 'Write ', 'MultiEdit ', 'NotebookEdit ')) for e in events)


def last_real_ts(events: list[Event]) -> float | None:
    ts = [e.ts for e in events if e.kind in ('human', 'peer') or (e.kind == 'assistant' and len(e.text) >= 80)]
    return max(ts) if ts else None


LIMITS = {'human': 2500, 'peer': 1500, 'assistant': 1500, 'tool': 160}
SHRUNK = {'human': 700, 'peer': 500, 'assistant': 500}
LABEL = {'human': 'USER', 'peer': 'FROM ANOTHER SESSION', 'assistant': 'AGENT'}


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f' […{len(text) - n} chars cut]'


def condense(events: list[Event], budget: int = config.BUDGET_CHARS) -> str:
    """The user's messages, the agent's text and one line per tool call, newest kept first when over budget: tool lines
    go first, then older agent text, then older messages from the user."""
    items = []
    for e in events:
        stamp = datetime.fromtimestamp(e.ts).strftime('%a %d %b %H:%M')
        if e.kind == 'tool':
            items.append([e.kind, f'  · {_clip(e.text, LIMITS["tool"])}', stamp])
        else:
            items.append([e.kind, f'[{stamp}] {LABEL[e.kind]}: {_clip(e.text, LIMITS[e.kind])}', stamp])

    def total():
        return sum(len(i[1]) + 1 for i in items if i is not None)

    def drop(kinds, shrink=False):
        for idx, item in enumerate(items):
            if total() <= budget:
                return
            if item is None or item[0] not in kinds:
                continue
            if shrink:
                e = events[idx]
                item[1] = f'[{item[2]}] {LABEL[e.kind]}: {_clip(e.text, SHRUNK[e.kind])}'
            else:
                items[idx] = None

    drop({'tool'})
    drop({'assistant', 'peer'}, shrink=True)
    drop({'assistant', 'peer'})
    drop({'human'}, shrink=True)
    drop({'human'})

    lines, gap = [], 0
    for item in items:
        if item is None:
            gap += 1
            continue
        if gap:
            lines.append(f'  [… {gap} earlier lines left out …]')
            gap = 0
        lines.append(item[1])
    # The "left out" markers can push it over: trim the oldest lines.
    while len(lines) > 1 and sum(len(line) + 1 for line in lines) > budget:
        lines.pop(0)
    return '\n'.join(lines)


def chunk_by_period(events: list[Event], daily_days: int = 30) -> list[list[Event]]:
    """One chunk per local day for the last `daily_days` days, one per ISO week before that: each chunk is one
    summary call and one point on the session's timeline."""
    chunks: list[list[Event]] = []
    key = None
    cutoff = time.time() - daily_days * 86400
    for e in events:
        d = datetime.fromtimestamp(e.ts)
        k = d.date() if e.ts >= cutoff else d.isocalendar()[:2]
        if k != key:
            chunks.append([])
            key = k
        chunks[-1].append(e)
    return chunks
