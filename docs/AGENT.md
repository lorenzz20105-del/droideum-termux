# Agent mode — driving the Android VM from an LLM

droideum's VM is built to be **operated by an AI agent**, not tapped by a human. There is no
requirement to look at a VNC window: every observation is machine-readable, every action is a tool
call, and the whole device can be checkpointed and rolled back.

```
your agent  ──MCP (JSON-RPC over stdio)──▶  droideum-agentd  ──adb──▶  Android VM (QEMU/TGC)
                26 tools                     in proot aarch64           inside proot aarch64
```

`droideum-agentd` is deliberately **stdlib-only Python 3** (no pip, no wheels to compile on a phone),
so it survives any `proot-distro` rebuild.

---

## 1. Start it

```bash
droideum setup              # once: qemu + adb + python3 in the container, Android image, disk
droideum install            # once: install Android into the disk (needs a screen — do it with VNC)
droideum up                 # headless boot; blocks until Android answers adb
droideum agent serve --autostart   # MCP server on stdio (for your agent)
```

`droideum up` is the agent-friendly boot: `DISPLAY_BACKEND=none`, no VNC, and it polls
`sys.boot_completed` so your agent never races a half-booted device.

### Client wiring

```bash
droideum agent config claude     # JSON for claude_desktop_config.json / Claude Code
droideum agent config codex      # TOML for ~/.codex/config.toml
droideum agent config            # generic: command + args for any MCP client
```

Claude Code, one line:

```bash
claude mcp add droideum-android -- droideum agent serve --autostart
```

Anything that speaks MCP stdio (Claude Desktop, Codex, Cursor, OpenClaw-style local agents, your own
script) works unchanged. No network ports, no daemon, no auth: **stdio only**, which is exactly what
you want on a phone.

---

## 2. The loop the agent should run

```
device.status → app.launch → wait.idle → observe → act → wait.idle → observe …
```

| Step | Tool | Why |
|---|---|---|
| Health | `device.status` | VM running? adb? booted? ABI list? foreground? free space? |
| Start work | `app.launch` | LAUNCHER intent or explicit activity |
| Settle | `wait.idle` | blocks until the UI tree stops changing — cheaper and more reliable than `sleep` |
| Perceive | `observe` | foreground + screen size + compact UI tree in **one** round trip |
| Act | `input.tap` / `input.text` / `input.swipe` / `input.key` | coordinates come from the tree, never guessed |
| Verify | `observe` / `ui.find` | read the result back before claiming success |
| Speed up | `device.tune` | animations off etc. — call once after boot or after rolling back to an untuned checkpoint |
| Recover | `task.rollback` / `device.reset` | undo a failed attempt |

A server-side prompt template (`prompts/get` → `drive_android_app`) encodes this, so MCP clients that
support prompts get the procedure for free.

---

## 3. Why the observations are shaped like this

Agents pay for tokens and lose accuracy on noise. `ui.dump` therefore:

- **drops pure layout frames** (`FrameLayout` wrappers are usually 60–80 % of a dump and never actionable)
- **precomputes tap targets**: every node carries `xy` = the centre of its bounds, plus `bounds`
- **flags affordances**: `tap`, `scroll`, `focused`, `checked`, `disabled`
- **truncates text** to 80 chars and caps nodes (default 120, hard cap 400)
- **short keys only** (`text`, `desc`, `id`, `cls`) — no attributes an agent can't use
- returns `foreground` inline, so one call answers "where am I?"

`ui.find` is the cheaper path: ask for `"Sign in"` and get `center: [540,540]` back, no tree.

When the tree is useless (games, Flutter/Unity SurfaceViews, DRM surfaces) the fallback is explicit:
`screen.capture` returns a real PNG (MCP image content) for a vision model, and coordinates are tapped
directly. `ui.dump` and `ui.find` say so in their `hint` when they fail rather than returning `[]`.

---

## 4. Failure design (what makes this usable unattended)

Every tool error is a JSON object the model can act on:

```json
{"error": "no UI node matches 'Sign in'",
 "code": "NOT_FOUND",
 "hint": "call ui.dump to see what is on screen; the app may still be loading"}
```

- `INSTALL_FAILED_NO_MATCHING_ABIS` → hint says run `droideum armbridge 9_y`.
- tap outside the screen → `OUT_OF_BOUNDS`, "coordinates must be in device pixels; call ui.dump".
- non-ASCII typing → `UNSUPPORTED_CHARS` with the ADBKeyboard workaround (Android's own limitation).
- adb gone → `NO_DEVICE` with the exact `setprop service.adb.tcp.port 5555` fix.
- every adb call has a timeout; input runs under an exclusive lock so parallel tool calls can't
  interleave half-gestures; adb is reconnected automatically; an internal exception is caught and
  reported as `INTERNAL` instead of killing the server.

---

## 5. Checkpoints: the killer feature for agents

Agents retry, explore and break things. Cheaply undo it:

| Want | Tool | Cost |
|---|---|---|
| Undo app state / a bad install / a crash | `task.checkpoint` → `task.rollback` | RAM-sized write into the qcow2, seconds |
| Cheap in-place reset between attempts | `device.reset` (`pm clear` + force-stop) | ~1–2 s |
| Reinstall an APK cleanly | `app.clear_data` | ~1 s |

`task.checkpoint` uses QEMU's internal `savevm` over the monitor socket (`run/monitor.sock`), so it
captures **RAM + disk**, not just files: the agent can resume a half-scrolled app exactly where it was,
or nuke the whole session back to a golden image. `task.rollback` also re-establishes adb for you.

Recommended pattern for a long task:

```
device.status
task.checkpoint {name: "before-task"}
… attempt the task, verify with observe …
on failure → task.rollback {name: "before-task"} → retry (max N)
```

---

## 5b. VM-level speed levers (do these before blaming the model)

Agent loops are dominated by *settle time*, not by the model. Three settings, in order of payoff:

1. **`device.tune` first.** Animation scales go to 0. A tap that used to keep `wait.idle` spinning for
   an extra 300–900 ms now settles almost immediately, and `observe` after it is already accurate.
   `device.status` reports `tuned: true/false`, so an agent can check rather than hope.
2. **Warm state instead of boot.** `droideum suspend` freezes RAM + disk into the qcow2 and
   `droideum resume` returns in seconds — across *process restarts*, not just across tool calls. An
   agent supervisor that may be killed by Android's OOM killer should prefer
   `task.checkpoint` → `droideum suspend` → later `droideum resume` over a cold boot.
   MCP clients can start the server with `--fresh <checkpoint>` to always begin from a known state.
3. **Smaller screen.** `droideum res 1024x600 160` cuts screenshot size and blit cost; coordinates
   stay in the same pixel space, so `ui.dump`/`ui.find` keep working unchanged.

`device.status` surfaces the observables (`tuned`, `animations`, `saved_states`,
`warm_resume_available`, plus `screen` and `data_free_mb`), and `droideum bench` prints
dump/screenshot/tap-settle latency so a failing loop can be diagnosed as *slow device* rather than
*wrong plan*.

## 6. Token and latency budget

Measured on the stub harness (and typical for a real device, TCG timings):

| Call | Typical latency on a phone | Output size |
|---|---|---|
| `device.status` | 0.3–1 s | ~15 fields, < 400 tokens |
| `observe` (UI only) | 1.0–2.5 s tuned, 1.5–3 s untuned | 1–3 k tokens depending on screen |
| `ui.find` | 1.5–3 s | < 100 tokens |
| `input.tap` | 0.2–0.6 s | 1 field |
| `wait.idle` | until quiet: 0.3–1 s tuned, 1–4 s untuned | 2 fields |
| `screen.capture` | 1–2 s | PNG image (vision model: ~1 image token block) |

Rules of thumb that keep an agent loop cheap:

1. `ui.find` before `ui.dump` — text search is ~10× smaller than a tree.
2. `observe` with `only_interactive: true` for menus and forms.
3. `wait.idle` instead of repeated polling.
4. Never `screen.capture` unless the UI tree is empty — one PNG costs more than 10 UI dumps.
5. `task.checkpoint` before destructive steps is cheaper than redoing a corrupt state.

---

## 7. Self-checks you can run on the phone

```bash
droideum agent selftest        # parsers, schemas, escaping — no device needed
python3 test/mcp-test.py       # 66 assertions: MCP contract + a full stubbed agent loop
STUB_ADB=1 python3 test/mcp-test.py   # + fake adb: observe→find→tap→wait→launch
```

The stub test proves the loop end to end offline: a fake `adb` returns a canned accessibility dump, and
the test asserts the server produced a 4-node tree, computed `Continue` at `[540,540]`, flagged it
tappable, sent `input tap 540 540`, rejected an out-of-bounds tap *before* touching adb, and mapped
`INSTALL_FAILED_NO_MATCHING_ABIS` to the armbridge hint.

---

## 8. Known limits (agent-relevant)

- **TCG is slow.** A tap→settle cycle is seconds, not milliseconds. Budget loop time accordingly;
  `wait.idle` matters more than usual, and `device.tune` removes the animation share of it.
- **Games/SurfaceViews have no UI tree.** Use `screen.capture`; there is no accessibility fallback.
- **No Play Services.** Apps requiring certified Google login / SafetyNet will refuse, by design.
- **`input.text` is ASCII-only** (Android's `input` limitation), surfaced as a clean error.
- **One device per server instance.** For parallel agents, run several instances with distinct
  `DROIDEUM_HOME` + `ADB_PORT` (the VM disk is the expensive part).
- **`task.rollback` reloads RAM**, so adb reconnects and any in-flight call fails — expect one retry.
