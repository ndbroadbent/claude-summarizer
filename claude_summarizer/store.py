"""The vault files, index.json and the byte-offset state.

~/Notes/Sessions/<folder>/<YYYY-MM-DDTHH>.md is one update, named by the local hour of the activity it covers
(floored), holding the sentence, paragraph and page. <folder> is the session's herdr tab name, or
"<directory name>-<session ID prefix>" for a session no tab holds. index.json lists every session with its latest
sentence and paragraph.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import time
from datetime import datetime

from . import config


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp{os.getpid()}'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path: str, data) -> None:
    _atomic_write(path, json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + '\n')


class Lock:
    """One run at a time: the hourly job skips an hour while the bootstrap (or a long run) still holds it."""

    def __enter__(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        self.f = open(config.LOCK_FILE, 'w')
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close()
            raise SystemExit('another claude-summarizer run holds the lock; skipping')
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# ---------------------------------------------------------------- index + folders

def load_index() -> dict:
    return load_json(config.INDEX_FILE, {'sessions': {}})


def save_index(index: dict) -> None:
    index['generated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    save_json(config.INDEX_FILE, index)


def safe_name(name: str) -> str:
    name = re.sub(r'[/\\:*?"<>|#^\[\]]', '-', name).strip(' .')
    return name or 'unnamed'


def default_folder(entry: dict) -> str:
    base = os.path.basename((entry.get('dir') or '').rstrip('/')) or entry['agent']
    return safe_name(f"{base}-{entry['session_id'][:8]}")


def resume_command(entry: dict) -> str:
    d = shlex.quote(entry.get('dir') or config.HOME)
    if entry['agent'] == 'codex':
        return f"cd {d} && codex resume {entry['session_id']}"
    return f"cd {d} && claude --resume {entry['session_id']}"


def _move(old: str, new: str) -> None:
    src, dst = os.path.join(config.VAULT_DIR, old), os.path.join(config.VAULT_DIR, new)
    if not os.path.isdir(src) or src == dst:
        return
    if not os.path.exists(dst):
        os.rename(src, dst)
        return
    for name in os.listdir(src):  # a leftover folder by that name: merge into it
        shutil.move(os.path.join(src, name), os.path.join(dst, name))
    os.rmdir(src)


def reconcile(index: dict, tabs: dict[str, dict]) -> None:
    """Refresh tab names from herdr and give each session its folder. A tab's current session owns the tab's name;
    a session that left the tab (after /clear, say) moves to "<tab>-<session ID prefix>" and keeps its old tab name
    as `former_tab`."""
    sessions = index['sessions']
    for sid, e in sessions.items():
        t = tabs.get(sid)
        if t:
            e.update(tab=t['tab'], running=t['running'], pane=t['pane'], in_herdr=True)
            e.pop('former_tab', None)
        else:
            if e.get('in_herdr') and e.get('tab'):
                e['former_tab'] = e['tab']
            e.update(running=False, pane=None, in_herdr=False)
            e.pop('tab', None)
        e['resume'] = resume_command(e)

    claimed: dict[str, str] = {}
    for sid, e in sorted(sessions.items(), key=lambda kv: -(kv[1].get('last_activity') or 0)):
        if e.get('in_herdr'):
            want = safe_name(e['tab'])
            if want in claimed:
                want = safe_name(f"{e['tab']}-{sid[:8]}")
            claimed[want] = sid
    wanted: dict[str, str] = {sid: name for name, sid in claimed.items()}
    for sid, e in sessions.items():
        if sid in wanted:
            continue
        want = e.get('folder') or default_folder(e)
        if want in claimed and claimed[want] != sid:
            want = safe_name(f"{e.get('former_tab') or want}-{sid[:8]}")
        wanted[sid] = want
    # Evictions first, so a tab's name is free before its new session takes it.
    order = sorted(sessions, key=lambda s: 0 if not sessions[s].get('in_herdr') else 1)
    for sid in order:
        e = sessions[sid]
        if e.get('folder') and e['folder'] != wanted[sid]:
            _move(e['folder'], wanted[sid])
            if e.get('latest_page'):
                e['latest_page'] = os.path.join(wanted[sid], os.path.basename(e['latest_page']))
        e['folder'] = wanted[sid]


# ---------------------------------------------------------------- page files

def hour_name(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H')


def write_update(entry: dict, summary, activity_from: float, activity_to: float) -> str:
    name = hour_name(activity_to)
    rel = os.path.join(entry['folder'], f'{name}.md')
    front = {
        'session_id': entry['session_id'],
        'tab': entry.get('tab') or entry.get('former_tab') or '',
        'agent': entry['agent'],
        'directory': entry.get('dir') or '',
        'activity_from': datetime.fromtimestamp(activity_from).astimezone().isoformat(timespec='minutes'),
        'activity_to': datetime.fromtimestamp(activity_to).astimezone().isoformat(timespec='minutes'),
        'written': datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    lines = ['---'] + [f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in front.items()] + ['---', '']
    body = f"## Sentence\n\n{summary.sentence}\n\n## Paragraph\n\n{summary.paragraph}\n\n## Page\n\n{summary.page}\n"
    _atomic_write(os.path.join(config.VAULT_DIR, rel), '\n'.join(lines) + body)
    return rel


SECTION = re.compile(r'^## (Sentence|Paragraph|Page)\s*$', re.M)


def read_update(rel: str) -> dict:
    with open(os.path.join(config.VAULT_DIR, rel)) as f:
        text = f.read()
    front = {}
    if text.startswith('---\n'):
        head, _, text = text[4:].partition('\n---\n')
        for line in head.splitlines():
            key, _, value = line.partition(': ')
            try:
                front[key] = json.loads(value)
            except json.JSONDecodeError:
                front[key] = value
    parts = SECTION.split(text)
    tiers = {parts[i].lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    return {'file': rel, 'name': os.path.basename(rel)[:-3], **front, **tiers}


def list_updates(folder: str) -> list[str]:
    d = os.path.join(config.VAULT_DIR, folder)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(folder, n) for n in os.listdir(d) if re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d\.md', n))


def log_call(record: dict) -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    record = {'at': datetime.now().astimezone().isoformat(timespec='seconds'), **record}
    with open(config.CALLS_LOG, 'a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def now() -> float:
    return time.time()
