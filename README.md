# droideum

**An Android device for AI agents — running on a stock, non-rooted phone.**
Termux → proot-distro aarch64 Debian → QEMU full-system Android VM → MCP tool server.

Give your agent a phone. It observes a compact, coordinate-annotated UI tree, taps and types like a
user, installs APKs, reads logcat, and can checkpoint/rollback the whole device state.

```
your agent ──MCP stdio──▶ droideum-agentd ──adb──▶ Android 9 VM (QEMU/TGC)
                          26 tools                    inside proot aarch64
```

---

## 60-second version

```bash
pkg install -y proot-distro git && cd droideum-termux && bash install.sh    # Termux
droideum setup && droideum install        # once: image + disk + Android into the disk (needs VNC)
droideum up                               # headless boot, blocks until adb answers
droideum agent serve --autostart          # MCP server on stdio
```

```bash
claude mcp add droideum-android -- droideum agent serve --autostart
# or: droideum agent config codex >> ~/.codex/config.toml
```

Agent-side loop: `observe` → `ui.find` → `input.tap` → `wait.idle` → `observe`. Full contract in
[`docs/AGENT.md`](docs/AGENT.md).

---

## The one architectural decision you must know about

> **Android apps cannot run inside proot.** An APK needs `zygote`, the `binder` *kernel driver*,
> `surfaceflinger` and the framework. proot only gives you `ptrace` + chroot fakery on the host
> kernel, and a locked phone exposes no binder node. Every "APK emulator in proot" is a VM wrapper.
>
> So: proot aarch64 provides the **engine room** (qemu, adb, python3, apktool — a real Debian
> userland), and the **Android runtime lives in a QEMU full-system VM** inside it. That combination
> is the only one that works on a non-rooted device, and it is exactly what an agent needs: a
> disposable, checkpointable, scriptable Android device.

Why this is a *good* fit for agents regardless of speed: an agent doesn't need 60 fps, it needs
**deterministic observation and control** — which a slow-but-fully-programmable device delivers, and
which "just use the host phone" cannot (no UI automation API, no installs, no rollback, no isolation).

| | Reality on a phone, no root |
|---|---|
| Android | **9.0 (Pie)** — Android-x86 9.0-r2 |
| Emulated CPU | x86_64 on **TCG** (no KVM on locked phones) |
| Cold boot | 2–20 min · warm boot ~2–5 min |
| Tap→settle cycle | ~1–4 s (use `wait.idle`, not `sleep 5`) |
| App speed | ~10–30 % of native: fine for utilities/forms/2D, bad for 3D |
| arm64-only APKs | need `droideum armbridge`, some still fail |
| Play Services / DRM | absent by design — SafetyNet-gated apps refuse |

Route comparison (Waydroid, redroid, Cuttlefish, cloud farms) and why none of the others run here:
[`docs/ROUTES.md`](docs/ROUTES.md).

---

## Install

**Requirements:** Termux from F-Droid, Android 7+, aarch64, ~10 GB free.

```bash
pkg update && pkg install -y proot-distro git
cd /path/to/droideum-termux
bash install.sh            # proot-distro Debian + engine + agentd + `droideum` command
termux-wake-lock           # downloads and boots die without this
droideum setup             # qemu/adb/python3 in container + 965 MB image + 16 GB disk
droideum install           # boots the installer ISO; needs a screen:
                           #   VNC app -> 127.0.0.1:5900 -> "Install Android-x86 to harddisk"
```

Installer choices: `No` to partitioning → `New → Primary → Bootable → Write → Quit` → select
partition → `ext4` → yes to GRUB → yes to `/system` rw. Then `droideum stop && droideum up`.

For APKs that ship only ARM natives:

```bash
droideum armbridge 9_y     # then verify: droideum shell getprop ro.product.cpu.abilist
```

---

## Speed: the three things that actually matter

Boot time and per-step latency are what decide whether this is usable. All three levers are built in.

### 1. Warm state — resume in seconds instead of booting

```bash
droideum suspend         # freeze the whole machine (RAM + disk) into the qcow2
droideum resume          # wake it: no boot, no fsck, app state and scroll position intact
droideum up              # auto-resumes if a saved state exists, else cold-boots
droideum up --cold       # force a fresh boot
```

`savevm`-based, so it restores **memory**, not just files — a half-filled form is still half-filled.
Cost: a few seconds and roughly RAM-sized disk. This is the single biggest win on a phone, because a
cold boot under TCG is 2–20 minutes and includes an ext4 fsck.

### 2. Keep the filesystem clean, so boots stay short

`droideum stop` no longer terminates QEMU directly. It now:

1. `adb shell sync` — flush Android's page cache
2. sends an **ACPI power button** event (`system_powerdown`) and waits for Android to power itself down
3. falls back to the monitor's `quit`, then SIGTERM, then SIGKILL (in that order, each one logged)

A SIGKILLed image is *dirty*, and the next boot pays for it with a multi-minute fsck. You can see the
difference in `droideum log`.

### 3. Turn Android's animations off (agent loops get ~30 % faster)

```bash
droideum up --tune        # boot + tune in one step
droideum tune             # re-apply later (idempotent)
```

Sets `window/transition/animator` scales to 0, screen always on, lock screen disabled, 4 MB logcat
buffer. Every `wait.idle` and every `observe` after a tap stops waiting for a 300 ms animation that
carries no information. `droideum tune --keyboard ADBKeyboard.apk` additionally installs the
broadcast IME, which lifts the ASCII-only limit on typed text.

### Measure it, don't guess

```bash
droideum bench
  ui dump (uiautomator)               820 ms
  screenshot (screencap -p)           540 ms  (61204 bytes PNG)
  tap -> UI settled                  1420 ms
  projected agent iteration          2240 ms
```

Saved as JSON in `~/.droideum/logs/bench-*.json` so you can compare configurations (screen size,
cores, `TB_SIZE`, tuned vs untuned) instead of arguing about them.

### Other levers, in order of payoff

| Lever | Effect |
|---|---|
| Lower resolution (`droideum res 1024x600 160`) | biggest win: software VGA blit dominates |
| `droideum tune` | removes animation dead time from every step |
| More `CORES` (default `nproc/2`) | TCG translates per-vCPU-thread |
| `TB_SIZE=512` on 8 GB phones | fewer translation-cache flushes on long sessions |
| `droideum profile use android9-lite` | 1536 MB RAM + virtio-vga for 4 GB phones |
| Warm resume instead of reboot | minutes → seconds |

QEMU is now also given `cache=writeback,discard=unmap,detect-zeroes=unmap`: the first hides eMMC
latency, the other two stop the qcow2 from growing forever as apps are installed and deleted.

---

## Agent tools (26)

Perception first — every observation is small, stable and self-describing.

| Tool | What the agent uses it for |
|---|---|
| `device.status` | VM/adb/boot state, Android version, ABI list, screen, foreground app, free space |
| `observe` | **one round trip**: foreground + compact UI tree (+ optional PNG) |
| `ui.dump` | full compact tree; layout frames dropped, `xy` tap centres precomputed |
| `ui.find` | text/desc/id → tap coordinates (≈10× cheaper than a dump) |
| `screen.capture` | PNG for vision models when the tree is empty (games, SurfaceViews) |
| `input.tap` / `long_press` / `swipe` / `text` / `key` | act on the device, bounds-checked, single-flighted |
| `wait.idle` | block until the UI stops changing — replaces brittle sleeps |
| `app.install` / `launch` / `stop` / `clear_data` / `list` / `current` | app lifecycle |
| `device.tune` | animations off, screen always on, lock screen off, bigger logcat — makes later steps faster |
| `device.reset` | known-good state cheaply: force-stop all, clear one app, relaunch |
| `task.checkpoint` / `rollback` / `snapshots` | save/restore **RAM + disk** (QEMU `savevm`) |
| `logcat.tail`, `shell.exec`, `file.push`/`pull` | triage, escape hatch, artifacts in/out |

Design notes that matter for agent reliability:

- **Errors are actionable.** Every failure returns `{error, code, hint}` — e.g.
  `INSTALL_FAILED_NO_MATCHING_ABIS` → "run `droideum armbridge 9_y`"; out-of-bounds tap → "call ui.dump
  and use a node's `xy`"; non-ASCII typing → Android's own limitation plus a workaround.
- **Token-shaped output.** Text truncated to 80 chars, node cap 120 (hard 400), short keys only,
  foreground inline. A screen dump lands around 1–3 k tokens instead of 20 k of raw XML.
- **Timeouts everywhere.** adb calls bounded, `uiautomator dump` retried, input serialised behind a
  file lock, adb auto-reconnected, internal exceptions reported instead of killing the server.
- **Stdlib-only Python 3.** No pip, no compiled wheels — it survives `proot-distro` rebuilds on a phone.

---

## Human use (still supported)

The same VM is perfectly usable interactively — you just do the install step this way anyway.

```bash
droideum start                 # boot with VNC on 127.0.0.1:5900
droideum res 1280x720 240      # virtual screen size + dpi (biggest speed lever)
droideum apk install app.apk   # sideload
droideum apk run com.example.app
droideum apk logcat | droideum apk screenshot shot.png
droideum stop | status | log
```

### APK lab (offline, no VM)

The proot container is a normal aarch64 Debian, so it doubles as an APK workbench — the fastest way to
know whether an APK will ever run here:

```bash
droideum lab install
droideum lab abis app.apk      # lib/x86_64 -> runs; lib/arm64-v8a only -> needs armbridge; none -> pure Java
droideum lab info app.apk      # package, version, target SDK
droideum lab decode app.apk    # smali + resources
```

---

## Checkpoints, images, disk

```bash
droideum suspend / resume      # warm machine state (RAM+disk) — seconds, not a boot
droideum snapshot golden       # copy-on-write disk-only checkpoint (VM stopped)
droideum agent call task.checkpoint --json '{"name":"before-task"}'   # live checkpoint
droideum restore golden        # roll the disk back
droideum resize 8G             # grow when apps fill the 16 GB image
droideum profile list          # android9 | android9-lite | bliss14 | custom
droideum profile use bliss14   # Android 11/12 image + 3 GB RAM (slower boot, newer APIs)
droideum push app.apk          # -> /sdcard/Download
droideum pull /sdcard/x.png .  # out of the device
```

`droideum status` now also reports acceleration in use (KVM vs TCG + `tb-size`), saved states, and real
disk usage; `droideum doctor` checks qcow2 integrity, leaked clusters and whether Android is tuned.

---

## Configuration

`droideum config` prints the file. Everything is a plain shell variable
([annotated example](config/droideum.conf)): `RAM_MB` (default 40 % of device RAM), `CORES` (default
half), `VNC_DISPLAY`, `ADB_PORT`, `DISPLAY_BACKEND=vnc|gtk|sdl|none`, `ISO_URL`/`ISO_SHA1` (swap in
BlissOS or your own image), `EXTRA_QEMU_ARGS`. Agent-relevant env: `DROIDEUM_HOME`, `ADB_PORT`,
`DROIDEUM_SERIAL`.

---

## It has actually been run

See [`docs/RUN-LOG.md`](docs/RUN-LOG.md) for a real session: QEMU 10.0.13 installed unprivileged from
extracted `.deb`s, a disk image created, a guest booted from that disk, and **three suspend/resume
cycles verified against the guest framebuffer** (RAM-only `/tmp` file still present after waking).
That run found five bugs the stubs could not — an uninstalled disk silently falling through to PXE,
`savevm` leaving stale state behind when it refuses to overwrite, HMP keystroke echo corrupting
monitor replies, and two waits that blocked for minutes on a guest that had no adb.

Measured there: suspend **0.8–1.0 s**, resume to a live shell **~2 s** (screen repaint ~22 s under
TCG), cold guest boot **25–30 s**, 142 MiB state for a 512 MB guest.

## Testing

```bash
./test/smoke-test.sh                # 109 assertions: lifecycle, qemu args, suspend/resume, guards
python3 test/mcp-test.py            # 30 assertions: MCP contract, schemas, prompts, error shape
STUB_ADB=1 python3 test/mcp-test.py # 75 assertions: + full agent loop against a fake device
droideum agent selftest             # on-device: parsers/schemas/escaping, no device needed
python3 examples/agent_loop.py smoke # end-to-end CLI loop (works against the stub device too)
```

Verified offline: QEMU command assembly, PID lifecycle, snapshot/restore, MCP handshake, tool
schemas, UI-tree compaction and coordinate maths, out-of-bounds rejection *before* touching adb,
ASCII guard, `ui.find` fallbacks, `task.*` monitor plumbing, error→hint mapping.
Not verifiable here: a real Android boot (needs the device and a ~1 GB image).

## Layout

```
droideum-termux/
├── install.sh                 Termux installer: proot-distro, engine, agentd, wrapper command
├── bin/droideum               engine + CLI (human and headless paths)
├── bin/droideum-agentd        MCP tool server, stdlib-only Python
├── docs/AGENT.md              agent contract: loop, token budget, failure design
├── docs/RUN-LOG.md            what a real boot/suspend/resume session found
├── docs/ROUTES.md             why a VM inside proot, and the routes that don't work
├── docs/TROUBLESHOOTING.md    error-by-error fixes
├── examples/agent_loop.py     dependency-free agent loop (smoke + LLM mode)
├── examples/mcp-client-config.json
├── config/droideum.conf       annotated configuration
└── test/                      smoke-test.sh, mcp-test.py
```

## Scope and ethics

droideum runs a generic AOSP-derived image: no Google apps, no DRM, no root. It is a *sandboxed,
disposable* device — good for automating your own apps, UI testing, data entry, scraping your own
account, and reproducible demos. Installing apps you are not licensed to use, or using it to bypass
another service's terms or hardware integrity checks, is not a supported use and generally won't work
anyway (SafetyNet/Play Integrity will report an emulated, uncertified device).

## License

MIT — see [LICENSE](LICENSE).
