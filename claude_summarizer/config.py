"""Paths and limits.

Optional settings live in ~/.config/claude-summarizer/config.json (or $CLAUDE_SUMMARIZER_CONFIG):

  {"vault_dir": "~/Notes/Sessions", "user_name": "Sam", "model": "sonnet", "port": 8790}

Keep the vault somewhere private: summaries say what each session worked on, which can include money, people and
customers.
"""
from __future__ import annotations

import json
import os

HOME = os.path.expanduser('~')
CONFIG_FILE = os.environ.get('CLAUDE_SUMMARIZER_CONFIG', os.path.join(HOME, '.config', 'claude-summarizer', 'config.json'))
try:
    with open(CONFIG_FILE) as _f:
        _settings = json.load(_f)
except FileNotFoundError:
    _settings = {}


def _setting(key: str, env: str, default):
    value = os.environ.get(env) or _settings.get(key) or default
    return os.path.expanduser(value) if isinstance(value, str) and value.startswith('~') else value


VAULT_DIR = _setting('vault_dir', 'CLAUDE_SUMMARIZER_VAULT', os.path.join(HOME, '.local', 'share', 'claude-summarizer'))
INDEX_FILE = os.path.join(VAULT_DIR, 'index.json')
STATE_DIR = _setting('state_dir', 'CLAUDE_SUMMARIZER_STATE', os.path.join(HOME, '.local', 'state', 'claude-summarizer'))
STATE_FILE = os.path.join(STATE_DIR, 'state.json')
CALLS_LOG = os.path.join(STATE_DIR, 'calls.jsonl')
LOCK_FILE = os.path.join(STATE_DIR, 'lock')
WORK_DIR = os.path.join(STATE_DIR, 'work')  # cwd for headless claude runs: no project, no CLAUDE.md

USER_NAME = _setting('user_name', 'CLAUDE_SUMMARIZER_USER', 'the user')
MODEL = _setting('model', 'CLAUDE_SUMMARIZER_MODEL', 'sonnet')
WEB_PORT = int(_setting('port', 'CLAUDE_SUMMARIZER_PORT', 8790))

CLAUDE_PROJECTS = os.path.join(HOME, '.claude', 'projects')
CODEX_SESSIONS = os.path.join(HOME, '.codex', 'sessions')
# Optional: github.com/ndbroadbent/claude-account-rotator. Without it, calls use Claude Code's own login.
COORDINATOR_SOCKET = os.path.join(HOME, '.claude', 'account-rotator', 'coordinator.sock')
# Optional: herdr (terminal tabs for agents). Without it, sessions are named after their directory.
HERDR_SESSION_FILE = os.path.join(HOME, '.config', 'herdr', 'session.json')

CALL_TIMEOUT_S = 420

# Ageing: the reader picks a tier from the session's last real activity.
PARAGRAPH_DAYS = 30
SENTENCE_DAYS = 183
# Transcripts last modified longer ago than this are never read.
MAX_AGE_DAYS = 183

# Condensed activity sent per summary call, in characters (about 4 per token).
BUDGET_CHARS = 48_000
# The filter: a chunk with no message from the user (or another session) needs this much assistant text, or a file
# edit, before it's worth a call.
MIN_ASSISTANT_CHARS = 300
