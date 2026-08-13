# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file macOS menu bar app (`app.py`) that lists every Claude Code session running inside Warp and jumps to its exact tab on click, across workspaces. Built on `rumps`.

## How it works

- Session discovery: `pgrep -x claude`, then read each process's environment (`ps -wwwE`) for `WARP_TERMINAL_SESSION_UUID` — Warp injects this into every tab's shell. Uuids are then cross-checked against Warp's live pane list (read-only query of `terminal_panes` in its sqlite DB) so orphaned claude processes from closed tabs don't produce dead links.
- Menu layout: top level shows just the project name; hovering opens a submenu with "→ Go to tab" plus the wrapped summary text. Clicking the top-level item also jumps.
- Topic + state: newest `*.jsonl` transcript in `~/.claude/projects/<cwd-with-non-alnum-as-dashes>/`. Topic prefers Claude's own recap (`system/away_summary` entry, then `summary` entries), falls back to the first user message. State comes from the last non-meta entry: trailing `system` (`turn_duration`/`stop_hook_summary`/`away_summary`) → waiting for input (🔵); trailing `assistant` with an unanswered `tool_use` and the file quiet > 90s → blocked, likely a permission prompt (🔴); anything else → working (no mark). Only the transcript tail is read (last 256KB).
- Deterministic state override: a global Notification hook (`~/.claude/hooks/record-session-state.sh`, wired in `~/.claude/settings.json`) writes each session's latest notification payload to `~/.claude/session-states/<session_id>.json`. `_hook_state()` reads it and overrides the heuristic: `notification_type` containing "permission" → blocked, "idle" → waiting. Ignored when older than the transcript's mtime (transcript moved on). Hook events are NOT written to the transcript itself, hence the side file. Sessions started before the hook was added (2026-08-10) won't emit these until restarted.
- Jump: `open warp://session/<uuid>` — Warp's own deep link focuses the precise tab/window. No Accessibility permission, no AppleScript keystrokes.
- Auto-refreshes every 30s (`REFRESH_SECS`); menu bar title shows the session count.

## Running

```bash
.env/bin/python app.py          # venv lives in .env/ (a virtualenv, not a dotenv file)
.env/bin/python app.py --list   # self-check: print detected sessions, no GUI
```

No tests beyond `--list`, no lint config, no build step.

## bus.py — inter-session bus

File-based bus letting live Claude sessions ask each other questions (`~/.claude/bus/`: `q/` questions, `a/<qid>/` answers, `forks.txt` fork ids, `tmp/` fork stdout/err). A daemon (`bus.py daemon`) watches `q/` and answers on behalf of each target session by forking it: `claude -p --resume <sid> --fork-session --session-id <fresh-uuid>` run in the target's cwd — the live session is never touched. One in-flight fork per session (queue, no fork storms); 300s fork timeout; questions older than 2h ignored. Session identity = newest non-fork transcript in the cwd's project dir (fork ids from forks.txt are excluded, so forks don't hijack identity). `ask` prints how many questions your own forks have answered (metadata for self-awareness); `status` lists them. The `bus` skill (~/.claude/skills/bus/SKILL.md) teaches sessions the commands. Daemon: `nohup .env/bin/python bus.py daemon >/tmp/bus-daemon.log 2>&1 &`.

## Notes

- Warp's live tab state is also readable (read-only!) at `~/Library/Group Containers/2BBY89MBSN.dev.warp/Library/Application Support/dev.warp.Warp-Stable/warp.sqlite` (tables: windows, tabs, pane_nodes, pane_leaves, terminal_panes with cwd) — not currently needed, but useful if per-tab metadata beyond the uuid is ever wanted. Never open it read-write while Warp runs.
- Two sessions in the same cwd currently get the same topic (newest transcript wins) — fix by matching transcript to process start time if it ever matters.
