#!/usr/bin/env python3
"""Dead-simple file bus between live Claude Code sessions.

  bus.py sessions                     list live sessions (id + project)
  bus.py ask "q" [--to SID] [--wait N] [--timeout S]
  bus.py status                       your bus footprint (answers you gave via forks)
  bus.py daemon                       answers questions by forking target sessions

Questions land in ~/.claude/bus/q/, answers in ~/.claude/bus/a/<qid>/.
The daemon answers a question by running, in the target session's cwd:
    claude -p --resume <sid> --fork-session --session-id <new-uuid> "<question>"
so the live session is never interrupted. Fork session ids are recorded in
~/.claude/bus/forks.txt and excluded from session-identity inference.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

BUS = Path.home() / ".claude" / "bus"
QDIR, ADIR, TMP = BUS / "q", BUS / "a", BUS / "tmp"
FORKS = BUS / "forks.txt"
FORK_TIMEOUT = 300
MAX_Q_AGE = 7200  # ponytail: daemon ignores questions older than 2h (restart safety)
NACK_AFTER = 60   # secs before an undeliverable 1:1 question gets a NACK answer


def _project_of(sid):
    """Project dir name that contains this session's transcript, or None."""
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"))
    return hits[0].parent.name if hits else None


def fork_ids():
    return set(FORKS.read_text().split()) if FORKS.exists() else set()


def sid_for_cwd(cwd):
    """Newest non-fork transcript for a cwd = that session's id."""
    proj = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    ign = fork_ids()
    fs = sorted((p for p in proj.glob("*.jsonl") if p.stem not in ign),
                key=lambda p: p.stat().st_mtime, reverse=True)
    return fs[0].stem if fs else None


def live_sessions_full():
    """{session_id: {cwd, warp, pid}} for running claude processes."""
    out = {}
    pids = subprocess.run(["pgrep", "-x", "claude"],
                          capture_output=True, text=True).stdout.split()
    for pid in pids:
        r = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                           capture_output=True, text=True).stdout
        m = re.search(r"^n(.+)$", r, re.M)
        if not m:
            continue
        env = subprocess.run(["ps", "-p", pid, "-wwwE", "-o", "command="],
                             capture_output=True, text=True).stdout
        w = re.search(r"WARP_TERMINAL_SESSION_UUID=([0-9a-f]+)", env)
        sid = sid_for_cwd(m.group(1))
        if sid:
            out[sid] = {"cwd": m.group(1),
                        "warp": w.group(1) if w else None, "pid": pid}
    return out


def live_sessions():
    """{session_id: cwd} for running claude processes."""
    return {sid: v["cwd"] for sid, v in live_sessions_full().items()}


def answers_by(sid):
    return sorted(ADIR.glob(f"*/{sid}.json")) if ADIR.exists() else []


def write_answer(qid, sid, text, ok=True):
    d = ADIR / qid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps(
        {"qid": qid, "from": sid, "text": text, "ok": ok, "ts": time.time()}))


# ---------------- commands ----------------

def cmd_sessions(_):
    for sid, cwd in sorted(live_sessions().items(), key=lambda kv: kv[1]):
        print(f"{sid}  {Path(cwd).name}  ({cwd})")


def cmd_ask(args):
    me = sid_for_cwd(os.getcwd()) or "unknown"
    qid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    QDIR.mkdir(parents=True, exist_ok=True)
    (QDIR / f"{qid}.json").write_text(json.dumps(
        {"id": qid, "from": me, "from_cwd": os.getcwd(),
         "to": args.to or "all", "text": args.text, "ts": time.time()}))
    prior = len(answers_by(me))
    print(f"posted {qid} as {me[:8]} (to: {args.to or 'all'})")
    print(f"[bus meta] this session has answered {prior} bus question(s) via forks"
          " — `bus.py status` shows them")
    if args.wait <= 0:
        return
    deadline, seen = time.time() + args.timeout, set()
    while time.time() < deadline:
        adir = ADIR / qid
        for f in sorted(adir.glob("*.json")) if adir.exists() else []:
            if f.name in seen:
                continue
            seen.add(f.name)
            a = json.loads(f.read_text())
            tag = "" if a.get("ok", True) else " [ERROR]"
            print(f"\n--- answer from {a['from'][:8]}{tag} ---\n{a['text']}")
        if len(seen) >= args.wait:
            return
        time.sleep(3)
    print(f"\n(timeout: got {len(seen)}/{args.wait} answers)")


def cmd_status(_):
    me = sid_for_cwd(os.getcwd())
    if not me:
        sys.exit("no session transcript found for this cwd")
    mine = answers_by(me)
    print(f"session {me[:8]}: answered {len(mine)} bus question(s) via forks")
    for f in mine:
        try:
            q = json.loads((QDIR / f"{f.parent.name}.json").read_text())
            a = json.loads(f.read_text())
            print(f"\nQ [{f.parent.name}] from {q['from'][:8]}: {q['text'][:120]}")
            print(f"A: {a['text'][:300]}")
        except (OSError, ValueError):
            continue
    if QDIR.exists():
        asked = [json.loads(p.read_text()) for p in QDIR.glob("*.json")]
        asked = [q for q in asked if q["from"] == me]
        if asked:
            print(f"\nquestions you asked: {len(asked)}")
            for q in asked:
                n = len(list((ADIR / q['id']).glob('*.json'))) if (ADIR / q['id']).exists() else 0
                print(f"  [{q['id']}] {n} answer(s): {q['text'][:100]}")


def spawn_fork(q, sid, cwd, prior):
    fid = str(uuid.uuid4())
    FORKS.parent.mkdir(parents=True, exist_ok=True)
    with FORKS.open("a") as fh:
        fh.write(fid + "\n")
    TMP.mkdir(parents=True, exist_ok=True)
    outp, errp = TMP / f"{q['id']}-{sid}.out", TMP / f"{q['id']}-{sid}.err"
    prompt = (
        f"[session-bus] Claude session {q['from'][:8]} (in {q.get('from_cwd', '?')}) asks you:\n"
        f"{q['text']}\n\n"
        "You are a temporary fork of your session, created only to answer this; your main "
        f"session continues untouched. You have answered {prior} earlier bus question(s) "
        f"(`python3 {Path(__file__).resolve()} status` in your cwd lists them, if relevant). "
        "Answer concisely from your session's context. Output ONLY the answer text.")
    p = subprocess.Popen(
        ["claude", "-p", "--resume", sid, "--fork-session", "--session-id", fid,
         "--dangerously-skip-permissions", prompt],
        cwd=cwd, stdout=outp.open("w"), stderr=errp.open("w"), text=True)
    return (q["id"], p, outp, errp, time.time())


def bus_events(limit=None):
    """All Q and A events, oldest first: (ts, kind, qid, who, text, extra)."""
    ev = []
    for qf in QDIR.glob("*.json") if QDIR.exists() else []:
        try:
            q = json.loads(qf.read_text())
            ev.append((q["ts"], "Q", q["id"], q["from"], q["text"], q["to"]))
        except (OSError, ValueError):
            continue
    for af in ADIR.glob("*/*.json") if ADIR.exists() else []:
        try:
            a = json.loads(af.read_text())
            ev.append((a["ts"], "A", a["qid"], a["from"], a["text"],
                       "ok" if a.get("ok", True) else "ERROR"))
        except (OSError, ValueError):
            continue
    ev.sort()
    return ev[-limit:] if limit else ev


def _print_event(e, names):
    ts, kind, qid, who, text, extra = e
    t = time.strftime("%H:%M:%S", time.localtime(ts))
    name = names.get(who, who[:8])
    if kind == "Q":
        to = "all" if extra == "all" else names.get(extra, extra[:8])
        print(f"{t}  ❓ {name} → {to}  [{qid}]\n          {text}")
    else:
        mark = "💬" if extra == "ok" else "⚠️"
        print(f"{t}  {mark} {name} answered [{qid}]\n          {text[:400]}")


def cmd_log(args):
    names = {sid: Path(cwd).name for sid, cwd in live_sessions().items()}
    seen = set()
    for e in bus_events(limit=args.n):
        seen.add((e[1], e[2], e[3]))
        _print_event(e, names)
    while args.follow:
        time.sleep(2)
        for e in bus_events():
            k = (e[1], e[2], e[3])
            if k not in seen:
                seen.add(k)
                _print_event(e, names)


WARP_DB = (Path.home() / "Library/Group Containers/2BBY89MBSN.dev.warp"
           / "Library/Application Support/dev.warp.Warp-Stable/warp.sqlite")


def _warp_pane_exists(warp_uuid):
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{WARP_DB}?mode=ro", uri=True, timeout=1)
        try:
            return con.execute("SELECT 1 FROM terminal_panes WHERE lower(hex(uuid))=?",
                               (warp_uuid.lower(),)).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return True  # can't verify -> don't block (caller beware)


def cmd_inject(args):
    """Type a prompt into a LIVE session's Warp tab (focuses it first)."""
    info = live_sessions_full().get(args.sid)
    if not info:
        sys.exit(f"session {args.sid} not live")
    if not info["warp"]:
        sys.exit(f"session {args.sid} is not in a Warp tab")
    if not _warp_pane_exists(info["warp"]):
        # Guard: dead pane means the deep link focuses nothing and the
        # keystrokes would land in whatever Warp window is frontmost.
        sys.exit(f"refusing to inject: Warp pane {info['warp']} no longer exists "
                 f"(tab closed?); keystrokes could hit the wrong session")
    subprocess.run(["open", f"warp://session/{info['warp']}"])
    time.sleep(0.8)
    # ponytail: osascript keystroke; needs Accessibility perms for the caller
    safe = args.text.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'tell application "System Events" to tell process "Warp"\n'
              f' keystroke "{safe}"\n delay 0.3\n key code 36\nend tell')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"inject failed: {r.stderr.strip()}")
    print(f"injected into {args.sid[:8]} ({Path(info['cwd']).name})")


def cmd_spawn(args):
    """Open a NEW visible Warp tab running claude in a folder (user can watch)."""
    d = Path(args.dir).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    claude_cmd = "claude"  # user shell alias already adds skip-permissions
    if args.prompt:
        claude_cmd += " " + json.dumps(args.prompt)  # json.dumps = safe shell quoting here
    cfg = f"""---
name: bus-spawn
windows:
  - tabs:
      - title: {d.name}
        layout:
          cwd: {d}
          commands:
            - exec: {claude_cmd}
"""
    cfgdir = Path.home() / ".warp" / "launch_configurations"
    cfgdir.mkdir(parents=True, exist_ok=True)
    name = f"bus-spawn-{d.name}"
    (cfgdir / f"{name}.yaml").write_text(cfg.replace("name: bus-spawn", f"name: {name}"))
    subprocess.run(["open", f"warp://launch/{name}"])  # launch URI takes the config NAME
    print(f"spawned Warp tab in {d}" + (f" with prompt: {args.prompt[:60]}" if args.prompt else ""))


def cmd_daemon(_):
    QDIR.mkdir(parents=True, exist_ok=True)
    ADIR.mkdir(parents=True, exist_ok=True)
    inflight = {}  # sid -> (qid, proc, outpath, errpath, t0); one fork per session
    print("bus daemon up", flush=True)
    while True:
        for sid, (qid, p, outp, errp, t0) in list(inflight.items()):
            rc = p.poll()
            if rc is None:
                if time.time() - t0 > FORK_TIMEOUT:
                    p.kill()
                    write_answer(qid, sid, "(fork timed out)", ok=False)
                    del inflight[sid]
                continue
            text = outp.read_text().strip()
            if rc == 0 and text:
                write_answer(qid, sid, text)
            else:
                write_answer(qid, sid,
                             f"(fork failed rc={rc}) {errp.read_text()[-500:]}", ok=False)
            print(f"answered {qid} as {sid[:8]} rc={rc}", flush=True)
            del inflight[sid]

        live = live_sessions()
        for qf in sorted(QDIR.glob("*.json")):
            try:
                q = json.loads(qf.read_text())
            except ValueError:
                continue
            if time.time() - q["ts"] > MAX_Q_AGE:
                continue

            to = q["to"]
            if to != "all" and to not in live and to not in inflight:
                # Target session id is stale (session restarted) or gone.
                # Try re-resolving by project dir; else NACK so asker unblocks.
                answered = (ADIR / q["id"]).exists() and any((ADIR / q["id"]).iterdir())
                if not answered:
                    old_proj = _project_of(q["to"])
                    match = [s for s, c in live.items()
                             if old_proj and _project_of(s) == old_proj and s != q["from"]]
                    if match:
                        to = match[0]
                        print(f"rerouted {q['id']} {q['to'][:8]} -> {to[:8]}", flush=True)
                    elif time.time() - q["ts"] > NACK_AFTER:
                        listing = "\n".join(f"  {s}  {Path(c).name}" for s, c in live.items())
                        write_answer(q["id"], "bus-daemon",
                                     f"UNDELIVERABLE: session {q['to']} is not live "
                                     f"(it may have restarted with a new id). Live sessions:\n{listing}",
                                     ok=False)
                        print(f"NACKed {q['id']} (target {q['to'][:8]} gone)", flush=True)
                        continue

            for sid, cwd in live.items():
                if sid == q["from"] or to not in ("all", sid):
                    continue
                if sid in inflight:  # busy answering: new questions wait their turn
                    continue
                if (ADIR / q["id"] / f"{sid}.json").exists():
                    continue
                prior = len(answers_by(sid))
                inflight[sid] = spawn_fork(q, sid, cwd, prior)
                print(f"forking {sid[:8]} for {q['id']}", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sessions")
    a = sub.add_parser("ask")
    a.add_argument("text")
    a.add_argument("--to", help="target session id (default: all live sessions)")
    a.add_argument("--wait", type=int, default=1, help="answers to wait for (0 = fire and forget)")
    a.add_argument("--timeout", type=int, default=300)
    sub.add_parser("status")
    sub.add_parser("daemon")
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=20, help="show last N events")
    lg.add_argument("--follow", "-f", action="store_true")
    inj = sub.add_parser("inject")
    inj.add_argument("sid", help="target session id (must be live)")
    inj.add_argument("text", help="prompt to type into the session")
    sp = sub.add_parser("spawn")
    sp.add_argument("dir", help="folder for the new session (created if missing)")
    sp.add_argument("prompt", nargs="?", default="", help="optional kickoff prompt")
    args = ap.parse_args()
    {"sessions": cmd_sessions, "ask": cmd_ask, "status": cmd_status,
     "daemon": cmd_daemon, "log": cmd_log, "inject": cmd_inject,
     "spawn": cmd_spawn}[args.cmd](args)
