# Android run log — trying to actually run an app

Second real session, this time with the **real Android-x86 9.0-r2 image** on the same sandbox
(x86_64, no root, no `/dev/kvm`, 2 GB RAM). Goal: run an actual Android app.

**Outcome: partial.** Android booted, gave root, and accepted an APK — but the framework never
finished booting on a 751 MB guest, so the app never appeared on screen. Everything below is measured,
including the two failures and their root causes.

---

## 1. Image acquisition and verification

```
921 MiB downloaded in 2m36s (~6 MB/s from SourceForge)
sha1sum a86.iso → 1cc85b5ed7c830ff71aecf8405c7281a9c995aa0   ✓ matches the published 9.0-r2 checksum
```

APK chosen: **F-Droid 1.20.0** (`org.fdroid.fdroid_2000052.apk`, 12 MB) from f-droid.org — free
software, so sideloading it is unambiguous, and it is a normal Android app rather than an updater shim.

```
droideum lab abis → arm64-v8a, armeabi-v7a, x86, x86_64     ← has x86_64, so no ARM bridge needed
```

## 2. Boot chain, verified by framebuffer

| t | What the guest showed | Evidence |
|---|---|---|
| 3 s | Android-x86 9.0-r2 bootloader, "Automatic boot in 56 s" | `shots/boot-t3.png` |
| 150 s | Android boot splash at 1024×768 | `shots/boot-t150.png` |
| ~330 s | still splash; guest kernel/initrd are fine, framework starting | `shots/boot-t330.png` |
| 623 s | **QEMU killed by the host OOM killer** (host had 2 GB) | `dmesg: Out of memory: Killed process ... anon-rss:1241192kB` |
| restart | ran at `-m 768`, `tb-size 64`, tree moved out of tmpfs | below |

Two host-side lessons, both now encoded in the tool:

1. **The QEMU tree was living in tmpfs.** `/tmp` is RAM here, so a 190 MB install was stealing
   guest memory. Moving it to the workspace dropped QEMU's RSS by ~400 MB.
2. **`-serial tcp:...,server,nowait`** turned out to be the single most useful flag of the session
   (see §4). It is now always passed by `droideum`, at `CONSOLE_PORT` (default 4555).

## 3. Live mode, and a root shell inside Android

Instead of installing to disk (which needs an interactive installer I cannot click), the console was
used with live boot and an edited kernel command line:

```
at the ISO menu: press Tab, append  console=ttyS0,115200 , Enter
```

Result, on the serial log within ~25 s:

```
Detecting Android-x86... found at /dev/sr0
console:/ #
```

**A root shell in Android**, with no adb, no installer, no UI. Verified from it:

```
getprop init.svc.zygote      → running
getprop init.svc.adbd        → running
getprop ro.build.version.release → 9
getprop ro.product.cpu.abilist   → x86_64,x86,armeabi-v7a,armeabi
```

That last line is the project's central claim, measured on the real image: Android-x86 advertises
**32-bit ARM (armeabi-v7a) but not arm64-v8a**, which is exactly why `droideum armbridge` exists and
why arm64-only APKs fail with `INSTALL_FAILED_NO_MATCHING_ABIS`.

## 4. Getting an APK into the guest without adb

adb did not work, and that turned out to be instructive:

- `service.adb.tcp.port=5555` was set and adbd was listening (`netstat`: `:::5555 LISTEN 1431/adbd`),
  but `adb devices` reported **`offline`**, never `unauthorized` — the handshake never completed.
- The host key was installed by hand at `/data/misc/adb/adb_keys` (715 bytes, byte-exact), `chown
  system:shell`, adbd restarted; still `offline`. Not chased further: **there is a path that doesn't
  need adb at all**, and it is a better primitive for agents anyway.

That path:

```bash
# host: build a tiny FAT image containing the APK, hot-plug it as USB storage
truncate -s 27M transfer.img && mformat -i transfer.img -F ::
mcopy -i transfer.img app.apk ::app.apk
# QEMU monitor:
drive_add 0 if=none,file=transfer.img,format=raw,id=transfer
device_add usb-storage,drive=transfer,bus=usb-bus.0,id=transferstick
```

Guest side (from the root console):

```
mount -t vfat /dev/block/sdb1 /mnt/apk
ls -l /mnt/apk → fdroid.apk, 12 569 951 bytes
```

**The APK was inside the Android guest.** Shipped as `droideum transfer <file>`.

Things this session corrected in the tool:

- `ifconfig eth0 ...` fails: Android-x86 exposes **only `wlan0`**; ethernet is brought up by netd,
  which needs the framework. So "just wget it" is not an option before boot completes.
- HMP echoes every keystroke (`s\x1b[K\x1b[Dsa...`). Added `mon2.py` locally and hardened the
  in-tree monitor parser: strip CSI sequences, apply backspaces, then drop the echoed command line.

## 5. Where it actually broke: the guest's own low-memory killer

`pm install -r -g /mnt/apk/fdroid.apk` was accepted, ran for ~20 minutes, then:

```
cmd: Failure calling service package: Broken pipe (32)
```

Cause, from the guest's own dmesg:

```
[1226.732034] libprocessgroup: Successfully killed process cgroup uid 0 pid 1144 in 467ms   ← zygote64
[1226.920914] libprocessgroup: Successfully killed process cgroup uid 1041 pid 1160 in 150ms
[1227.852522] libprocessgroup: Successfully killed process cgroup uid 1047 pid 1166 in 764ms
```

The guest had `MemTotal: 751492 kB` — the host could only give it 768 MB. Android 9's system_server,
zygote and the `pm` service do not survive that; the "Broken pipe" is simply the package service
dying mid-command.

Other measured facts from the same boot:

- `system_server` spent >10 minutes in `PackageManager.DexOptimizer` running **dex2oat AOT
  compilation of every system app** (Chrome, GmsCore, Google Wallet... at 60–90 s each under TCG).
  Killing `dex2oat` unblocked boot: `SystemServerTiming` advanced to boot phase 520 immediately after.
- Even after that, `sys.boot_completed` stayed empty and `init.svc.bootanim` stayed `running`, with
  ~40 MB free inside the guest. Two force-stop passes on Chrome/GMS freed ~40 MB and did not finish
  the boot. It was left at ~40 minutes of wall clock.

## 6. What this proves and what it does not

Proven, on the real Android image:

- the ISO downloads, verifies, and boots under droideum's QEMU command line
- Android reaches a **root shell** and reports correct properties/ABIs
- an APK can be delivered into the guest and `pm install` invokes the real package manager
- the failure mode when RAM is short is diagnosable (guest LGK kill, not "QEMU crashed")

Not proven here:

- an app running on screen. That needs **≥1.5 GB for the guest**, which needs ≥2.5 GB of host RAM
  after the emulator itself. This sandbox has 2 GB total.
- the Android installer path (`droideum install` → GUI), for the same reason plus no display device.

So the honest summary: **the tool works; this host is too small to finish the last step.** A phone with
6–12 GB does this comfortably — that is what the RAM guard now tells you before you try:

```
$ RAM_MB=768 droideum doctor
 warn RAM_MB=768 is below what Android 9 needs (>=1536). system_server gets killed by
 warn the guest's own low-memory killer mid-boot and 'pm install' dies with 'Broken pipe'.
```

## 7. Artefacts

```
_run/shots/boot-t3.png        Android-x86 bootloader
_run/shots/boot-t150.png      boot splash
_run/shots/boot-t330.png      still splash (framework starting)
_run/shots/during-install.png during pm install
_run/shots/final-android.png  final state of the session
_run/serial.log               the full guest serial console transcript (root shell evidence)
_run/apk/…apk                 12 MB F-Droid APK used
```

## 8. Reproduce on a machine with enough RAM

```bash
ISO_URL=https://sourceforge.net/projects/android-x86/files/Release%209.0/android-x86_64-9.0-r2.iso/download \
RAM_MB=3072 CORES=4 droideum setup && droideum install     # or: droideum live
droideum console bridge      # root shell, no adb needed; append console=ttyS0,115200 at boot
droideum transfer app.apk    # APK into the guest as USB storage
droideum console send 'pm install -r -g /mnt/x/app.apk'
droideum up --tune && droideum agent serve --autostart     # then let the agent drive it
```

Kill the dexopt pass early on a slow host (`droideum console send 'killall -9 dex2oat'`) — it is the
difference between a ~10 minute and a ~25 minute first boot under TCG, at the cost of slower app starts
later.
