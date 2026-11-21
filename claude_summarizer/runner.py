"""The hourly run (and the bootstrap, which is the same run over every transcript from the start).

For each transcript that grew since its stored byte offset: read the new records, drop the noise, split what's left by
local day, and for each day with real activity make one summary call that rewrites the session's page. Offsets advance
only after a day's call succeeds, so a failed call is retried next run.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import accounts, config, herdr, store, summarise, transcripts

MAX_FAILURES = 3  # a day that fails this many runs in a row is skipped, so one bad chunk can't block a session


@dataclass
class Totals:
    files_read: int = 0
    chunks_idle: int = 0
    calls: int = 0
    updates: int = 0
    no_change: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    sessions: set = field(default_factory=set)

    def line(self) -> str:
        return (f'{self.files_read} transcripts read, {self.chunks_idle} idle chunks skipped, {self.calls} calls '
                f'({self.updates} updates, {self.no_change} no_change, {self.failures} failed) for '
                f'{len(self.sessions)} sessions; {self.input_tokens:,} in / {self.output_tokens:,} out tokens, '
                f'{self.seconds:.0f}s, notional ${self.cost_usd:.2f}')


class StopRun(Exception):
    pass


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _span(chunk) -> str:
    a, b = datetime.fromtimestamp(chunk[0].ts), datetime.fromtimestamp(chunk[-1].ts)
    end = b.strftime('%H:%M') if a.date() == b.date() else b.strftime('%a %d %b %H:%M')
    return f"{a.strftime('%a %d %b %Y %H:%M')} to {end}"


def _entry(index: dict, tabs: dict, sid: str, agent: str, cwd: str | None, path: str) -> dict:
    e = index['sessions'].get(sid)
    if e is None:
        e = index['sessions'][sid] = {'session_id': sid, 'agent': agent, 'dir': cwd, 'transcripts': []}
        store.reconcile(index, tabs)
    if path not in e['transcripts']:
        e['transcripts'].append(path)
    if cwd and not e.get('dir'):
        e['dir'] = cwd
    return e


def process(source, state, index, tabs, totals: Totals, opts) -> None:
    files = state.setdefault('files', {})
    st = files.get(source.path, {})
    offset = st.get('offset', 0)
    size = os.path.getsize(source.path)
    if size < offset:
        offset = 0  # rewritten
    if size == offset or st.get('skip'):
        return
    res = transcripts.read(source, offset)
    totals.files_read += 1
    if res.skip or not res.session_id:
        files[source.path] = {'offset': res.end, 'skip': res.skip or 'no session ID'}
        return

    for chunk in transcripts.chunk_by_period(res.events):
        chunk_end = chunk[-1].end
        if not transcripts.is_real(chunk):
            totals.chunks_idle += 1
            files[source.path] = {'offset': chunk_end}
            continue
        entry = _entry(index, tabs, res.session_id, source.agent, res.cwd, source.path)
        real_ts = transcripts.last_real_ts(chunk) or chunk[-1].ts
        if opts.dry_run:
            totals.calls += 1
            totals.sessions.add(res.session_id)
            continue
        if opts.max_calls is not None and totals.calls >= opts.max_calls:
            raise StopRun(f'reached --max-calls {opts.max_calls}')
        account = accounts.choose()
        if account is None:
            raise StopRun('no account has room left this week')
        updates = store.list_updates(entry['folder'])
        current = store.read_update(updates[-1]) if updates else None
        prompt = summarise.build_prompt(
            entry, current and current.get('page'), current and current.get('name'), transcripts.condense(chunk),
            _span(chunk))
        totals.calls += 1
        totals.sessions.add(res.session_id)
        label = entry.get('tab') or entry['folder']
        try:
            summary, stats = summarise.call(prompt, account)
        except summarise.CallError as error:
            totals.failures += 1
            fails = st.get('failures', 0) + 1 if st.get('failed_at') == chunk_end else 1
            store.log_call({'session': res.session_id, 'tab': label, 'error': str(error), 'account': account.id})
            log(f'  {label}: call failed ({fails}/{MAX_FAILURES}) on {account.label()}: {error}')
            if 'limit' in str(error).lower():
                raise StopRun(f'usage limit: {error}')
            if fails >= MAX_FAILURES:
                files[source.path] = {'offset': chunk_end}
                log(f'  {label}: skipping that day after {fails} failures')
                continue
            files[source.path] = {'offset': files.get(source.path, {}).get('offset', offset), 'failed_at': chunk_end,
                                  'failures': fails}
            return
        totals.input_tokens += stats['input_tokens']
        totals.output_tokens += stats['output_tokens']
        totals.cost_usd += stats['cost_usd'] or 0
        totals.seconds += stats['seconds']
        entry['last_activity'] = max(entry.get('last_activity') or 0, real_ts)
        if summary.status == 'update':
            totals.updates += 1
            rel = store.write_update(entry, summary, chunk[0].ts, real_ts)
            entry.update(sentence=summary.sentence, paragraph=summary.paragraph, latest_page=rel,
                         updated=time.time())
        else:
            totals.no_change += 1
        store.log_call({'session': res.session_id, 'tab': label, 'status': summary.status, 'span': _span(chunk),
                        'events': len(chunk), 'prompt_chars': len(prompt), **stats})
        log(f'  {label}: {summary.status} ({_span(chunk)}, {len(prompt):,} chars, {stats["input_tokens"]:,} tokens, '
            f'{stats["seconds"]}s, {account.label()})')
        files[source.path] = {'offset': chunk_end}
        store.save_json(config.STATE_FILE, state)
        store.save_index(index)
        if opts.sleep:
            time.sleep(opts.sleep)
    files[source.path] = {'offset': res.end}


def run(opts) -> Totals:
    totals = Totals()
    with store.Lock():
        state = store.load_json(config.STATE_FILE, {'files': {}})
        index = store.load_index()
        tabs = herdr.session_tabs()
        store.reconcile(index, tabs)
        sources = transcripts.discover()
        # Newest first, so the sessions that matter now get their pages first if a run stops early.
        sources.sort(key=lambda s: os.path.getmtime(s.path), reverse=True)
        if opts.only:
            sources = [s for s in sources if any(o in s.path for o in opts.only)]
        log(f'run started: {len(sources)} transcripts in the last {config.MAX_AGE_DAYS} days'
            f"{' (dry run)' if opts.dry_run else ''}")
        try:
            for source in sources:
                try:
                    process(source, state, index, tabs, totals, opts)
                except OSError as error:
                    log(f'  {source.path}: {error}')
                if not opts.dry_run:
                    store.save_json(config.STATE_FILE, state)
        except StopRun as stop:
            log(f'stopping early: {stop}')
        finally:
            if not opts.dry_run:
                store.reconcile(index, tabs)
                store.save_index(index)
                store.save_json(config.STATE_FILE, {**state, 'last_run': time.time()})
                store.log_call({'run': totals.line()})
    log(f'run finished: {totals.line()}')
    return totals
