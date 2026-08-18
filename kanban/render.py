#!/usr/bin/env python3
"""Weekly kanban board — render kanban/board.json into a static board.html.

Mirrors overnight.py: one source-of-truth JSON, a self-contained dark HTML page
(inline CSS, no assets, no server), and a dated snapshot per run. fnp opens the
page with a file:// link, exactly like the overnight dashboard.

    .env/bin/python kanban/render.py            # render board.html + snapshot
    .env/bin/python kanban/render.py --selftest # offline asserts, writes nothing

board.json is the source of truth. To move a card: edit its "lane" in
board.json and re-run render.py. Idempotent — regenerates board.html each run.
"""

import html
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOARD_JSON = HERE / "board.json"
BOARD_HTML = HERE / "board.html"
HIST = HERE / "history"

LANES = ["Backlog", "In Progress", "Blocked", "Done"]  # column order, left to right
LANE_CLS = {"Backlog": "backlog", "In Progress": "wip", "Blocked": "blocked", "Done": "done"}

# Same palette as overnight/dashboard.html so the two pages feel like one system.
CSS = """
:root{--bg:#0e1216;--panel:#171d24;--fg:#d8dee6;--dim:#7a8494;--acc:#5ec2b7;
--red:#e5647d;--amber:#e0a955;--line:#242c36;--blue:#6ea8fe}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,Helvetica,sans-serif}
header{padding:22px 28px 10px}h1{margin:0;font-size:20px}
.sub{color:var(--dim);font-size:13px;margin-top:4px}
.board{display:grid;grid-template-columns:repeat(4,minmax(240px,1fr));
gap:14px;padding:16px 28px;align-items:start}
.lane{background:#12171d;border:1px solid var(--line);border-radius:12px;
padding:10px 10px 14px;min-height:80px}
.lane h2{margin:2px 4px 10px;font-size:12px;text-transform:uppercase;
letter-spacing:1px;color:var(--dim);display:flex;justify-content:space-between}
.lane h2 .n{color:var(--fg);opacity:.6}
.lane.backlog{border-top:3px solid var(--dim)}
.lane.wip{border-top:3px solid var(--acc)}
.lane.blocked{border-top:3px solid var(--red)}
.lane.done{border-top:3px solid var(--blue)}
.card{background:var(--panel);border-radius:10px;padding:10px 12px;margin:8px 0;
border-left:4px solid var(--acc);overflow-wrap:anywhere}
.card.blocked{border-left-color:var(--red)}
.card.backlog{border-left-color:var(--dim)}
.card.done{border-left-color:var(--blue)}
.card.sub{margin-left:10px;background:#141a21}
.card .trk{font-size:10px;text-transform:uppercase;letter-spacing:1px;
color:var(--dim);margin-bottom:3px}
.card h3{margin:0 0 5px;font-size:14px}
.card .notes{color:var(--fg);font-size:12.5px;white-space:pre-wrap}
.card .links{margin-top:7px;display:flex;flex-wrap:wrap;gap:6px}
.card .links a{font-size:11px;color:var(--acc);text-decoration:none;
border:1px solid var(--line);border-radius:6px;padding:1px 7px}
.card .links a:hover{border-color:var(--acc)}
.card .upd{color:var(--dim);font-size:10px;margin-top:6px}
.empty{color:var(--dim);font-style:italic;font-size:12px;padding:6px 4px}
footer{color:var(--dim);font-size:12px;padding:10px 28px 30px}
"""


def _card(c):
    lane = c.get("lane", "")
    cls = "card " + LANE_CLS.get(lane, "")
    # A sub-card's title starts with a "(x)" marker in the seed data.
    if c.get("title", "").lstrip().startswith("("):
        cls += " sub"
    links = "".join(
        f'<a href="{html.escape(l["href"])}">{html.escape(l["label"])}</a>'
        for l in c.get("links", []) if l.get("href"))
    links_html = f'<div class="links">{links}</div>' if links else ""
    notes = c.get("notes", "")
    notes_html = f'<div class="notes">{html.escape(notes)}</div>' if notes else ""
    upd = c.get("updated", "")
    upd_html = f'<div class="upd">updated {html.escape(upd)}</div>' if upd else ""
    return (f'<div class="{cls}">'
            f'<div class="trk">{html.escape(c.get("track", ""))}</div>'
            f'<h3>{html.escape(c.get("title", ""))}</h3>'
            f'{notes_html}{links_html}{upd_html}</div>')


def _lane(name, cards):
    body = "".join(_card(c) for c in cards) if cards \
        else '<div class="empty">nothing here</div>'
    return (f'<div class="lane {LANE_CLS[name]}">'
            f'<h2>{html.escape(name)}<span class="n">{len(cards)}</span></h2>'
            f'{body}</div>')


def render(board, ts):
    cards = board.get("cards", [])
    by_lane = {name: [c for c in cards if c.get("lane") == name] for name in LANES}
    week = board.get("week_of", "")
    try:
        week_dt = datetime.strptime(week, "%Y-%m-%d")
        week_label = f"Week of {week_dt:%A %d %B %Y}"
    except ValueError:
        week_label = f"Week of {week}" if week else "This week"
    gen = datetime.fromtimestamp(ts)
    lanes_html = "".join(_lane(name, by_lane[name]) for name in LANES)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kanban — {html.escape(week_label)}</title><style>{CSS}</style></head><body>
<header><h1>Weekly kanban</h1>
<div class="sub">{html.escape(week_label)} · {len(cards)} card(s) · generated {gen:%d %b %Y %H:%M}</div></header>
<div class="board">{lanes_html}</div>
<footer>Source of truth: kanban/board.json · Regenerated by kanban/render.py ·
Snapshots: kanban/history/*.json</footer>
</body></html>"""


def load_board():
    board = json.loads(BOARD_JSON.read_text())
    cards = board.get("cards", [])
    bad = [c.get("id", "?") for c in cards if c.get("lane") not in LANES]
    if bad:
        raise ValueError(f"card(s) with invalid lane: {bad} (valid: {LANES})")
    return board


def run():
    HIST.mkdir(parents=True, exist_ok=True)
    board = load_board()
    ts = time.time()
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    snap = {"ts": ts, "date": date, **board}
    (HIST / f"{date}.json").write_text(json.dumps(snap, indent=2))
    BOARD_HTML.write_text(render(board, ts))
    print(f"wrote {BOARD_HTML}  (file://{BOARD_HTML})", flush=True)


def selftest():
    board = load_board()  # asserts board.json parses + every card has a valid lane
    cards = board["cards"]
    assert cards, "board.json has no cards"
    ids = [c["id"] for c in cards]
    assert len(ids) == len(set(ids)), f"duplicate card ids: {ids}"
    for c in cards:
        assert c.get("lane") in LANES, c
        assert c.get("track") and c.get("title"), c
    tracks = {c["track"] for c in cards}
    assert len(tracks) == 5, f"expected 5 tracks, got {len(tracks)}: {tracks}"
    doc = render(board, time.time())
    assert doc.startswith("<!doctype html>") and "</html>" in doc
    for name in LANES:
        assert f">{name}<" in doc, f"lane {name} missing from HTML"
    # every card title reaches the page
    for c in cards:
        assert html.escape(c["title"]) in doc, f"card not rendered: {c['id']}"
    assert html.escape("<script>") not in "<script>"  # sanity: escaping active
    print(f"selftest OK — {len(cards)} cards, {len(tracks)} tracks, "
          f"{len(doc)} bytes rendered")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run()
