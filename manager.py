#!/usr/bin/env python3
"""Manager console: chat with a persistent 'manager' Claude session that can
talk to every other live session (via bus.py), browse their Claude memories,
and inject follow-up commands into live sessions.

    .env/bin/python manager.py          # http://localhost:8765

Stdlib only. The manager is a real headless Claude session (claude -p) with
tools enabled, so it asks/injects/spawns by running bus.py itself.
"""

import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bus

PORT = 8799
HERE = Path(__file__).resolve().parent
STATE = Path.home() / ".claude" / "bus" / "manager.json"
LOCK = threading.Lock()  # ponytail: one manager turn at a time

SYSTEM_PROMPT = f"""You are the MANAGER of all Claude Code sessions on this machine.
Your tools for coordinating the team, all via Bash:

- {HERE}/bus.py sessions                       -> list live sessions (id, project, cwd)
- {HERE}/bus.py ask "q" [--to SID] --wait N    -> ask session(s); a fork answers from their context
- {HERE}/bus.py inject SID "prompt"            -> type a prompt INTO the live session (real, visible action in their Warp tab). Use after asking, when the user wants a session to actually DO something.
- {HERE}/bus.py log -n 20                      -> recent bus traffic
- To spawn a fresh agent for a task: run `claude -p --dangerously-skip-permissions "task"` in the right cwd (background it if long).
- Session memories live in ~/.claude/projects/<cwd-dashed>/memory/ and transcripts as .jsonl next to them.

Rules: prefer ask (read, invisible) before inject (write, visible). Confirm with the user before injecting or spawning unless they already told you to. Be concise; you are chatting in a small console UI."""

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Manager</title><style>
:root{--bg:#101418;--panel:#1a2028;--fg:#d8dee6;--dim:#7a8494;--acc:#5ec2b7;--me:#2b3a4a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,Helvetica,sans-serif;display:grid;
grid-template-columns:230px 1fr 330px;height:100vh}
h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin:14px 12px 6px}
#side,#right{background:var(--panel);overflow-y:auto}
.sess{padding:7px 12px;cursor:pointer;border-left:3px solid transparent}
.sess:hover{background:#232b36}.sess.sel{border-left-color:var(--acc);background:#232b36}
.sess .n{font-weight:600}.sess .c{color:var(--dim);font-size:11px}
#chat{display:flex;flex-direction:column;height:100vh}
#msgs{flex:1;overflow-y:auto;padding:16px 20px}
.m{max-width:78%;margin:6px 0;padding:9px 12px;border-radius:10px;white-space:pre-wrap;word-wrap:break-word}
.me{background:var(--me);margin-left:auto}.mgr{background:#20303c}
.sysnote{color:var(--dim);font-size:12px;text-align:center;margin:8px 0}
#inbar{display:flex;gap:8px;padding:12px;border-top:1px solid #2a3340}
#in{flex:1;background:#0d1117;border:1px solid #2a3340;border-radius:8px;color:var(--fg);
padding:10px;font:inherit;resize:none;height:60px}
button{background:var(--acc);border:0;border-radius:8px;color:#08201d;font-weight:700;
padding:0 18px;cursor:pointer}button:disabled{opacity:.4}
#mem{padding:4px 12px 20px;font-size:12px}
#mem pre{background:#0d1117;padding:10px;border-radius:8px;white-space:pre-wrap;
word-wrap:break-word;max-height:46vh;overflow-y:auto}
.mfile{color:var(--acc);cursor:pointer;padding:2px 0}.mfile:hover{text-decoration:underline}
#busfeed{padding:4px 12px;font-size:12px}
.be{margin:6px 0;color:var(--dim)}.be b{color:var(--fg);font-weight:600}
.spin{color:var(--dim);font-style:italic}
</style></head><body>
<div id="side"><h2>Live sessions</h2><div id="slist"></div>
<h2>Memory</h2><div id="mem">select a session</div></div>
<div id="chat"><div id="msgs"><div class="sysnote">manager console — one persistent Claude session with bus powers</div></div>
<div id="inbar"><textarea id="in" placeholder="Talk to the manager… (Enter to send, Shift+Enter newline)"></textarea>
<button id="send">Send</button></div></div>
<div id="right"><h2>Bus feed</h2><div id="busfeed"></div></div>
<script>
const $=q=>document.querySelector(q);
let sel=null;
async function j(u,opt){const r=await fetch(u,opt);return r.json()}
async function sessions(){const s=await j('/api/sessions');const el=$('#slist');el.innerHTML='';
 s.forEach(x=>{const d=document.createElement('div');d.className='sess'+(sel===x.sid?' sel':'');
 d.innerHTML=`<div class="n">${x.name}</div><div class="c">${x.sid.slice(0,8)} · ${x.state}</div>`;
 d.onclick=()=>{sel=x.sid;sessions();memory(x.sid)};el.appendChild(d)})}
async function memory(sid){const m=await j('/api/memory?sid='+sid);const el=$('#mem');
 if(!m.files.length){el.innerHTML='<i>no memory files</i>';return}
 el.innerHTML='';m.files.forEach(f=>{const d=document.createElement('div');d.className='mfile';
 d.textContent=f;d.onclick=async()=>{const c=await j('/api/memory?sid='+sid+'&file='+encodeURIComponent(f));
 let pre=$('#mem pre');if(!pre){pre=document.createElement('pre');el.appendChild(pre)}
 pre.textContent=c.content};el.appendChild(d)})}
async function feed(){const ev=await j('/api/bus');const el=$('#busfeed');el.innerHTML='';
 ev.reverse().forEach(e=>{const d=document.createElement('div');d.className='be';
 d.innerHTML=`<b>${e.icon} ${e.who}</b> ${e.t}<br>${e.text}`;el.appendChild(d)})}
function add(cls,text){const d=document.createElement('div');d.className='m '+cls;
 d.textContent=text;$('#msgs').appendChild(d);$('#msgs').scrollTop=1e9;return d}
async function send(){const t=$('#in').value.trim();if(!t)return;$('#in').value='';
 add('me',t);const w=add('mgr spin','manager is thinking…');$('#send').disabled=true;
 try{const r=await j('/api/chat',{method:'POST',body:JSON.stringify({text:t})});
 w.textContent=r.reply;w.classList.remove('spin')}catch(e){w.textContent='error: '+e}
 $('#send').disabled=false}
$('#send').onclick=send;
$('#in').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
sessions();feed();setInterval(sessions,15000);setInterval(feed,10000);
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
    r = subprocess.run(cmd, capture_output=True, text=True,
                       cwd=str(HERE), timeout=600)
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


def project_dir(cwd):
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/sessions":
            import app as menubar
            out = []
            live = bus.live_sessions()
            states = {}
            for name, topic, state, _ in menubar.build_session_list():
                states[name] = state
            for sid, cwd in sorted(live.items(), key=lambda kv: kv[1]):
                name = Path(cwd).name
                out.append({"sid": sid, "cwd": cwd, "name": name,
                            "state": states.get(name, "?")})
            self._json(out)
        elif u.path == "/api/memory":
            sid = q.get("sid", "")
            cwd = bus.live_sessions().get(sid)
            if not cwd:
                self._json({"files": []})
                return
            mdir = project_dir(cwd) / "memory"
            if q.get("file"):
                f = (mdir / q["file"]).resolve()
                if mdir.resolve() not in f.parents and f != mdir.resolve():
                    self._json({"content": "(outside memory dir)"}, 400)
                    return
                try:
                    self._json({"content": f.read_text(errors="replace")[:40000]})
                except OSError:
                    self._json({"content": "(unreadable)"})
            else:
                files = sorted(p.name for p in mdir.glob("*.md")) if mdir.exists() else []
                self._json({"files": files})
        elif u.path == "/api/bus":
            names = {s: Path(c).name for s, c in bus.live_sessions().items()}
            ev = []
            for ts, kind, qid, who, text, extra in bus.bus_events(limit=12):
                ev.append({"t": time.strftime("%H:%M", time.localtime(ts)),
                           "icon": "❓" if kind == "Q" else "💬",
                           "who": names.get(who, who[:8]), "text": text[:160]})
            self._json(ev)
        else:
            self._json({"err": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/chat":
            self._json({"err": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length", 0))
        text = json.loads(self.rfile.read(n))["text"]
        with LOCK:
            reply = manager_reply(text)
        self._json({"reply": reply})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"manager console: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
