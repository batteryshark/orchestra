"""`orchestra ui` — zero-dependency live web dashboard for a project's runs,
inboxes, findings feed, and teams. Reads the project SQLite; no writes."""
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from orchestra_cli import db, runners

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>orchestra</title>
<style>
:root { color-scheme: light dark;
  --bg:#f6f7f9; --card:#fff; --ink:#1a202c; --muted:#64748b; --line:#e2e8f0;
  --blue:#2563eb; --green:#16a34a; --red:#dc2626; --amber:#d97706; --gray:#6b7280; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0f1115; --card:#181b21; --ink:#e5e9f0; --muted:#8b95a5; --line:#2a2f3a; } }
* { box-sizing:border-box }
body { margin:0; font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif;
  background:var(--bg); color:var(--ink); }
header { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
  padding:14px 20px; border-bottom:1px solid var(--line); }
header h1 { font-size:16px; margin:0 }
header .root { color:var(--muted); font-family:ui-monospace,monospace; font-size:12px }
header nav { margin-left:auto; display:flex; gap:12px }
header a { color:var(--blue); text-decoration:none; font-size:13px }
main { display:grid; grid-template-columns:1fr 1fr; gap:16px; padding:16px 20px;
  max-width:1400px; margin:0 auto; }
@media (max-width:900px){ main { grid-template-columns:1fr } }
section { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; overflow:hidden }
section.wide { grid-column:1 / -1 }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:0 0 10px }
table { width:100%; border-collapse:collapse; font-size:13px }
th { text-align:left; color:var(--muted); font-weight:500; padding:4px 8px;
  border-bottom:1px solid var(--line); white-space:nowrap }
td { padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top }
tr:last-child td { border-bottom:none }
.chip { display:inline-block; padding:1px 9px; border-radius:99px; font-size:12px;
  font-weight:600; color:#fff }
.s-running { background:var(--blue); animation:pulse 1.6s infinite }
.s-spawning { background:var(--amber) }
.s-done { background:var(--green) } .s-failed,.s-timeout { background:var(--red) }
.s-killed { background:var(--gray) }
@keyframes pulse { 50% { opacity:.55 } }
.muted { color:var(--muted) } .mono { font-family:ui-monospace,monospace; font-size:12px }
.msg { border-left:3px solid var(--line); padding:6px 10px; margin:8px 0 }
.msg.unread { border-left-color:var(--blue) }
.msg .meta { font-size:12px; color:var(--muted) }
.msg .body { white-space:pre-wrap; overflow-wrap:anywhere; margin-top:2px }
.feeditem { padding:5px 0; border-bottom:1px solid var(--line); overflow-wrap:anywhere }
.feeditem:last-child { border-bottom:none }
button { font:inherit; font-size:12px; border:1px solid var(--line); background:none;
  color:var(--blue); border-radius:6px; padding:2px 10px; cursor:pointer }
pre.log { background:var(--bg); border:1px solid var(--line); border-radius:8px;
  padding:10px; font-size:12px; max-height:340px; overflow:auto; white-space:pre-wrap;
  overflow-wrap:anywhere; margin:8px 0 0 }
.summary { white-space:pre-wrap; overflow-wrap:anywhere; color:var(--muted); font-size:12px;
  max-height:120px; overflow:auto; margin-top:4px }
#beat { width:8px; height:8px; border-radius:50%; background:var(--green);
  display:inline-block; margin-left:6px }
</style></head><body>
<header><h1>orchestra<span id="beat"></span></h1><span class="root" id="root"></span>
<nav><a href="http://localhost:4747" target="_blank">ensemble dashboard</a>
<a href="http://localhost:43170" target="_blank">work tracker</a></nav></header>
<main>
<section class="wide"><h2>Runs</h2><table id="runs"></table></section>
<section><h2>Inboxes</h2><div id="inboxes"></div></section>
<section><h2>Findings feed</h2><div id="feed"></div>
<h2 style="margin-top:14px">Teams</h2><div id="teams"></div></section>
</main>
<script>
const esc = s => (s??'').toString().replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const openLogs = new Set();
function elapsed(start, end){
  const a = new Date(start), b = end ? new Date(end) : new Date();
  let s = Math.max(0, Math.floor((b-a)/1000));
  const m = Math.floor(s/60); s = s%60;
  return m ? `${m}m${String(s).padStart(2,'0')}s` : `${s}s`;
}
async function toggleLog(id){
  if (openLogs.has(id)) openLogs.delete(id); else openLogs.add(id);
  refresh();
}
async function refresh(){
  let st;
  try { st = await (await fetch('api/state')).json(); }
  catch(e){ document.getElementById('beat').style.background='var(--red)'; return; }
  document.getElementById('beat').style.background='var(--green)';
  document.getElementById('root').textContent = st.root;
  const rt = document.getElementById('runs');
  let rows = '<tr><th>id</th><th>agent</th><th>status</th><th>work</th><th>title</th><th>elapsed</th><th></th></tr>';
  for (const r of st.runs){
    rows += `<tr><td class="mono">${r.id}</td><td>${esc(r.agent)}</td>
      <td><span class="chip s-${esc(r.status)}">${esc(r.status)}</span></td>
      <td class="mono">${esc(r.work_item||'—')}</td>
      <td>${esc((r.title||'').slice(0,80))}${r.branch?` <span class="muted mono">${esc(r.branch)}</span>`:''}</td>
      <td class="mono">${elapsed(r.started_at, r.finished_at)}</td>
      <td><button onclick="toggleLog(${r.id})">${openLogs.has(r.id)?'hide':'log'}</button></td></tr>`;
    if (r.summary && ['done','failed','timeout'].includes(r.status))
      rows += `<tr><td></td><td colspan="6"><div class="summary">${esc(r.summary)}</div></td></tr>`;
    if (openLogs.has(r.id)){
      const lg = await (await fetch('api/log/'+r.id)).json();
      rows += `<tr><td></td><td colspan="6"><pre class="log">${esc(lg.text||'(empty)')}</pre></td></tr>`;
    }
  }
  rt.innerHTML = rows;
  const byRcpt = {};
  for (const m of st.messages) (byRcpt[m.recipient] ??= []).push(m);
  document.getElementById('inboxes').innerHTML = Object.entries(byRcpt).map(([r, ms]) => {
    const unread = ms.filter(m=>!m.read_at).length;
    return `<details ${unread?'open':''}><summary><b>${esc(r)}</b>
      <span class="muted">${ms.length} msgs${unread?`, <b>${unread} unread</b>`:''}</span></summary>
      ${ms.slice(-12).map(m=>`<div class="msg ${m.read_at?'':'unread'}">
        <div class="meta">${esc(m.sender)} → ${esc(m.recipient)} · ${esc(m.created_at)}
          ${m.run_id?` · run ${m.run_id}`:''}${m.work_item?` · ${esc(m.work_item)}`:''}</div>
        <div class="body">${esc(m.body)}</div></div>`).join('')}</details>`;
  }).join('') || '<span class="muted">no messages yet</span>';
  document.getElementById('feed').innerHTML = st.feed.map(f =>
    `<div class="feeditem"><span class="muted mono">${esc(f.created_at.slice(11,19))}</span>
     <b>${esc(f.author)}</b>: ${esc(f.body)}
     ${f.tags?`<span class="muted">[${esc(f.tags)}]</span>`:''}</div>`).join('')
    || '<span class="muted">feed empty</span>';
  document.getElementById('teams').innerHTML = st.teams.map(t =>
    `<div><b>${esc(t.name)}</b>: ${esc(t.members.join(', ')||'(empty)')}</div>`).join('')
    || '<span class="muted">no teams</span>';
}
refresh(); setInterval(refresh, 2500);
</script></body></html>"""


def make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                con = db.connect(root)
                state = {
                    "root": str(root),
                    "runs": [dict(r) for r in con.execute(
                        "SELECT * FROM runs ORDER BY id DESC LIMIT 50")][::-1],
                    "messages": [dict(r) for r in con.execute(
                        "SELECT * FROM messages ORDER BY id DESC LIMIT 150")][::-1],
                    "feed": [dict(r) for r in con.execute(
                        "SELECT * FROM feed ORDER BY id DESC LIMIT 50")][::-1],
                    "teams": [{"name": t["name"],
                               "members": [m["agent"] for m in con.execute(
                                   "SELECT agent FROM members WHERE team_id=?", (t["id"],))]}
                              for t in con.execute("SELECT * FROM teams")],
                }
                con.close()
                self._json(state)
            elif path.startswith("/api/log/"):
                try:
                    run_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    return self._json({"error": "bad id"}, 400)
                con = db.connect(root)
                r = con.execute("SELECT log_path FROM runs WHERE id=?", (run_id,)).fetchone()
                con.close()
                text = ""
                if r and r["log_path"] and Path(r["log_path"]).is_file():
                    lines = []
                    for line in Path(r["log_path"]).read_text(errors="replace").splitlines()[-400:]:
                        line = line.strip()
                        if line.startswith("{"):
                            try:
                                lines.extend(runners._dig(json.loads(line), {"text"}))
                                continue
                            except ValueError:
                                pass
                        if line:
                            lines.append(line)
                    text = "\n".join(lines)[-20000:]
                self._json({"text": text})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(root: Path, port: int = 4764, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root))
    url = f"http://127.0.0.1:{port}"
    print(f"orchestra ui: {url}  (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
