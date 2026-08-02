# myagents

**A memory for your coding agent, and a way to tell finished from claimed-finished.**

myagents watches your Claude Code sessions from the outside. It records what was
actually asked, what files were actually changed, and what commands were actually
run — then hands that back to the agent at the start of every message, so it stops
forgetting what you were doing.

It also draws a line the agent cannot cross on its own: a task turns green **only**
when a command was executed and exited 0. Nothing else counts. Not the agent saying
"done". Not a summary that reads like success.

macOS · Python 3.12+ · no dependencies · nothing leaves your machine.

---

## Why this exists

Three failures, all observed, none hypothetical:

1. **Context gets compacted, and work gets lost.** Long session, the window fills,
   the history is summarized. What you asked forty minutes ago is gone.
2. **You can't remember where each project stands.** With a handful of repos in
   flight, "what was I doing here?" costs real time, every time.
3. **The agent says "done" for things that aren't.** This is the dangerous one,
   because it's *invisible*. It disappears into the conversation and nobody audits it.

The third is what the project is really about. An agent declaring success is not
evidence of success. myagents makes the difference visible — not by trusting a model
less, but by only counting what it watched happen.

---

## What it actually does

```
   ┌── your Claude Code session ─────────────────────────────────┐
   │                                                             │
   │  hook ──append──▶ spool/*.jsonl        (<1ms, never locks)  │
   │  hook ──read────▶ injection/*.txt      (a file, not the DB) │
   │                                                             │
   └─────────────────────────────────────────────────────────────┘
                              │
                    drainer (the only writer)
                              ▼
                     ~/.myagents/tasks.db
                              ▲
              ┌───────────────┼───────────────┐
          dashboard        menu bar        reviewer
                                        (claude -p, sandboxed)
```

**Capture.** Hooks append one JSON line per event and exit. They never open SQLite,
never take a lock, never block you. If myagents breaks, your session doesn't notice.

**A note back to the agent.** Every message, the agent receives a short status line
for the project you're in — open tasks, anything claimed but unproven, what changed
recently. Capped, pre-rendered, read from a plain file. It's state, not history.

```
[myagents · my-project]
Aperti: rewrite the pricing page · PR-B2 pending merge
⚠ Dichiarato fatto ma non verificato: "cache invalidation" (2gg)
  Per chiuderli serve una prova eseguita: tk verify <chiave> --cmd '<comando>'
Ultima attività 3h fa · 12 modifiche in src/components
```

**Verification.** `tk verify` runs your command, captures the real exit code, and
records the current commit plus a fingerprint of the files involved. Exit 0 is the
only path to green. If those files change afterwards the task goes yellow again — a
green from three days ago on code changed yesterday isn't a verification, it's a memory.

**The reviewer.** When a session ends, a sandboxed `claude -p` reads the raw notes and
proposes tasks. It runs with no tools at all, because it reads untrusted text — anything
you paste into a chat passes through it. It can create tasks, and it can move a task to
*yellow* when the session's own transcript shows the work was claimed done. It can never
mark anything verified.

**Tasks have a way out.** A list that only grows stops being a to-do list and becomes
noise, and noise gets ignored. Every ten minutes the service re-runs the verify commands
it already knows: a task whose command now passes closes itself, and a green task whose
command stops passing reopens. No model decides this — an exit code does.

**Nothing gets lost mid-turn.** Messages you send *while* the agent is working never
fire the prompt hook — they're queued by the harness. Those are exactly the requests
most likely to be forgotten, and myagents used to lose all of them. It now recovers
them from the session transcript.

**Terminals.** myagents knows which terminal windows are open on which project, and
brings the existing one to the front instead of opening a seventh.

**Agent workflows.** If you run multi-agent workflows, the dashboard shows them live:
who's running, who finished, each agent named from its own prompt.

---

## Install

```bash
git clone https://github.com/<you>/myagents.git
cd myagents
python3 -m venv venv && ./venv/bin/pip install -e .
./venv/bin/tk install     # registers hooks in every ~/.claude* config dir
./venv/bin/tk doctor      # confirm it's alive
```

`tk install` finds your Claude Code config directories on its own. If you use aliases
with `CLAUDE_CONFIG_DIR` (`~/.claude-work`, `~/.claude-client`, …) it covers all of
them — hardcoding the list means silently missing one the day you add another. Safe to
re-run.

Then just work. Data shows up within about twenty seconds.

Optional menu bar icon:

```bash
./venv/bin/pip install -e ".[bar]"   # adds rumps
./venv/bin/tk-bar
```

---

## Use

```bash
tk list                             # tasks, all projects
tk list --project NAME
tk projects                         # projects with open counts
tk verify KEY --cmd "pytest -q"     # run it, record it — the only road to green
tk cura                             # run the reviewer now, and report what it did
tk riverifica                       # re-run known verify commands, update states
tk terminali                        # open terminals and where they are
tk dash                             # local service + dashboard on 127.0.0.1:7777
tk doctor                           # health: db, spool, kill switch, error log
tk drain                            # apply the spool by hand
tk uninstall                        # remove the hooks
```

The dashboard shows every project, its tasks, the files actually touched, the last
real request, live agent workflows, and one-click confirm / archive / reopen.

### The icon is the information

| Icon | Meaning |
|---|---|
| `●` | nothing pending |
| `● 7` | seven open tasks, all tracked |
| `⚠ 3` | three declared done with no proof — **the reason this exists** |
| `◌` | capture paused |
| `⃠` | service down: capture continues, updating doesn't |

---

## Settings

| Variable | Effect |
|---|---|
| `MYAGENTS_OFF=1` | kill switch: hooks fire and do nothing |
| `MYAGENTS_HOME` | where data lives (default `~/.myagents`) |
| `MYAGENTS_CLAUDE` | path to the `claude` binary, if it isn't on `PATH` |

The menu bar can pause capture instantly via a sentinel file — an environment variable
only affects processes started afterwards, and pausing should also stop sessions you
already have open.

Everything lives under `~/.myagents/`, deliberately outside every Claude Code config
directory. Delete that folder and myagents is gone.

---

## Design rules

**A bug here must never break your session.** Every hook wraps everything, always exits
0, never writes to stderr, and logs to `~/.myagents/hook-errors.log`.

**The hot path never touches SQLite.** WAL mode allows concurrent readers but a single
writer. With parallel sessions plus a service plus a reviewer, writes serialize — and a
lock timeout combined with silent failure means silently losing your source of truth.
Hooks append to a spool; exactly one process drains it.

**The model proposes; it never decides.** `stale` is arithmetic on a timestamp.
`verified` requires evidence the harness collected by executing something. The reviewer
suggests, and does nothing else.

**Honest threat model.** `evidence.source` is an anti-sloppiness mechanism, not an
anti-lying one. A process with your privileges can write any row it likes, and no amount
of design fixes that without a privilege boundary this project doesn't have. The value
is in making the difference between *done* and *said-done* **visible**, not impossible.

---

## What it doesn't do

No cloud sync. No multi-user. No Jira or GitHub integration. No estimates, no burndown,
no task dependencies. And deliberately **no agent that re-reads your code to judge
whether the work is done**: that costs a lot and gets it wrong. An exit code costs
nothing and doesn't lie.

---

## Costs and limits

- **Capture is free.** One appended line and one small file read per event.
- **The reviewer costs one `claude -p` call per session**, billed to your Claude Code
  subscription. Measured at roughly $0.25/session — the model loads your installed
  plugins and skills into context. It deliberately handles only a couple of sessions
  at a time.
- **macOS only.** Terminal detection and the menu bar are AppleScript and PyObjC.
- **Capture is imperfect.** It produces false negatives and the occasional phantom
  task. The comparison isn't against a perfect system; it's against the current state,
  whose recall is zero. Noise is archived in one click.

---

## Development

```bash
./venv/bin/python -m pytest -q     # 129 tests
```

Tests never touch your real config directories — `tests/conftest.py` redirects
`MYAGENTS_HOME` and refuses to write outside its sandbox.

Worth reading before changing anything:

- [`SPEC.md`](SPEC.md) — the full specification, and the reasoning behind each decision
- [`docs/findings-fase-1.md`](docs/findings-fase-1.md) — measured hook payloads,
  including the discovery that Claude Code exposes **no exit code**, which is why
  verification executes instead of observing
- [`docs/findings-fase-2.md`](docs/findings-fase-2.md) — the context injection contract,
  and why emitting two formats "to be safe" doubles your token cost forever

The code and its comments are in Italian. The reasoning lives in those comments — if
you're going to change something, read them first.

---

## License

MIT — see [LICENSE](LICENSE).
