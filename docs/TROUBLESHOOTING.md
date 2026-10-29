# Troubleshooting

Run `droideum doctor` first — it checks the engine, the image, the disk and free space in one shot.

---

## Installer (`install.sh`)

**`This must run inside Termux (PREFIX is not set)`**
You ran it in a proot container, an SSH session into a PC, or on a Linux box. Run it in the Termux
app itself. (Termux from the Play Store is deprecated and may misbehave — use F-Droid.)

**`could not install proot-distro`**
No network or a stale mirror: `pkg update && pkg upgrade -y`, then re-run. In India/China some
mirrors are slow; `termux-change-repo` switches them.

**`proot-distro install debian failed`**
Usually a partial download. `proot-distro remove debian && bash install.sh` again. Check free space:
`df -h $PREFIX` needs ~1 GB.

**`droideum: command not found` after install**
`$PREFIX/bin` isn't on PATH in your shell — restart Termux, or
`export PATH=$PREFIX/bin:$PATH`.

---

## Setup / image download

**`droideum setup` dies during the 965 MB download**
Termux was backgrounded and Android froze the process. Fix all three:
```bash
termux-wake-lock
# Android Settings -> Apps -> Termux -> Battery -> Unrestricted
droideum setup      # re-run; it starts the download fresh, deletes the .part
```

**`checksum mismatch`**
Truncated download or a mirror serving an error page. Delete
`~/.droideum/cache/android-x86_64-9.0-r2.iso.part` and retry, or verify manually:
```bash
# on a PC
sha1sum android-x86_64-9.0-r2.iso   # expect 1cc85b5ed7c830ff71aecf8405c7281a9c995aa0
```
Using a custom image (BlissOS, your own build)? Set `ISO_URL` **and** `ISO_SHA1=skip`.

**`qemu-img: command not found` / partial apt**
```bash
proot-distro login debian -- apt-get install -y qemu-system-x86 qemu-utils adb curl
```

---

## Booting

**VM "runs" but the VNC viewer shows a black screen**
1. `droideum log` — if you see TCG startup lines, it's just slow: first boot of the ISO is 5–20 min.
2. Port mismatch: viewer must hit `5900 + VNC_DISPLAY` (default 5900).
3. Viewer connecting from *another* device won't work — the VNC server binds `127.0.0.1` on purpose.
   Keep `VNC_ADDR=127.0.0.1` and view on the phone.

**`QEMU exited immediately`**
`droideum log` shows why. Frequent causes: bad `EXTRA_QEMU_ARGS`, a config with `-accel kvm`
(no KVM on phones), or a corrupt `android.img` (`droideum restore <snapshot>`).

**Installer can't see the disk**
It's an IDE drive by design (`if=ide`) because Android-x86's initrd looks for `/dev/sda`-style
devices. Don't switch it to virtio in `EXTRA_QEMU_ARGS` unless you know that image supports it.

**Installed, rebooted, black screen**
Boot parameters: the installed GRUB entry needs `nomodeset` (Android-x86 on QEMU has no KMS driver
for the default VGA). At the GRUB menu press `e`, append `nomodeset xforcevesa`, boot, then make it
permanent in `/grub/android.cfg` inside the VM.

---

## adb

**`No adb connection`**
Inside Android, switch to the console with **Alt+F1**, log in as root, then:
```
setprop service.adb.tcp.port 5555
stop adbd
start adbd
```
Then `droideum adbconnect`. Also confirm hostfwd is live: `droideum status` shows the port.

**`adb: device offline`**
Stale key. `rm -rf ~/.android/adbkey*` inside the container, then `droideum adbconnect`.

**`adb root refused`**
Normal on non-`userdebug` builds; only `armbridge` really wants it and it degrades gracefully
(Settings → Apps compatibility → Enable native bridge does the same job from the UI).

---

## Installing apps

**`INSTALL_FAILED_NO_MATCHING_ABIS`**
The APK ships natives without an x86_64 build. Check with `droideum lab abis app.apk`, then:
```bash
droideum armbridge 9_y
droideum apk install app.apk
```
Still failing? The app is arm64-v8a-only. Try `armbridge 9_z` (partial), or accept that it needs a
real ARM device / a KVM-capable host. Some games ship an x86 split inside an `.xapk` — unpack it and
install the `x86_64` APK plus its split config.

**`INSTALL_FAILED_INSUFFICIENT_STORAGE`**
```bash
droideum stop
droideum resize 8G
droideum start
```
Then in Android: `droideum shell sm resize` (or reboot) so the partition expands.

**`INSTALL_FAILED_VERSION_DOWNGRADE` / `UPDATE_INCOMPATIBLE`**
`droideum apk uninstall <pkg>`, then install.

**`.xapk` / `.apks` / `.apkm` bundles**
Plain `adb install` can't take them. Unzip and use `adb install-multiple base.apk split_*.apk`
through `droideum shell`... or simply: `unzip bundle.xapk -d b && droideum apk install b/*.apk`
for single-APK bundles.

**App installs but crashes on launch**
`droideum apk logcat | grep -i -E 'fatal|abi|houdini'`. `UnsatisfiedLinkError` → missing native ABI.
`ClassNotFoundException` on Google APIs → needs Play Services, which this image does not include.

---

## Speed and state

**Every boot takes 5–20 minutes and "scanning for errors on /dev/sda" appears first**
The image is dirty from an unclean kill. From now on use `droideum stop` (ACPI → quit → SIGTERM →
SIGKILL, in that order) or `droideum suspend`. To repair once:
```bash
droideum stop
qemu-img check -r all ~/droideum/android.img     # or restore a snapshot
droideum start
```

**`droideum suspend` fails / "state was not written"**
`savevm` writes roughly RAM-sized data into the qcow2, so it needs free space:
```bash
droideum stop            # (or let suspend finish first)
droideum resize 4G       # then suspend again
```
Also stop the VM's own writes first — suspend already runs `adb shell sync`.

**`droideum resume` says "no saved state named autosave"**
Either nothing was suspended yet, or the state was written under a different name:
```bash
droideum start --list-warm     # internal qcow2 snapshots (savevm states)
droideum resume <name>
```
`droideum snapshot`/`restore` are a *different* mechanism (disk-only, copy-on-write files) and do not
capture RAM.

**Resume works but adb never reconnects**
The guest's adbd survives, but the host forward takes a moment. `droideum resume` waits up to
`RESUME_TIMEOUT` (120 s) and then leaves the VM running; retry with `droideum adbconnect`. If it still
fails, the saved state was taken while adb was down — resume, then inside Android run
`setprop service.adb.tcp.port 5555; stop adbd; start adbd`.

**Agent loop is slow even though the VM is idling**
Run `droideum bench`, then:
1. `droideum tune` (animations) — usually worth 20-40 % of iteration time
2. `droideum res 1024x600 160` — blit cost scales with pixels
3. raise `CORES` (default is half your cores) and, on 8 GB phones, `TB_SIZE=512`
4. check `droideum status` for `KVM` — if it says TCG, that is the floor

**`droideum doctor` says "qcow2 image reports errors"**
```bash
qemu-img check -r leaks ~/droideum/android.img    # reclaim leaked clusters (safe)
qemu-img check -r all  ...                        # repairs; take a copy first
```
Leaks accumulate when the VM is SIGKILLed repeatedly.

**Doctor says KVM is available**
You are on a rooted device or a Linux host: `USE_KVM=auto` already picked it up and the VM is running
at near-native speed. Do not add `-accel kvm` manually.

## Everyday

**Battery drain / heat**
Expected: full-system emulation is the most expensive thing a phone can do. `droideum stop` when
done; it does not idle cheaply.

**Termux gets killed while the VM runs**
Wake lock + Unrestricted battery, and keep the notification alive. If Android still kills it, run the
VM with `DISPLAY_BACKEND=none` at low resolution for unattended jobs and drive it purely through adb.

**Disk filling up**
`droideum stop && qemu-img info ~/droideum/android.img` shows real usage. Reclaim by snapshotting,
restoring a small snapshot, or recreating the disk and reinstalling.

**Two VMs at once**
Not supported by design — one PID file, one adb port. Run a second copy with
`DROIDEUM_HOME=~/.droideum2 ADB_PORT=5556 droideum ...` if you really need it.

## `pm install` fails with `Failure calling service package: Broken pipe (32)`

The guest ran out of memory and Android's own low-memory killer (not QEMU) killed `system_server`
or `zygote64` while the command was in flight. Check from the guest console:

```
dmesg | grep -i "killed process cgroup"     # uid 0 pid ... ← that is zygote/system_server
head -3 /proc/meminfo                       # MemTotal is what the guest actually got
```

Android 9 needs **≥1536 MB of guest RAM**; give the host correspondingly more and retry:
`RAM_MB=3072 CORES=4 droideum stop && droideum up`. `droideum doctor` flags this before you start.

## First boot takes forever / HMP monitor output is full of `\x1b[K`

The guest is AOT-compiling every system app (`dex2oat`, 60–90 s each under TCG). Two options: wait, or

```
droideum console send 'killall -9 dex2oat'      # unblocks boot; apps start slower afterwards
```

The escape-sequence noise is the monitor echoing keystrokes; the in-tree parser strips CSI sequences
and applies backspaces, so command output stays readable.

## I cannot reach a host HTTP server from the guest

Android-x86 exposes **only `wlan0`** until the framework brings netd up, so `10.0.2.2` and `busybox
wget` fail with *Network is unreachable*. Use `droideum transfer file` instead — it needs no network.

## `adb devices` says `offline` (never `unauthorized`)

adbd is up and listening but the handshake does not complete. Do not loop on `adb kill-server`; use the
serial console (`droideum console bridge`), which needs no authentication.
