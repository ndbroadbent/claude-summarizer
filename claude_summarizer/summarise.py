"""One headless `claude -p` call: the session's current page plus its new activity in, a new page (or no_change) out."""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass

from . import config
from .accounts import Account

SYSTEM_PROMPT = """\
You keep the running record of one of {user}'s AI coding agent sessions. They run many of them at once, often one \
per area of work, in terminal tabs named after that work. Your record lets {user} (and other agents) see at a glance \
what each session is about, where it stands, and what it's waiting on.

You get the session's current page (empty for a new session) and a condensed log of its new activity: {user}'s \
messages, messages forwarded from their other sessions, the agent's replies, and one line per tool call. Long logs \
have older lines left out.

Answer with JSON.
- If the new activity changes nothing worth recording (a background check that found nothing, a watcher wake-up, \
"still waiting", a resumed session that did nothing, routine chatter), answer {"status": "no_change"}.
- Otherwise answer {"status": "update", "page": ..., "paragraph": ..., "sentence": ...}, rewriting all three from the \
current page plus the new activity:
  - page: Markdown, at most about one page (450 words), shorter when there's less to say. Start with what the \
session is for and its main thread of work, then what happened recently (newest last, with dates such as "21 Nov"), \
then what's open: waiting on {user}, on CI or a deploy, on someone else, or next steps. Keep the identifiers someone \
would search for: Shortcut stories (sc-1234), PR numbers, branches, repos, services, customers' company names, \
Intercom or email subjects. Fold older history into a sentence or two as it ages; don't let the page become a log.
  - paragraph: one or two short paragraphs: what the session is for, where it stands now, what's open.
  - sentence: one sentence (under 30 words) naming the session's focus and current state.
Write plainly, past tense for what happened, present for where it stands. Say "the session" or \
"the agent", not "I".
Never copy secrets (tokens, passwords, keys), bank or card numbers, or personal data about end users (people who \
fill in or sign customers' forms): refer to records by their IDs instead.
"""

SCHEMA = {
    'type': 'object',
    'properties': {
        'status': {'type': 'string', 'enum': ['update', 'no_change']},
        'page': {'type': 'string'},
        'paragraph': {'type': 'string'},
        'sentence': {'type': 'string'},
    },
    'required': ['status'],
}


@dataclass
class Summary:
    status: str
    page: str = ''
    paragraph: str = ''
    sentence: str = ''


class CallError(RuntimeError):
    pass


def build_prompt(meta: dict, current_page: str | None, current_ts: str | None, activity: str, span: str) -> str:
    head = (
        f"Session: tab \"{meta.get('tab') or '(no tab)'}\", agent {meta['agent']}, directory {meta.get('dir') or '?'}, "
        f"session ID {meta['session_id']}."
    )
    page = f'Current page (written {current_ts}):\n\n{current_page}' if current_page else 'Current page: none yet (first summary of this session).'
    return f'{head}\n\n{page}\n\n---\nNew activity ({span}):\n\n{activity}\n'


def call(prompt: str, account: Account) -> tuple[Summary, dict]:
    os.makedirs(config.WORK_DIR, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
    env.update(account.env())
    cmd = [
        os.environ.get('CLAUDE_SUMMARIZER_CLAUDE', 'claude'), '-p',
        '--model', config.MODEL,
        '--tools', '',
        '--strict-mcp-config',
        '--no-session-persistence',
        '--disable-slash-commands',
        '--setting-sources', '',
        '--output-format', 'json',
        '--system-prompt', SYSTEM_PROMPT.replace('{user}', config.USER_NAME),
        '--json-schema', json.dumps(SCHEMA),
    ]
    started = time.time()
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, cwd=config.WORK_DIR,
                              timeout=config.CALL_TIMEOUT_S)
    except subprocess.TimeoutExpired as error:
        raise CallError(f'claude -p timed out after {config.CALL_TIMEOUT_S}s') from error
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise CallError(f'claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:400]}') from error
    usage = out.get('usage') or {}
    stats = {
        'seconds': round(time.time() - started, 1),
        'input_tokens': usage.get('input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
        + usage.get('cache_read_input_tokens', 0),
        'output_tokens': usage.get('output_tokens', 0),
        'cost_usd': out.get('total_cost_usd'),
        'account': account.id,
    }
    if out.get('is_error') or proc.returncode != 0:
        raise CallError(f"claude -p failed ({out.get('subtype')}): {str(out.get('result'))[:400]}")
    data = out.get('structured_output')
    if not isinstance(data, dict):
        try:
            data = json.loads(out.get('result') or '')
        except json.JSONDecodeError as error:
            raise CallError(f"no structured output: {str(out.get('result'))[:300]}") from error
    summary = Summary(**{k: (data.get(k) or '').strip() for k in ('status', 'page', 'paragraph', 'sentence')})
    if summary.status == 'update' and not (summary.page and summary.paragraph and summary.sentence):
        raise CallError('update without all three tiers')
    if summary.status not in ('update', 'no_change'):
        raise CallError(f'unknown status {summary.status!r}')
    return summary, stats
