"""A small local web UI over the vault files: http://localhost:8790"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from . import config, store


def _tier(e: dict, now: float) -> str:
    age = (now - (e.get('last_activity') or 0)) / 86400
    return 'paragraph' if age <= config.PARAGRAPH_DAYS else 'sentence' if age <= config.SENTENCE_DAYS else 'old'


def _sessions() -> list[dict]:
    now = time.time()
    out = []
    for e in store.load_index()['sessions'].values():
        if not e.get('sentence'):
            continue
        out.append({k: e.get(k) for k in ('session_id', 'tab', 'former_tab', 'folder', 'agent', 'dir', 'resume',
                                          'running', 'last_activity', 'sentence', 'paragraph')}
                   | {'tier': _tier(e, now), 'updates': len(store.list_updates(e['folder']))})
    out.sort(key=lambda e: -(e.get('last_activity') or 0))
    return out


def _session(sid: str) -> dict | None:
    e = store.load_index()['sessions'].get(sid)
    if not e:
        return None
    updates = []
    for rel in store.list_updates(e['folder']):
        u = store.read_update(rel)
        updates.append({k: u.get(k) for k in ('name', 'sentence', 'activity_from', 'activity_to')})
    return {'session': {k: e.get(k) for k in ('session_id', 'tab', 'former_tab', 'folder', 'agent', 'dir', 'resume',
                                              'running', 'last_activity')}, 'updates': updates}


def _update(sid: str, name: str) -> dict | None:
    e = store.load_index()['sessions'].get(sid)
    if not e or '/' in name or '..' in name:
        return None
    rel = os.path.join(e['folder'], f'{name}.md')
    if not os.path.exists(os.path.join(config.VAULT_DIR, rel)):
        return None
    u = store.read_update(rel)
    u['path'] = os.path.join(config.VAULT_DIR, rel)
    return u


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data) -> None:
        if data is None:
            self._send(404, b'{"error":"not found"}', 'application/json')
        else:
            self._send(200, json.dumps(data, ensure_ascii=False).encode(), 'application/json')

    def do_GET(self):
        parts = [unquote(p) for p in self.path.split('?')[0].strip('/').split('/') if p]
        if not parts:
            self._send(200, PAGE.encode(), 'text/html; charset=utf-8')
        elif parts == ['api', 'sessions']:
            self._json(_sessions())
        elif len(parts) == 3 and parts[:2] == ['api', 'session']:
            self._json(_session(parts[2]))
        elif len(parts) == 4 and parts[:2] == ['api', 'update']:
            self._json(_update(parts[2], parts[3]))
        else:
            self._send(404, b'not found', 'text/plain')


def serve(port: int) -> None:
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'claude-summarizer UI on http://localhost:{port}', flush=True)
    server.serve_forever()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Summarizer</title>
<style>
:root {
  --bg: #f7f6f3; --panel: #ffffff; --text: #1d1d1f; --muted: #6b6b70; --line: #e4e2dc; --accent: #2f5bd3;
  --chip: #eeece6; --live: #1f8a4c; --hover: #f1efe9; --code: #f0eee8;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #151517; --panel: #1d1d20; --text: #ececee; --muted: #9a9aa2; --line: #2e2e33; --accent: #7fa2ff;
          --chip: #2a2a2f; --live: #4cc17f; --hover: #25252a; --code: #26262b; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif; }
header { position: sticky; top: 0; z-index: 2; background: var(--bg); border-bottom: 1px solid var(--line);
         padding: 14px 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
header h1 { font-size: 17px; margin: 0; font-weight: 650; letter-spacing: -0.01em; cursor: pointer; }
header .count { color: var(--muted); font-size: 13px; }
#q { flex: 1; min-width: 180px; max-width: 420px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 8px;
     background: var(--panel); color: var(--text); font: inherit; }
main { max-width: 1100px; margin: 0 auto; padding: 12px 16px 60px; }
h2.group { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 22px 0 8px; }
.row { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; margin-bottom: 6px; }
.row .top { display: grid; grid-template-columns: minmax(130px, 200px) 1fr auto; gap: 14px; padding: 10px 12px;
            cursor: pointer; align-items: baseline; }
.row .top:hover { background: var(--hover); border-radius: 10px; }
.name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.name .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--live);
             margin-right: 6px; vertical-align: 2px; }
.name .dot.off { background: transparent; border: 1px solid var(--muted); }
.sentence { color: var(--text); }
.when { color: var(--muted); font-size: 13px; white-space: nowrap; }
.more { display: none; padding: 0 12px 12px; border-top: 1px solid var(--line); }
.row.open .more { display: block; }
.meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; color: var(--muted); font-size: 13px; margin: 10px 0; }
.chip { background: var(--chip); border-radius: 6px; padding: 2px 7px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 12px; }
button { font: inherit; font-size: 13px; border: 1px solid var(--line); background: var(--panel); color: var(--text);
         border-radius: 7px; padding: 4px 10px; cursor: pointer; }
button:hover { background: var(--hover); }
button:disabled { opacity: 0.4; cursor: default; }
button.primary { border-color: var(--accent); color: var(--accent); }
.paragraph p { margin: 0 0 8px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.view { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 18px 22px; margin-top: 12px; }
.view h1 { font-size: 21px; line-height: 1.3; margin: 0 0 6px; }
.md h1 { font-size: 19px; } .md h2 { font-size: 16px; margin: 18px 0 6px; } .md h3 { font-size: 15px; margin: 14px 0 4px; }
.md ul { padding-left: 20px; margin: 6px 0; } .md li { margin: 3px 0; }
.md code { background: var(--code); border-radius: 4px; padding: 1px 4px; font-size: 13px; }
.nav { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0 14px; }
.nav .stamp { color: var(--muted); font-size: 13px; margin: 0 6px; }
.timeline { list-style: none; padding: 0; margin: 10px 0 0; border-left: 2px solid var(--line); }
.timeline li { padding: 6px 0 6px 16px; position: relative; cursor: pointer; }
.timeline li::before { content: ""; position: absolute; left: -6px; top: 13px; width: 10px; height: 10px; border-radius: 50%;
                       background: var(--panel); border: 2px solid var(--muted); }
.timeline li.cur::before { background: var(--accent); border-color: var(--accent); }
.timeline li:hover { background: var(--hover); }
.timeline .t { color: var(--muted); font-size: 12px; display: block; }
.empty { color: var(--muted); padding: 30px 0; text-align: center; }
@media (max-width: 640px) {
  .row .top { grid-template-columns: 1fr; gap: 2px; }
  .view { padding: 14px; }
}
</style>
</head>
<body>
<header>
  <h1 onclick="location.hash=''">Claude Summarizer</h1>
  <input id="q" type="search" placeholder="Filter by tab, directory, words…" autocomplete="off">
  <span class="count" id="count"></span>
</header>
<main id="app"></main>
<script>
const app = document.getElementById('app');
const q = document.getElementById('q');
let sessions = [];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const home = s => String(s ?? '').replace(/^\/Users\/[^/]+/, '~');

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function md(text) {
  const out = []; let list = null, para = [];
  const flush = () => { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; }
                        if (list) { out.push('</ul>'); list = null; } };
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { flush(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) { flush(); const n = Math.min(m[1].length + 1, 4); out.push(`<h${n}>${inline(m[2])}</h${n}>`); continue; }
    if ((m = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/))) {
      if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; }
      if (!list) { out.push('<ul>'); list = true; }
      out.push('<li>' + inline(m[1]) + '</li>'); continue;
    }
    if (list) { out.push('</ul>'); list = null; }
    para.push(line.trim());
  }
  flush();
  return out.join('');
}
function when(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  const days = (now - d) / 86400000;
  if (days < 1 && d.getDate() === now.getDate()) return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  if (days < 6) return d.toLocaleDateString([], {weekday: 'short'}) + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  return d.toLocaleDateString([], {day: 'numeric', month: 'short', year: days > 300 ? 'numeric' : undefined});
}
const stampName = n => { const [d, h] = n.split('T'); return new Date(d + 'T' + h + ':00').toLocaleString([], {weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'}); };
const title = s => s.tab || (s.former_tab ? `${s.folder}` : s.folder);
async function copy(text, btn) {
  await navigator.clipboard.writeText(text);
  const old = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => btn.textContent = old, 1200);
}

function listView() {
  const needle = q.value.trim().toLowerCase();
  const shown = sessions.filter(s => !needle || [s.tab, s.former_tab, s.folder, s.dir, s.session_id, s.sentence, s.paragraph]
    .some(v => String(v || '').toLowerCase().includes(needle)));
  document.getElementById('count').textContent = `${shown.length} of ${sessions.length} sessions`;
  if (!shown.length) { app.innerHTML = '<div class="empty">No sessions match.</div>'; return; }
  const groups = [['paragraph', 'Last 30 days'], ['sentence', '30 days to 6 months'], ['old', 'Older']];
  app.innerHTML = groups.map(([tier, label]) => {
    const rows = shown.filter(s => s.tier === tier);
    if (!rows.length) return '';
    return `<h2 class="group">${label} · ${rows.length}</h2>` + rows.map(s => `
      <div class="row" data-sid="${esc(s.session_id)}">
        <div class="top" onclick="this.parentNode.classList.toggle('open')">
          <div class="name" title="${esc(title(s))}"><span class="dot ${s.running ? '' : 'off'}" title="${s.running ? 'running' : 'not running'}"></span>${esc(title(s))}</div>
          <div class="sentence">${esc(s.sentence)}</div>
          <div class="when">${when(s.last_activity)}</div>
        </div>
        <div class="more">
          <div class="meta">
            <span class="chip">${esc(s.session_id)}</span>
            <span>${esc(s.agent)}</span>
            <span>${esc(home(s.dir))}</span>
            ${s.former_tab ? `<span>was tab “${esc(s.former_tab)}”</span>` : ''}
            <button onclick="copy(${esc(JSON.stringify(s.resume))}, this)">Copy resume command</button>
            <a href="#/s/${encodeURIComponent(s.session_id)}">Full page →</a>
            <span>${s.updates} update${s.updates === 1 ? '' : 's'}</span>
          </div>
          <div class="paragraph md">${md(s.paragraph)}</div>
        </div>
      </div>`).join('');
  }).join('');
}

async function sessionView(sid, name, showTimeline) {
  const data = await (await fetch(`/api/session/${encodeURIComponent(sid)}`)).json();
  if (!data || !data.updates) { app.innerHTML = '<div class="empty">Session not found.</div>'; return; }
  const ups = data.updates, s = data.session;
  if (!ups.length) { app.innerHTML = '<div class="empty">No pages yet.</div>'; return; }
  let i = name ? ups.findIndex(u => u.name === name) : ups.length - 1;
  if (i < 0) i = ups.length - 1;
  const u = await (await fetch(`/api/update/${encodeURIComponent(sid)}/${encodeURIComponent(ups[i].name)}`)).json();
  const link = n => `#/s/${encodeURIComponent(sid)}/${encodeURIComponent(n)}${showTimeline ? '/timeline' : ''}`;
  document.getElementById('count').textContent = '';
  app.innerHTML = `
    <div class="nav"><a href="#">← All sessions</a></div>
    <div class="view">
      <h1>${esc(title(s))}</h1>
      <div class="meta">
        <span class="dot ${s.running ? '' : 'off'}"></span>
        <span class="chip">${esc(s.session_id)}</span><span>${esc(s.agent)}</span><span>${esc(home(s.dir))}</span>
        ${s.former_tab ? `<span>was tab “${esc(s.former_tab)}”</span>` : ''}
        <button onclick="copy(${esc(JSON.stringify(s.resume))}, this)">Copy resume command</button>
      </div>
      <div class="nav">
        <button ${i === 0 ? 'disabled' : ''} onclick="location.hash='${link(ups[Math.max(i - 1, 0)].name)}'">← Prev</button>
        <span class="stamp">${stampName(ups[i].name)} · ${i + 1} of ${ups.length}${i === ups.length - 1 ? ' · latest' : ''}</span>
        <button ${i === ups.length - 1 ? 'disabled' : ''} onclick="location.hash='${link(ups[Math.min(i + 1, ups.length - 1)].name)}'">Next →</button>
        <button class="${showTimeline ? 'primary' : ''}" onclick="location.hash='#/s/${encodeURIComponent(sid)}/${encodeURIComponent(ups[i].name)}${showTimeline ? '' : '/timeline'}'">${showTimeline ? 'Hide timeline' : 'Timeline'}</button>
      </div>
      ${showTimeline ? `<ol class="timeline">${ups.map((x, j) => `<li class="${j === i ? 'cur' : ''}" onclick="location.hash='${link(x.name)}'"><span class="t">${stampName(x.name)}</span>${esc(x.sentence)}</li>`).join('')}</ol><hr style="border:0;border-top:1px solid var(--line);margin:16px 0">` : ''}
      <div class="md">${md(u.page)}</div>
      <div class="meta" style="margin-top:18px"><span class="chip">${esc(home(u.path))}</span></div>
    </div>`;
  window.scrollTo(0, 0);
}

async function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (parts[0] === 's' && parts[1]) {
    q.style.visibility = 'hidden';
    const tl = parts[parts.length - 1] === 'timeline';
    return sessionView(parts[1], parts[2] && parts[2] !== 'timeline' ? parts[2] : null, tl);
  }
  q.style.visibility = 'visible';
  sessions = await (await fetch('/api/sessions')).json();
  listView();
}
q.addEventListener('input', listView);
window.addEventListener('hashchange', route);
route();
</script>
</body>
</html>
"""
