**My Claude sessions now talk to each other 🚌**

I run 8–10 Claude Code sessions in Warp at the same time. Today I gave them a message bus.

Any session can ask another session a question, or ask all of them. The target session does not stop. A fork of it answers (`claude -p --resume <id> --fork-session`), then exits. The backend is JSON files in one directory and a ~200-line daemon. No server. No ports.

A skill teaches each session the commands. Each session also sees a count of the answers its forks gave, so it can go read what it said.

The test that sold me: I left a note for one agent. The note said "you will need a dashboard; another session already builds one; follow up with it and reuse." The agent listed the live sessions, picked the right one, and asked it over the bus. The fork answered with the MLflow store path, the backfill script, and which component to reuse. I was not in that loop.

Screenshots attached. Code is ~350 lines total, happy to share.
