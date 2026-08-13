"""Menu bar switcher for Claude Code sessions running in Warp.

For each running `claude` process:
  - its Warp tab is identified via the WARP_TERMINAL_SESSION_UUID env var
  - its topic comes from the session transcript in ~/.claude/projects/
Clicking a menu item opens warp://session/<uuid>, which focuses the exact
tab even across workspaces. No Accessibility permissions needed.
"""

import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import rumps

REFRESH_SECS = 30
WARP_DB = (Path.home() / "Library/Group Containers/2BBY89MBSN.dev.warp"
           / "Library/Application Support/dev.warp.Warp-Stable/warp.sqlite")


def live_warp_pane_uuids():
    """Set of pane uuids Warp currently has open (read-only DB query).

    Returns None if the DB can't be read, meaning 'unknown, don't filter'.
    """
    try:
        con = sqlite3.connect(f"file:{WARP_DB}?mode=ro", uri=True, timeout=1)
        try:
            return {row[0].lower() for row in
                    con.execute("SELECT hex(uuid) FROM terminal_panes")}
        finally:
            con.close()
    except sqlite3.Error:
        return None


def running_claude_sessions():
    """[{uuid, cwd, pid}] for every claude process inside a Warp tab."""
    pids = subprocess.run(
        ["pgrep", "-x", "claude"], capture_output=True, text=True
    ).stdout.split()
    sessions = []
    for pid in pids:
        env = subprocess.run(
            ["ps", "-p", pid, "-wwwE", "-o", "command="],
            capture_output=True, text=True,
        ).stdout
        m = re.search(r"WARP_TERMINAL_SESSION_UUID=([0-9a-f]+)", env)
        if not m:
            continue  # claude running outside Warp
        cwd_out = subprocess.run(
            ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
            capture_output=True, text=True,
        ).stdout
        cm = re.search(r"^n(.+)$", cwd_out, re.M)
        cwd = cm.group(1) if cm else "?"
        sessions.append({"uuid": m.group(1), "cwd": cwd, "pid": pid})
    return sessions


# Session-metadata lines that say nothing about turn state.
_META_TYPES = {"last-prompt", "ai-title", "mode", "permission-mode", "summary",
               "file-history-snapshot", "queued-command"}
BLOCKED_AFTER_SECS = 90


def _tail_entries(path, nbytes=262144):
    """Parsed JSON entries from the last nbytes of a transcript."""
    entries = []
    with open(path, "rb") as fh:
        fh.seek(max(0, path.stat().st_size - nbytes))
        chunk = fh.read().decode("utf-8", errors="replace")
    for line in chunk.splitlines()[1:] if path.stat().st_size > nbytes else chunk.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def session_status(cwd):
    """(topic, state) from the newest transcript for this cwd.

    state: 'waiting' (turn done, wants your input), 'blocked' (pending tool
    call and the file went quiet — likely a permission prompt), 'working'.
    """
    proj = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    files = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None, "working"
    # ponytail: newest transcript wins; two sessions in one cwd share topic+state
    path = files[0]
    try:
        entries = _tail_entries(path)
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None, "working"

    topic = None
    for e in reversed(entries):  # prefer the real recap Claude wrote
        if e.get("type") == "system" and e.get("subtype") == "away_summary":
            topic = e.get("content")
            break
        if e.get("type") == "summary" and e.get("summary"):
            topic = e["summary"]
            break
    if not topic:
        for e in entries:  # fall back to the first real user prompt
            if e.get("type") == "user" and not e.get("isMeta"):
                c = e.get("message", {}).get("content")
                if isinstance(c, list):
                    c = next((b.get("text") for b in c
                              if isinstance(b, dict) and b.get("type") == "text"), None)
                if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                    topic = " ".join(c.split())
                    break

    state = "working"
    for e in reversed(entries):  # last entry that reflects turn state
        t = e.get("type")
        if t in _META_TYPES:
            continue
        if t == "system":
            if e.get("subtype") in ("turn_duration", "stop_hook_summary", "away_summary"):
                state = "waiting"
            break
        if t == "assistant":
            blocks = e.get("message", {}).get("content", [])
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use"
                               for b in blocks)
            if has_tool_use and age > BLOCKED_AFTER_SECS:
                state = "blocked"  # tool call issued, no result, file went quiet
            break
        break  # user / tool_result: Claude is processing

    # Deterministic override: the Notification hook records permission prompts
    # and idle waits per session id (= transcript filename).
    hook_state = _hook_state(path.stem, path.stat().st_mtime)
    return topic, hook_state or state


def _hook_state(session_id, transcript_mtime):
    """State from the Notification-hook file, if fresher than the transcript."""
    f = Path.home() / ".claude" / "session-states" / f"{session_id}.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    if d.get("ts", 0) < transcript_mtime - 2:
        return None  # transcript moved on since the notification
    ntype = d.get("notification_type", "")
    if "permission" in ntype:
        return "blocked"
    if "idle" in ntype:
        return "waiting"
    return None


STATE_MARK = {"waiting": "", "blocked": "🔴 ", "working": "✳ "}


def build_session_list():
    """[(name, topic, state, uuid)] sorted by project name, deduped by tab uuid."""
    live = live_warp_pane_uuids()
    items, seen = [], set()
    for s in running_claude_sessions():
        if s["uuid"] in seen:
            continue  # split panes in one tab share the uuid
        if live is not None and s["uuid"] not in live:
            continue  # tab was closed; claude may linger as an orphan
        seen.add(s["uuid"])
        name = Path(s["cwd"]).name or s["cwd"]
        topic, state = session_status(s["cwd"])
        items.append((name, topic or "", state, s["uuid"]))
    return sorted(items)


def wrap(text, width=55, max_lines=6):
    """Wrap text into menu-item-sized lines."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] += " …"
    return lines


class WarpSessionManager(rumps.App):
    def __init__(self):
        super().__init__("💬")
        self.refresh(None)
        rumps.Timer(self.refresh, REFRESH_SECS).start()

    def refresh(self, _):
        sessions = build_session_list()
        self.title = f"💬 {len(sessions)}" if sessions else "💬"

        # Rebuild from scratch: rumps keys the menu by title, so duplicate
        # labels leave orphaned NSMenu items behind on incremental deletes.
        self.menu.clear()
        self.menu.add(rumps.MenuItem("Refresh now", callback=self.refresh))
        self.menu.add(rumps.separator)
        if not sessions:
            self.menu.add(rumps.MenuItem("No Claude sessions in Warp"))
            return
        seen_labels = {}
        for name, topic, state, uuid in sessions:
            n = seen_labels[name] = seen_labels.get(name, 0) + 1
            label = f"{name} ({n})" if n > 1 else name
            label = STATE_MARK[state] + label

            parent = rumps.MenuItem(label, callback=self.jump)
            parent.session_uuid = uuid

            sub = []
            go = rumps.MenuItem("→ Go to tab", callback=self.jump)
            go.session_uuid = uuid
            sub.append(go)
            if topic:
                sub.append(rumps.separator)
                sub.extend(rumps.MenuItem(line) for line in wrap(topic))
            parent.update(sub)
            self.menu.add(parent)
        self.menu.add(rumps.separator)
        self.menu.add(self._bus_menu())
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application()))

    def _bus_menu(self):
        """Bus submenu: daemon status + last few Q/A events."""
        import bus
        daemon_up = subprocess.run(["pgrep", "-f", "bus.py daemon"],
                                   capture_output=True).returncode == 0
        events = bus.bus_events(limit=8)
        parent = rumps.MenuItem(f"🚌 Bus {'●' if daemon_up else '○ (daemon down)'}")
        sub = [rumps.MenuItem("Open live log in Warp", callback=self._bus_log)]
        if events:
            live = {sid: Path(cwd).name for sid, cwd in bus.live_sessions().items()}
            sub.append(rumps.separator)
            for ts, kind, qid, who, text, extra in reversed(events):
                t = time.strftime("%H:%M", time.localtime(ts))
                name = live.get(who, who[:8])
                icon = "❓" if kind == "Q" else ("💬" if extra != "ERROR" else "⚠️")
                head = f"{t} {icon} {name}: {text}"
                sub.append(rumps.MenuItem(head[:80] + ("…" if len(head) > 80 else "")))
        else:
            sub.append(rumps.MenuItem("no bus traffic yet"))
        parent.update(sub)
        return parent

    def _bus_log(self, _):
        script = ('tell application "Warp" to activate\n'
                  'tell application "System Events" to tell process "Warp"\n'
                  ' keystroke "t" using {command down}\n delay 0.5\n'
                  f' keystroke "{sys.executable} {Path(__file__).parent / "bus.py"} log -f"\n'
                  ' key code 36\nend tell')
        subprocess.run(["osascript", "-e", script])

    def jump(self, sender):
        subprocess.run(["open", f"warp://session/{sender.session_uuid}"])


if __name__ == "__main__":
    if "--list" in sys.argv:  # self-check without starting the app
        for name, topic, state, uuid in build_session_list():
            print(f"{uuid}  [{state:7}] {name}: {topic[:70]}")
        sys.exit(0)
    WarpSessionManager().run()
