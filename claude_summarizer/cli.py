"""claude-summarizer: rolling, tiered summaries of your Claude Code and Codex sessions.

  claude-summarizer run [--dry-run] [--max-calls N] [--only TEXT]   the hourly job (launchd runs it)
  claude-summarizer bootstrap [--dry-run] [--max-calls N] [--sleep S]   the first pass over the last 6 months
  claude-summarizer digest [--json]      paragraphs for the last 30 days, sentences for 30 days to 6 months
  claude-summarizer page <tab or session ID prefix> [--all]   the latest page (or every update) of one session
  claude-summarizer serve [--port N]     the web UI on localhost
  claude-summarizer status               accounts, last run, call totals
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from . import accounts, config, runner, store


def _ago(ts: float | None) -> str:
    if not ts:
        return 'never'
    return datetime.fromtimestamp(ts).strftime('%a %d %b %H:%M')


def tier(entry: dict, now: float | None = None) -> str | None:
    age_days = ((now or time.time()) - (entry.get('last_activity') or 0)) / 86400
    if age_days <= config.PARAGRAPH_DAYS:
        return 'paragraph'
    if age_days <= config.SENTENCE_DAYS:
        return 'sentence'
    return None


def _name(e: dict) -> str:
    if e.get('tab'):
        return e['tab']
    if e.get('former_tab'):
        return f"{e['folder']} (was tab {e['former_tab']})"
    return e['folder']


def digest(args) -> None:
    index = store.load_index()
    sessions = [e for e in index['sessions'].values() if e.get('sentence')]
    sessions.sort(key=lambda e: -(e.get('last_activity') or 0))
    now = time.time()
    if args.json:
        out = []
        for e in sessions:
            t = tier(e, now)
            if t:
                out.append({k: e.get(k) for k in ('session_id', 'tab', 'former_tab', 'folder', 'agent', 'dir', 'resume',
                                                  'running', 'pane', 'last_activity', 'latest_page')}
                           | {'tier': t, 'summary': e[t]})
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return
    print(f"# Session digest ({datetime.now().strftime('%a %d %b %Y %H:%M')})\n")
    print(f'Pages: {config.VAULT_DIR}/<folder>/<hour>.md, or `claude-summarizer page <tab>`.\n')
    print(f'## Active in the last {config.PARAGRAPH_DAYS} days\n')
    for e in sessions:
        if tier(e, now) != 'paragraph':
            continue
        where = (e.get('dir') or '').replace(config.HOME, '~')
        state = 'running' if e.get('running') else 'not running'
        print(f"### {_name(e)}\nLast active {_ago(e.get('last_activity'))} · {e['agent']} · {state} · {where} · "
              f"{e['session_id']}\n\n{e['paragraph']}\n")
    older = [e for e in sessions if tier(e, now) == 'sentence']
    if older:
        print(f'## {config.PARAGRAPH_DAYS} days to 6 months ago\n')
        for e in older:
            print(f"- **{_name(e)}** ({datetime.fromtimestamp(e['last_activity']).strftime('%d %b')}): {e['sentence']}")


def find_session(index: dict, needle: str) -> dict:
    sessions = list(index['sessions'].values())
    for test in (
        lambda e: e.get('tab') == needle,
        lambda e: e['session_id'].startswith(needle),
        lambda e: e.get('folder') == needle,
        lambda e: needle.lower() in (e.get('tab') or e.get('folder') or '').lower(),
    ):
        hits = [e for e in sessions if test(e)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ', '.join(sorted(_name(e) for e in hits))
            raise SystemExit(f'"{needle}" matches several sessions: {names}')
    raise SystemExit(f'no session matches "{needle}"')


def page(args) -> None:
    e = find_session(store.load_index(), args.session)
    updates = store.list_updates(e['folder'])
    if not updates:
        raise SystemExit(f'{_name(e)} has no pages yet')
    print(f"# {_name(e)}\n{e['agent']} · {e['session_id']} · `{e['resume']}`\n")
    if args.all:
        for rel in updates:
            u = store.read_update(rel)
            print(f"- {u['name']}: {u.get('sentence', '')}")
        print()
    u = store.read_update(updates[-1])
    print(f"Latest update: {os.path.join(config.VAULT_DIR, u['file'])}\n\n{u.get('page', '')}")


def status(args) -> None:
    state = store.load_json(config.STATE_FILE, {})
    index = store.load_index()
    print(f"Last run: {_ago(state.get('last_run'))}")
    print(f"Transcripts tracked: {len(state.get('files', {}))}; sessions with pages: "
          f"{sum(1 for e in index['sessions'].values() if e.get('sentence'))}")
    try:
        chosen = accounts.choose()
        print(f"Next call would use: {chosen.label() if chosen else 'nothing (every account is spent)'}")
    except OSError as error:
        print(f'Coordinator unreachable: {error}')
    if os.path.exists(config.CALLS_LOG):
        with open(config.CALLS_LOG) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        runs = [r for r in rows if 'run' in r][-5:]
        print('\nRecent runs:')
        for r in runs:
            print(f"  {r['at']}: {r['run']}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog='claude-summarizer', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('run', 'bootstrap'):
        r = sub.add_parser(name)
        r.add_argument('--dry-run', action='store_true', help='count the calls a run would make; change nothing')
        r.add_argument('--max-calls', type=int, help='stop after this many summary calls')
        r.add_argument('--sleep', type=float, default=5.0 if name == 'bootstrap' else 0.0,
                       help='seconds between calls')
        r.add_argument('--only', action='append', help='only transcripts whose path contains this (repeatable)')
    d = sub.add_parser('digest')
    d.add_argument('--json', action='store_true')
    g = sub.add_parser('page')
    g.add_argument('session')
    g.add_argument('--all', action='store_true', help='list every update\'s sentence first')
    s = sub.add_parser('serve')
    s.add_argument('--port', type=int, default=config.WEB_PORT)
    sub.add_parser('status')
    args = p.parse_args(argv)
    if args.cmd in ('run', 'bootstrap'):
        runner.run(args)
    elif args.cmd == 'digest':
        digest(args)
    elif args.cmd == 'page':
        page(args)
    elif args.cmd == 'serve':
        from . import web
        web.serve(args.port)
    elif args.cmd == 'status':
        status(args)


if __name__ == '__main__':
    main(sys.argv[1:])
