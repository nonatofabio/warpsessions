#!/usr/bin/env python3
"""Manager console — Discord-style chat over the session bus.

    .env/bin/python manager.py          # http://localhost:8799

Channels:
  #manager   — chat with the persistent manager Claude session
  #general   — broadcast to ALL live sessions (each answers via a fork)
  @<session> — 1:1 channel per live session (bus ask --to under the hood)

Click a session name to open its channel; the ⤴ button jumps to its Warp tab.
Stdlib only on the server; marked + DOMPurify from CDN for markdown.
"""

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bus

PORT = 8799
HERE = Path(__file__).resolve().parent
STATE = Path.home() / ".claude" / "bus" / "manager.json"
CHAT_LOG = Path.home() / ".claude" / "bus" / "manager-chat.jsonl"
DASHBOARD = HERE / "overnight" / "dashboard.html"
LOCK = threading.Lock()  # ponytail: one manager turn at a time

SYSTEM_PROMPT = f"""You are the MANAGER of all Claude Code sessions on this machine.
Your tools for coordinating the team, all via Bash:

- {HERE}/bus.py sessions                       -> list live sessions (id, project, cwd)
- {HERE}/bus.py ask "q" [--to SID] --wait N    -> ask session(s); a fork answers from their context
- {HERE}/bus.py inject SID "prompt"            -> type a prompt INTO the live session (real, visible action in their Warp tab). Use after asking, when the user wants a session to actually DO something.
- {HERE}/bus.py log -n 20                      -> recent bus traffic
- {HERE}/bus.py spawn <dir> ["kickoff prompt"]  -> open a NEW VISIBLE Warp tab running claude in that folder (created if missing). The user can watch it work; it joins the bus/sidebar automatically within ~30s. First time in a new folder, Claude asks the user to confirm folder trust in that tab. Prefer this over headless when the user wants to see or interact with the new agent.
- For invisible one-off work: `claude -p --dangerously-skip-permissions "task"` in the right cwd (background it if long).
- Session memories live in ~/.claude/projects/<cwd-dashed>/memory/ and transcripts as .jsonl next to them.

Rules: prefer ask (read, invisible) before inject (write, visible). Confirm with the user before injecting or spawning unless they already told you to. Be concise; you are chatting in a small console UI.

HARD LIMIT: your chat turn is killed at 10 minutes. Never do long work inline — no builds, no multi-question bus sweeps with long waits, no writing whole apps in-turn. For anything heavy, background a subagent and reply immediately with where its output will land:
  nohup claude -p --dangerously-skip-permissions "task..." > /tmp/mgr-task-X.log 2>&1 &
Then tell the user the log/output path. You can check on it in a later turn."""

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Manager</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>
<style>
:root{--bg:#313338;--side:#2b2d31;--sidedark:#1e1f22;--fg:#dbdee1;--dim:#949ba4;
--acc:#5865f2;--green:#23a559;--red:#f23f43;--hover:#35373c;--chip:#404249}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,'Segoe UI',Helvetica,sans-serif;display:grid;
grid-template-columns:250px 1fr;height:100vh}
#side{background:var(--side);display:flex;flex-direction:column;overflow-y:auto}
#side .top{padding:14px 14px 8px;border-bottom:1px solid var(--sidedark)}
#side .top b{font-size:15px}
#side .top a{display:block;margin-top:8px;color:#00a8fc;font-size:13px;text-decoration:none}
#side .top a:hover{text-decoration:underline}
h3{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);
margin:16px 10px 4px;font-weight:600}
.ch{display:flex;align-items:center;gap:8px;margin:1px 8px;padding:6px 8px;
border-radius:5px;cursor:pointer;color:var(--dim)}
.ch:hover{background:var(--hover);color:var(--fg)}
.ch.sel{background:var(--chip);color:#fff}
.ch .hash{opacity:.7;width:14px;text-align:center}
.ch .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ch .st{font-size:9px}.st.working{color:var(--green)}.st.blocked{color:var(--red)}
.st.waiting{color:var(--dim)}
.ch .jump{visibility:hidden;border:0;background:none;color:var(--dim);cursor:pointer;
font-size:14px;padding:0 2px}
.ch:hover .jump{visibility:visible}.ch .jump:hover{color:#fff}
#main{display:flex;flex-direction:column;height:100vh;min-width:0}
#head{padding:12px 18px;border-bottom:1px solid var(--sidedark);font-weight:700}
#head small{color:var(--dim);font-weight:400;margin-left:10px}
#msgs{flex:1;overflow-y:auto;padding:10px 18px}
.msg{display:flex;gap:12px;padding:7px 4px;border-radius:4px}
.msg:hover{background:#2e3035}
.av{width:38px;height:38px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;
justify-content:center;font-weight:700;font-size:15px;color:#fff}
.body{min-width:0;flex:1}
.hd .who{font-weight:600}.hd .t{color:var(--dim);font-size:11px;margin-left:8px}
.txt{overflow-wrap:break-word}
.txt p{margin:4px 0}.txt pre{background:var(--sidedark);padding:8px 10px;border-radius:6px;
overflow-x:auto;font-size:13px}.txt code{background:var(--sidedark);padding:1px 4px;
border-radius:4px;font-size:13px}.txt pre code{padding:0;background:none}
.txt ul,.txt ol{margin:4px 0;padding-left:22px}
.txt a{color:#00a8fc}.txt h1,.txt h2,.txt h3{margin:8px 0 4px;font-size:15px}
.pending{color:var(--dim);font-style:italic}
.err .txt{color:var(--red)}
#inwrap{padding:0 18px 20px}
#inbar{display:flex;gap:8px;background:#383a40;border-radius:8px;padding:4px 6px 4px 14px}
#in{flex:1;background:none;border:0;color:var(--fg);padding:10px 0;font:inherit;
resize:none;height:44px;outline:none}
button{background:var(--acc);border:0;border-radius:6px;color:#fff;font-weight:600;
padding:0 16px;cursor:pointer;margin:4px 0}button:disabled{opacity:.4}
.sysnote{color:var(--dim);font-size:12px;text-align:center;margin:10px 0}
</style></head><body>
<div id="side">
 <div class="top"><b>Manager Console</b><a id="dash" href="/dashboard" target="_blank"
  style="display:none">📊 Overnight dashboard</a></div>
 <h3>Channels</h3>
 <div class="ch" data-ch="manager"><span class="hash">👑</span><span class="nm">manager</span></div>
 <div class="ch" data-ch="general"><span class="hash">#</span><span class="nm">general</span></div>
 <h3>Direct — live sessions</h3><div id="dms"></div>
</div>
<div id="main">
 <div id="head">👑 manager<small>persistent coordinator session</small></div>
 <div id="msgs"></div>
 <div id="inwrap"><div id="inbar">
  <textarea id="in" placeholder="Message #manager"></textarea>
  <button id="send">Send</button></div></div>
</div>
<script>
const $=q=>document.querySelector(q), $$=q=>document.querySelectorAll(q);
let cur='manager', names={}, inflight=false;
const COLORS=['#5865f2','#23a559','#f0b232','#eb459e','#3ba55c','#faa61a','#7289da','#e91e63'];
const col=s=>COLORS[[...s].reduce((a,c)=>a+c.charCodeAt(0),0)%COLORS.length];
const md=t=>DOMPurify.sanitize(marked.parse(t||''));
async function j(u,opt){const r=await fetch(u,opt);return r.json()}
function chName(){return cur==='manager'?'👑 manager':cur==='general'?'# general':'@ '+(names[cur]||cur.slice(0,8))}
function chTopic(){return cur==='manager'?'persistent coordinator session'
 :cur==='general'?'broadcast — every live session answers via a fork'
 :'1:1 with a fork of this session · ⤴ jumps to its Warp tab'}
async function sidebar(){const d=await j('/api/channels');
 $('#dash').style.display=d.dashboard?'block':'none';
 names={};d.sessions.forEach(s=>names[s.sid]=s.name);
 const el=$('#dms');el.innerHTML='';
 d.sessions.forEach(s=>{const div=document.createElement('div');
  div.className='ch'+(cur===s.sid?' sel':'');div.dataset.ch=s.sid;
  div.innerHTML=`<span class="st ${s.state}">●</span><span class="nm">${s.name}</span>
   <button class="jump" title="Jump to Warp tab">⤴</button>`;
  div.onclick=()=>switchCh(s.sid);
  div.querySelector('.jump').onclick=e=>{e.stopPropagation();fetch('/api/jump',{method:'POST',body:JSON.stringify({sid:s.sid})})};
  el.appendChild(div)});
 $$('#side .ch[data-ch=manager],#side .ch[data-ch=general]').forEach(e=>
  e.classList.toggle('sel',cur===e.dataset.ch));}
function render(list){const el=$('#msgs');el.innerHTML='';
 if(!list.length)el.innerHTML='<div class="sysnote">no messages yet</div>';
 list.forEach(m=>{const d=document.createElement('div');
  d.className='msg'+(m.ok===false?' err':'');
  const ini=(m.who[0]||'?').toUpperCase();
  d.innerHTML=`<div class="av" style="background:${col(m.who)}">${ini}</div>
   <div class="body"><div class="hd"><span class="who">${m.who}</span>
   <span class="t">${m.t}</span></div><div class="txt">${m.pending?'<span class="pending">'+m.text+'</span>':md(m.text)}</div></div>`;
  el.appendChild(d)});
 el.scrollTop=1e9}
async function loadMsgs(){if(inflight)return;const d=await j('/api/messages?ch='+cur);render(d)}
function switchCh(ch){cur=ch;$('#head').innerHTML=chName()+`<small>${chTopic()}</small>`;
 $('#in').placeholder='Message '+chName();sidebar();loadMsgs()}
$$('#side .ch').forEach(e=>e.onclick=()=>switchCh(e.dataset.ch));
function bubble(who,text,pending){const el=$('#msgs');
 const d=document.createElement('div');d.className='msg';
 d.innerHTML=`<div class="av" style="background:${col(who)}">${who[0].toUpperCase()}</div>
  <div class="body"><div class="hd"><span class="who">${who}</span>
  <span class="t">now</span></div><div class="txt">${pending?'<span class="pending">'+text+'</span>':md(text)}</div></div>`;
 el.appendChild(d);el.scrollTop=1e9;return d}
async function send(){const t=$('#in').value.trim();if(!t)return;$('#in').value='';
 bubble('you',t);
 const wait=bubble(cur==='manager'?'manager':'bus',
  cur==='manager'?'thinking…':'delivering to fork(s)…',true);
 const ch=cur;$('#send').disabled=true;inflight=true;
 try{await fetch('/api/say',{method:'POST',body:JSON.stringify({ch,text:t})})}
 catch(e){wait.querySelector('.txt').textContent='send failed: '+e}
 $('#send').disabled=false;inflight=false;if(cur===ch)loadMsgs()}
$('#send').onclick=send;
$('#in').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
switchCh('manager');setInterval(loadMsgs,4000);setInterval(sidebar,15000);sidebar();
</script></body></html>"""


def manager_reply(text):
    """One manager turn; persists conversation via resume-chain in STATE."""
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    cmd = ["claude", "-p", "--output-format", "json",
           "--dangerously-skip-permissions",
           "--strict-mcp-config",  # no MCP servers: manager only needs Bash/Read
           "--append-system-prompt", SYSTEM_PROMPT]
    if st.get("sid"):
        cmd += ["--resume", st["sid"]]
    cmd.append(text)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=str(HERE), timeout=600)
    except subprocess.TimeoutExpired:
        return ("(manager turn timed out after 10 min — it tried to do heavy work "
                "inside the chat turn. Ask it to background the work instead: it can "
                "nohup a `claude -p` subagent and reply immediately.)")
    if r.returncode != 0:
        return f"(manager error rc={r.returncode}) {r.stderr[-400:]}"
    try:
        out = json.loads(r.stdout)
        if isinstance(out, list):  # event array; result event carries the reply
            out = next((e for e in out if e.get("type") == "result"), {})
        STATE.parent.mkdir(parents=True, exist_ok=True)
        sid = out.get("session_id", st.get("sid"))
        STATE.write_text(json.dumps({"sid": sid}))
        if sid and sid not in bus.fork_ids():
            with bus.FORKS.open("a") as fh:  # manager must not hijack cwd identity
                fh.write(sid + "\n")
        return out.get("result", "(no result)")
    except ValueError:
        return r.stdout.strip() or "(empty reply)"


def chat_append(role, text):
    CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with CHAT_LOG.open("a") as fh:
        fh.write(json.dumps({"role": role, "text": text, "ts": time.time()}) + "\n")


def display_name(sid, names):
    if sid in names:
        return names[sid]
    if sid in ("fnp-console",):
        return "you"
    if sid == "bus-daemon":
        return "daemon"
    proj = bus._project_of(sid)
    return proj.split("-")[-1] if proj else sid[:8]


def channel_messages(ch, names):
    """Messages for a channel from bus q/a files (or the manager chat log)."""
    out = []
    if ch == "manager":
        if CHAT_LOG.exists():
            for line in CHAT_LOG.read_text().splitlines()[-200:]:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                out.append({"who": "you" if d["role"] == "user" else "manager",
                            "t": time.strftime("%b %d %H:%M", time.localtime(d["ts"])),
                            "text": d["text"]})
        return out
    events = bus.bus_events()
    answered = set()
    for ts, kind, qid, who, text, extra in events:
        if kind == "A":
            answered.add(qid)
    for ts, kind, qid, who, text, extra in events:
        rel = (ch == "general" and (kind == "Q" and extra == "all"))
        if ch != "general":
            rel = who == ch or (kind == "Q" and extra == ch)
        if kind == "A" and ch == "general":
            # answers to broadcast questions belong in #general
            q = next((e for e in events if e[1] == "Q" and e[2] == qid), None)
            rel = bool(q and q[5] == "all")
        if not rel:
            continue
        m = {"who": display_name(who, names),
             "t": time.strftime("%b %d %H:%M", time.localtime(ts)), "text": text}
        if kind == "A" and extra == "ERROR":
            m["ok"] = False
        out.append(m)
        if kind == "Q" and qid not in answered:
            out.append({"who": "bus", "t": "", "text": "awaiting reply…", "pending": True})
    return out


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _html(self, text):
        b = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            self._html(HTML)
        elif u.path == "/dashboard":
            if DASHBOARD.exists():
                self._html(DASHBOARD.read_text(errors="replace"))
            else:
                self._html("<h3>no dashboard yet</h3>")
        elif u.path == "/api/channels":
            import app as menubar
            states = {n: s for n, _, s, _ in
                      [(x[0], x[1], x[2], x[3]) for x in menubar.build_session_list()]}
            sessions = []
            for sid, cwd in sorted(bus.live_sessions().items(), key=lambda kv: kv[1]):
                name = Path(cwd).name
                sessions.append({"sid": sid, "name": name,
                                 "state": states.get(name, "waiting")})
            self._json({"sessions": sessions, "dashboard": DASHBOARD.exists()})
        elif u.path == "/api/messages":
            names = {s: Path(c).name for s, c in bus.live_sessions().items()}
            self._json(channel_messages(q.get("ch", "manager"), names))
        else:
            self._json({"err": "not found"}, 404)

    def do_POST(self):
        import os
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        if self.path == "/api/jump":
            info = bus.live_sessions_full().get(body.get("sid", ""))
            if info and info.get("warp"):
                subprocess.run(["open", f"warp://session/{info['warp']}"])
                self._json({"ok": True})
            else:
                self._json({"ok": False}, 404)
        elif self.path == "/api/say":
            ch, text = body.get("ch", "manager"), body.get("text", "").strip()
            if not text:
                self._json({"err": "empty"}, 400)
                return
            if ch == "manager":
                chat_append("user", text)
                with LOCK:
                    reply = manager_reply(text)
                chat_append("manager", reply)
                self._json({"ok": True})
            else:
                import uuid as _uuid
                qid = time.strftime("%Y%m%d-%H%M%S") + "-" + _uuid.uuid4().hex[:6]
                bus.QDIR.mkdir(parents=True, exist_ok=True)
                (bus.QDIR / f"{qid}.json").write_text(json.dumps(
                    {"id": qid, "from": "fnp-console", "from_cwd": str(HERE),
                     "to": "all" if ch == "general" else ch,
                     "text": text, "ts": time.time()}))
                self._json({"ok": True, "qid": qid})
        else:
            self._json({"err": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"manager console: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
