"""Pick the Claude account for a headless call from the claude-account-rotator coordinator.

The rule is the coordinator's own "soonest reset": of the signed-in accounts that aren't spent (the coordinator marks an
account spent at 98% of its week), use the one whose week resets first, so room that's about to refill is used before
it does. Summaries run on Sonnet, so only the account's week counts, not Fable's own week.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime

from . import config


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 10):
        super().__init__('coordinator', timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def coordinator_state() -> dict:
    conn = _UnixConnection(config.COORDINATOR_SOCKET)
    try:
        conn.request('GET', '/state', headers={'Connection': 'close'})
        res = conn.getresponse()
        body = res.read()
        if res.status != 200:
            raise RuntimeError(f'coordinator /state answered {res.status}: {body[:200]!r}')
        return json.loads(body)
    finally:
        conn.close()


@dataclass
class Account:
    id: str
    slot: str  # '' is Claude Code's own login
    email: str
    week_pct: float
    resets_at: float | None

    def env(self) -> dict[str, str]:
        # The same variables the coordinator uses to point a `claude` child at a login (hooks/model.ts slotEnv): an
        # added account keeps its config in its slot too, so a run never touches ~/.claude.json's record of Claude
        # Code's own login. CLAUDE_ACCOUNT_ROTATOR_REFRESH keeps the rotator mod out of the run (it otherwise registers
        # the run as a session and may move it to another account); --setting-sources "" keeps plugins, hooks and
        # CLAUDE.md out as well.
        env = {'CLAUDE_SECURESTORAGE_CONFIG_DIR': self.slot, 'CLAUDE_ACCOUNT_ROTATOR_REFRESH': '1'}
        if self.slot:
            env['CLAUDE_CONFIG_DIR'] = self.slot
        return env

    def label(self) -> str:
        if self is DEFAULT_LOGIN or self.id == 'default':
            return self.email
        return f'account {self.id} ({self.email}, week {self.week_pct:.0f}%)'


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


DEFAULT_LOGIN = Account('default', '', "Claude Code's own login", 0, None)


def choose(state: dict | None = None) -> Account | None:
    """None when every account is spent. Without the coordinator installed, Claude Code's own login."""
    if state is None and not os.path.exists(config.COORDINATOR_SOCKET):
        return DEFAULT_LOGIN
    state = state or coordinator_state()
    now = time.time()
    usable = []
    for a in state.get('accounts', []):
        if (a.get('health') or {}).get('kind') != 'ok' or a.get('isSpent') or a.get('isRetiring'):
            continue
        week = (a.get('reading') or {}).get('sevenDay')
        if not week:
            continue
        resets = _epoch(week.get('resetsAt'))
        pct = 0 if resets is not None and resets <= now else float(week.get('pct', 0))
        if resets is not None and resets <= now:
            resets = None  # reset already; its next reset is a week out, which we don't know
        usable.append(Account(a['id'], a.get('slot', ''), a.get('email', '?'), pct, resets))
    if not usable:
        return None
    usable.sort(key=lambda a: (a.resets_at if a.resets_at is not None else now + 7 * 86400, a.week_pct))
    return usable[0]
