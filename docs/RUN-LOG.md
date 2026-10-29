# Real run log — what happened when the VM was actually booted

This is not a stub test. Everything below was executed for real, in a sandbox that resembles a
phone much more than it resembles a server: **x86_64, no root, no `/dev/kvm`, 2 GB RAM, `/tmp`
on a 1 GB tmpfs**. The point was to stop trusting mocks and find out what breaks.

Artefacts from the session live in `../../_run/` (QEMU install, disk image with a live machine
state, framebuffer screenshots).

---

## 1. Getting an engine without root

`apt-get install qemu` is impossible unprivileged, so I resolved the dependency graph out of the
Debian `Packages` index myself and extracted 90 `.deb` files into a private tree:

```bash
# what _run/resolve.py automates
curl -s https://deb.debian.org/debian/dists/trixie/main/binary-amd64/Packages.xz | xz -dc > Packages
python3 resolve.py qemu-system-x86   # -> 90 packages, full transitive Depends/Pre-Depends
dpkg-deb -x each.deb root/           # no root needed to unpack
export LD_LIBRARY_PATH=$PWD/root/usr/lib/x86_64-linux-gnu:...
```

Result: **QEMU emulator version 10.0.13 (Debian 1:10.0.13+ds-0+deb13u1)**.

Worth noting: `qemu-system-x86_64 -accel help` on this binary lists **`kvm`** as well as `tcg`. The
same engine that crawls here runs at near-native speed on any KVM-capable host — which is exactly
what `USE_KVM=auto` detects.

`qemu-img` needed a second resolution pass (`qemu-utils`); `droideum setup` caught the missing tool
immediately rather than failing halfway through the download.

## 2. A real `setup`, with a real image

```bash
ISO_URL=https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/alpine-virt-3.24.2-x86_64.iso \
ISO_SHA1=skip RAM_MB=512 DISK_SIZE=2G droideum setup
```

```
1/3  Checking host engine      ok qemu-system-x86_64 10.0.13
2/3  Android system image      66M downloaded, checksum skipped (custom image)
3/3  Virtual disk              created 2G qcow2
```

**Bug found:** the ISO cache path was hardcoded to `android-x86_64-9.0-r2.iso`, so any custom image
would have been saved under the wrong name or silently reused. Fixed: the cache filename is now
derived from `ISO_URL`.

**Second bug found (introduced by that fix):** the derivation ran at line 26, before `ISO_URL`'s
default was applied, so `set -u` aborted the script for anyone who hadn't exported `ISO_URL`.
Fixed by moving it after the defaults — a good example of why the stub suite alone isn't enough.

## 3. Install boot → screenshot of the guest

`droideum install` attaches the disk and boots the ISO. With no display backend available I pulled
the framebuffer out of QEMU's own monitor (`screendump`) and converted PPM→PNG with a stdlib script:

```
OpenRC 0.63.2 is starting up Linux 6.18.52-0-virt (x86_64)
...
Welcome to Alpine Linux 3.24
localhost login:
```

The whole install-boot path works: `-drive $(_drive)` + `-cdrom` + `-boot d`, monitor socket at
`run/monitor.sock`, and a guest at a login prompt in ~30 s.

## 4. The bug that only a real run could find

Booting `droideum start` against the (still empty) disk produced:

```
Booting from DVD/CD...
Boot failed: Could not read from CDROM (code 0003)
Booting from ROM...
iPXE 1.21.1 -- Open Source Network Boot Firmware
Nothing to boot: No such file or directory
No bootable device.
```

Not a bug in the boot path — the disk genuinely had no OS on it, so the BIOS fell through to PXE. But
from a user's point of view that looks exactly like a broken emulator, and for an agent it means
waiting 25 minutes for `sys.boot_completed` that will never arrive.

**Fix:** `disk_has_boot_sector()` reads the MBR signature (`0x55AA` at offset 510) through
`qemu-img dd` and refuses to boot an uninstalled disk:

```
warn the virtual disk has no boot loader — Android is not installed on it yet
  run: droideum install   (boots the ISO; choose "Install Android-x86 to harddisk")
  booting anyway would drop into a network boot (PXE) and look like a crash.
```

`droideum up` (the agent path) fails instantly with "not a boot timeout" instead of hanging.

## 5. Suspend/resume, proven with state

This is the feature I had claimed but never executed. Typing into the guest over HMP `sendkey`
(no guest agent needed):

```
localhost:~# echo SUSPEND-TEST-WORKED > /tmp/marker.txt
localhost:~# cat /tmp/marker.txt
SUSPEND-TEST-WORKED
```

Then:

```
$ droideum suspend
  ok suspended into 'autosave' in 1s
$ qemu-img snapshot -l android.img
ID  TAG       VM_SIZE   DATE                 VM_CLOCK
1   autosave  142 MiB   2026-10-28 04:28:06  0000:00:58.995
$ droideum resume
  ok state 'autosave' restored in 22s — the guest is running exactly where it was
```

And the screenshot after resuming shows the **same shell session**: the `SUSPEND-TEST-WORKED`
command, its output, and then a *new* command typed after the wake-up. `/tmp` (tmpfs, RAM-only)
survived, which proves RAM was restored rather than the disk being rebooted.

Three suspend/resume cycles later, all four marker lines were on screen at once:

```
localhost:~# echo SUSPEND-TEST-WORKED > /tmp/marker.txt
localhost:~# cat /tmp/marker.txt
SUSPEND-TEST-WORKED
localhost:~# echo RESUMED-AND-STATE-INTACT
RESUMED-AND-STATE-INTACT
localhost:~# echo MARKER-ALPHA
MARKER-ALPHA
localhost:~# echo MARKER-BETA
MARKER-BETA
```

**Measured cost:** suspend 0.8–1.0 s for a 512 MB guest; resume ~2 s to `LOADVM`, ~22 s until the
screen repaints (TCG redraws the whole framebuffer).

### Three bugs this exposed

1. **`savevm` cannot overwrite a tag.** QEMU refuses, *and leaves the old state in place*, so my
   "replacing existing state" path silently kept stale RAM — an agent would roll back to the wrong
   past. Fixed: `delvm` first, verify the tag is really gone, then `savevm`; same fix in
   `task.checkpoint` in `droideum-agentd`.
2. **HMP echoes keystrokes.** Monitor replies arrived as `s\x1b[K\x1b[Dsa\x1b[K...`, so any parser
   looking for `error` could match noise. Fixed in both readers: strip CSI sequences and apply
   backspaces to reconstruct what a human would see, then drop the echoed command line.
3. **Honest waits.** `resume` used to block up to 120 s waiting for adb — pointless for a guest with
   no adbd (`RESUME_ADB_TIMEOUT`, default 20 s, then it reports success anyway). `stop` waited the
   full 90 s for ACPI to power off a guest that has no ACPI handler; it now uses
   `STOP_QUICK_TIMEOUT` (12 s) when adb was never up, and says why.

## 6. What this run does **not** prove

- **Not Android.** The image was Alpine (66 MB) for speed; Android-x86 is 965 MB and its boot is
  5–20 min under TCG, and no output device existed here to run the installer GUI. Every Android
  *code path* was exercised (`install`, `start`, `up`, `tune`, `apk`, `armbridge`), but the guest
  was Linux. The Android-specific pieces (installer menus, `enable_nativebridge`, `uiautomator`)
  remain device-verified-only.
- **Not on a phone.** No proot-distro, no Termux, no aarch64. Memory pressure here (577 MB
  available) is harsher than a modern phone, but the proot layer itself was not in the test.
- **No KVM, ever.** TCG numbers here are the worst case by construction.

## 7. Reproduce it

```bash
cd _run
. ./env.sh                                  # PATH/LD_LIBRARY_PATH to the private QEMU tree
bin/droideum setup                          # or point ISO_URL at Android-x86 for the real thing
bin/droideum install                        # ISO + blank disk
bash mon.py home/run/monitor.sock "screendump /tmp/s.ppm"   # see the guest
python3 ppm2png.py /tmp/s.ppm /tmp/s.png
bash type.py "root"$'\n' "uname -a"$'\n'    # keyboard into the guest, no guest agent
bin/droideum suspend && bin/droideum resume
```

Helpers used for the session: `_run/mon.py` (one HMP command), `_run/type.py` (sendkey typing),
`_run/ppm2png.py` (stdlib framebuffer→PNG), `_run/resolve.py` (unprivileged package resolver).

## 8. Suite status after these fixes

```
test/smoke-test.sh              109 passed, 0 failed     (+  short ACPI grace without adb   (+1)
                                                          +  resume without adb still wakes  (+1)
                                                          +  uninstalled disk is refused    (+3)
                                                          +  stale checkpoint is replaced   (+3))
test/mcp-test.py (offline)       30 passed, 0 failed
test/mcp-test.py (STUB_ADB=1)    75 passed, 0 failed
droideum-agentd selftest          0 failures
```

Five of the new assertions exist *only* because the real run found those bugs.
