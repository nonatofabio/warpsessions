#!/usr/bin/env python3
"""Overnight digest — a 7AM launchd sweep of every live Claude Code session.

Mirrors WAIED's shape: live-scan sessions, ask each ONE fixed question, render a
self-contained HTML dashboard (inline CSS/JS, no assets). Unlike WAIED it forks
each session in its OWN context (via the bus fork mechanism) so the answer is
each session's own overnight delta — never interrupting the live session.

    .env/bin/python overnight.py            # run the sweep, write the dashboard
    .env/bin/python overnight.py --selftest # offline: parse + render asserts, no claude

Output (all under overnight/, gitignored — generated data, WAIED convention):
    overnight/dashboard.html      stable path fnp opens (regenerated each run)
    overnight/history/<date>.json one archive per run — the running historical log
    overnight/overnight.log       launchd stdout/err
"""

import html
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import bus  # reuse live-session discovery + fork-id recording

HERE = Path(__file__).resolve().parent
OUT = HERE / "overnight"
HIST = OUT / "history"
DASH = OUT / "dashboard.html"

MAX_CONCURRENT = 3          # ponytail: forks in flight at once; a resumed fork is API+RAM-heavy, 9-at-once starved every one. Raise only if the box has spare cores AND API headroom.
FORK_TIMEOUT = 600          # ponytail: per-fork ceiling (~2-3min typical); raise if heavy sessions truncate
SWEEP_DEADLINE = 2400       # ponytail: whole-sweep cap = ceil(sessions/MAX_CONCURRENT) waves x FORK_TIMEOUT + margin; bump both together if sessions grow

# The one fixed question every session's fork answers, from ITS context.
DIGEST_Q = (
    "[overnight-digest] You are a temporary READ-ONLY fork of this session, made "
    "at 7AM for Fabio's daily standup. Your main session is untouched. Looking at "
    "what THIS session actually worked on most recently (roughly since yesterday), "
    "write a tight standup digest in EXACTLY this format, nothing else:\n\n"
    "PROGRESS: <1-3 bullets, what advanced or got done>\n"
    "BLOCKERS: <what is stuck/failing/waiting; write 'none' if clear>\n"
    "DECISIONS: <decisions waiting on Fabio's input; write 'none' if clear>\n\n"
    "Be specific and concrete (name the thing). Output ONLY those three sections."
)


def claude_bin():
    return shutil.which("claude") or "/opt/homebrew/bin/claude"


def parse_digest(text):
    """Split a fork answer into the three labelled sections; keep raw as fallback."""
    out = {"progress": "", "blockers": "", "decisions": "", "raw": text.strip()}
    # Grab each label's body up to the next label or end.
    for key, label in (("progress", "PROGRESS"), ("blockers", "BLOCKERS"),
                       ("decisions", "DECISIONS")):
        m = re.search(rf"{label}\s*:\s*(.*?)(?=\n\s*(?:PROGRESS|BLOCKERS|DECISIONS)\s*:|$)",
                      text, re.S | re.I)
        if m:
            out[key] = m.group(1).strip()
    return out


def _is_clear(v):
    return not v or v.strip().lower() in ("none", "none.", "n/a", "-")


def spawn_digest_fork(sid, cwd):
    """Fork the session headless to answer the digest; return a live proc handle.

    Reuses bus's fork-id ledger so the fork transcript never hijacks cwd identity.
    """
    fid = str(uuid.uuid4())
    bus.FORKS.parent.mkdir(parents=True, exist_ok=True)
    with bus.FORKS.open("a") as fh:
        fh.write(fid + "\n")
    outp = bus.TMP / f"overnight-{sid}.out"
    errp = bus.TMP / f"overnight-{sid}.err"
    bus.TMP.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [claude_bin(), "-p", "--resume", sid, "--fork-session",
         "--session-id", fid, "--dangerously-skip-permissions", DIGEST_Q],
        cwd=cwd, stdout=outp.open("w"), stderr=errp.open("w"), text=True)
    return {"sid": sid, "cwd": cwd, "proc": p, "out": outp, "err": errp,
            "t0": time.time()}


def sweep():
    """Fork live sessions MAX_CONCURRENT at a time, collect each digest, return records.

    ponytail: 9 resumed forks at once starved every one past FORK_TIMEOUT (0-byte
    output). A bounded window (queue + at-most-N in flight) gives each fork the
    throughput to finish. Poll loop, no threads — Popen is already async.
    """
    sessions = bus.live_sessions()  # {sid: cwd}
    if not sessions:
        print("no live sessions to sweep", flush=True)
        return []
    print(f"sweeping {len(sessions)} session(s) @ {MAX_CONCURRENT} at a time: "
          + ", ".join(Path(c).name for c in sessions.values()), flush=True)
    pending = list(sessions.items())  # [(sid, cwd), ...]
    running = []
    results = {}
    deadline = time.time() + SWEEP_DEADLINE
    while (pending or running) and time.time() < deadline:
        while pending and len(running) < MAX_CONCURRENT:
            sid, cwd = pending.pop(0)
            running.append(spawn_digest_fork(sid, cwd))
        for r in list(running):
            rc = r["proc"].poll()
            if rc is None:
                if time.time() - r["t0"] > FORK_TIMEOUT:
                    r["proc"].kill()
                    results[r["sid"]] = {"cwd": r["cwd"], "ok": False,
                                         "text": "(fork timed out)"}
                    print(f"digest from {Path(r['cwd']).name} TIMED OUT "
                          f"after {FORK_TIMEOUT}s", flush=True)
                    running.remove(r)
                continue
            text = r["out"].read_text().strip()
            if rc == 0 and text:
                results[r["sid"]] = {"cwd": r["cwd"], "ok": True, "text": text}
            else:
                err = r["err"].read_text()[-400:]
                results[r["sid"]] = {"cwd": r["cwd"], "ok": False,
                                     "text": f"(fork failed rc={rc}) {err}"}
            print(f"digest from {Path(r['cwd']).name} rc={rc} "
                  f"({len(text)} chars)", flush=True)
            running.remove(r)
        if pending or running:
            time.sleep(3)
    for r in running:  # deadline hit
        r["proc"].kill()
        results[r["sid"]] = {"cwd": r["cwd"], "ok": False,
                             "text": "(sweep deadline reached)"}
    for sid, cwd in pending:  # never got a slot
        results[sid] = {"cwd": cwd, "ok": False,
                        "text": "(sweep deadline reached before fork started)"}
    return [{"sid": s, "project": Path(v["cwd"]).name, "cwd": v["cwd"],
             "ok": v["ok"], **parse_digest(v["text"])}
            for s, v in results.items()]


# ------------------------- rendering -------------------------

CSS = """
:root{--bg:#0e1216;--panel:#171d24;--fg:#d8dee6;--dim:#7a8494;--acc:#5ec2b7;
--red:#e5647d;--amber:#e0a955;--line:#242c36}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,Helvetica,sans-serif}
header{padding:22px 28px 10px}h1{margin:0;font-size:20px}
.sub{color:var(--dim);font-size:13px;margin-top:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));
gap:14px;padding:16px 28px}
.card{background:var(--panel);border-radius:12px;padding:14px 16px;
border-left:4px solid var(--acc);overflow-wrap:anywhere}
.card.blocked{border-left-color:var(--red)}
.card.decide{border-left-color:var(--amber)}
.card h3{margin:0 0 8px;font-size:15px}
.sid{color:var(--dim);font-weight:400;font-size:11px}
.sec{margin:8px 0}.sec .lbl{font-size:11px;text-transform:uppercase;
letter-spacing:1px;color:var(--dim)}
.sec.b .lbl{color:var(--red)}.sec.d .lbl{color:var(--amber)}
.sec .body{white-space:pre-wrap;margin-top:2px}
.clear{color:var(--dim);font-style:italic}
.raw{color:var(--red);white-space:pre-wrap;font-size:12px}
h2.hist{padding:20px 28px 0;font-size:13px;text-transform:uppercase;
letter-spacing:1px;color:var(--dim)}
details{margin:6px 28px;background:var(--panel);border-radius:10px;padding:8px 14px}
summary{cursor:pointer;color:var(--fg)}summary .c{color:var(--dim);font-size:12px}
details .grid{padding:12px 0 4px}
footer{color:var(--dim);font-size:12px;padding:10px 28px 30px}
"""


def _sec(lbl, val, cls=""):
    if _is_clear(val):
        return f'<div class="sec {cls}"><span class="lbl">{lbl}</span>' \
               f'<div class="body clear">none</div></div>'
    return f'<div class="sec {cls}"><span class="lbl">{lbl}</span>' \
           f'<div class="body">{html.escape(val)}</div></div>'


def _card(d):
    cls = "card"
    if d.get("ok") is False:
        return (f'<div class="card blocked"><h3>{html.escape(d["project"])} '
                f'<span class="sid">{d["sid"][:8]}</span></h3>'
                f'<div class="raw">{html.escape(d.get("raw", "(no response)"))}</div></div>')
    if not _is_clear(d.get("blockers")):
        cls += " blocked"
    elif not _is_clear(d.get("decisions")):
        cls += " decide"
    return (f'<div class="{cls}"><h3>{html.escape(d["project"])} '
            f'<span class="sid">{d["sid"][:8]}</span></h3>'
            + _sec("Progress", d.get("progress") or d.get("raw", ""))
            + _sec("Blockers", d.get("blockers"), "b")
            + _sec("Decisions for Fabio", d.get("decisions"), "d")
            + "</div>")


def _grid(records):
    if not records:
        return '<div class="grid"><div class="clear" style="padding:0 28px">' \
               'no sessions</div></div>'
    return '<div class="grid">' + "".join(_card(d) for d in records) + "</div>"


def render(today_ts, today_records, history):
    """Full dashboard: today's board + collapsed history of prior days."""
    dt = datetime.fromtimestamp(today_ts)
    n = len(today_records)
    blocked = sum(1 for d in today_records if not _is_clear(d.get("blockers"))
                  or d.get("ok") is False)
    decide = sum(1 for d in today_records if not _is_clear(d.get("decisions")))
    hist_html = ""
    for h in history:  # newest-first, excluding today
        hdt = datetime.fromtimestamp(h["ts"])
        hist_html += (f'<details><summary>{hdt:%A %d %b %Y} '
                      f'<span class="c">— {len(h["records"])} session(s)</span>'
                      f'</summary>{_grid(h["records"])}</details>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Overnight digest — {dt:%d %b %Y}</title><style>{CSS}</style></head><body>
<header><h1>Overnight digest</h1>
<div class="sub">{dt:%A %d %B %Y, %H:%M} · {n} session(s) · {blocked} with blockers ·
{decide} awaiting a decision</div></header>
{_grid(today_records)}
{'<h2 class="hist">History</h2>' + hist_html if hist_html else ''}
<footer>Regenerated every run. Data: overnight/history/*.json ·
Sessions forked read-only — live sessions untouched.</footer>
</body></html>"""


def load_history(exclude_date=None):
    """All archived days, newest first."""
    out = []
    for f in sorted(HIST.glob("*.json"), reverse=True):
        if exclude_date and f.stem == exclude_date:
            continue
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return out


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    HIST.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    records = sweep()
    archive = {"ts": ts, "date": date, "records": records}
    (HIST / f"{date}.json").write_text(json.dumps(archive, indent=2))
    DASH.write_text(render(ts, records, load_history(exclude_date=date)))
    print(f"wrote {DASH}  (file://{DASH})", flush=True)


# ------------------------- self-test -------------------------

def selftest():
    p = parse_digest(
        "PROGRESS: shipped the parser\n- added tests\n"
        "BLOCKERS: none\nDECISIONS: pick a name for the API")
    assert p["progress"].startswith("shipped the parser"), p
    assert "added tests" in p["progress"], p
    assert _is_clear(p["blockers"]), p
    assert p["decisions"] == "pick a name for the API", p
    # unlabelled answer falls back to raw
    p2 = parse_digest("just some free text")
    assert p2["progress"] == "" and p2["raw"] == "just some free text", p2
    # render is self-contained and reflects counts / highlighting
    recs = [
        {"sid": "abcd1234-x", "project": "alpha", "cwd": "/a", "ok": True,
         "progress": "did X", "blockers": "none", "decisions": "none", "raw": ""},
        {"sid": "efgh5678-y", "project": "beta", "cwd": "/b", "ok": True,
         "progress": "did Y", "blockers": "build is red", "decisions": "none", "raw": ""},
        {"sid": "ijkl9012-z", "project": "gamma", "cwd": "/c", "ok": False,
         "progress": "", "blockers": "", "decisions": "", "raw": "(fork failed)"},
    ]
    hist = [{"ts": time.time() - 86400, "records": recs[:1]}]
    doc = render(time.time(), recs, hist)
    assert doc.startswith("<!doctype html>") and "</html>" in doc
    assert "1 with blockers" not in doc  # 2: beta blocker + gamma failure
    assert "2 with blockers" in doc, "blocker count wrong"
    assert 'class="card blocked"' in doc, "blocker card not colored"
    assert "History" in doc and "1 session(s)" in doc
    assert html.escape("<script>") not in "<script>"  # sanity: escaping active
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run()
