"""Map agent session IDs to herdr tabs (https://github.com/herdrdev/herdr). Without herdr, every lookup is empty."""
from __future__ import annotations

import json
import os
import subprocess

from . import config


def _herdr(*args) -> dict:
    try:
        out = subprocess.run([os.environ.get('CLAUDE_SUMMARIZER_HERDR', 'herdr'), *args], capture_output=True, text=True,
                             check=True, timeout=20).stdout
        return json.loads(out).get('result') or {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def session_tabs() -> dict[str, dict]:
    """session ID -> {tab, pane, running, cwd}. Tabs whose agent isn't running come from herdr's saved session."""
    tabs: dict[str, dict] = {}
    if os.path.exists(config.HERDR_SESSION_FILE):
        with open(config.HERDR_SESSION_FILE) as f:
            for ws in json.load(f).get('workspaces', []):
                for tab in ws.get('tabs', []):
                    for pane in (tab.get('panes') or {}).values():
                        sid = (pane.get('agent_session') or {}).get('value')
                        if sid and tab.get('custom_name'):
                            tabs[sid] = {'tab': tab['custom_name'], 'pane': None, 'running': False, 'cwd': pane.get('cwd')}
    labels = {t['tab_id']: t.get('label') for t in _herdr('tab', 'list').get('tabs', [])}
    for agent in _herdr('agent', 'list').get('agents', []):
        sid = (agent.get('agent_session') or {}).get('value')
        label = labels.get(agent.get('tab_id'))
        if sid and label:
            tabs[sid] = {'tab': label, 'pane': agent.get('pane_id'), 'running': True, 'cwd': agent.get('cwd')}
    return tabs
