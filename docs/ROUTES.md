# Why droideum is built this way

Short version: **Android apps cannot run in proot.** This document explains the mechanism, then
ranks every route that exists for running Android apps on a non-rooted ARM phone, so the design
choices in `bin/droideum` are auditable rather than arbitrary.

---

## 1. What an APK actually needs

An APK is a zip containing DEX bytecode, resources and (often) native `.so` libraries. Running it
requires all of the following, provided by the *Android userspace*, not by Linux:

| Requirement | Why proot can't supply it |
|---|---|
| `zygote` / `app_process` → `artd` | ART is built against Android's bionic libc, linker namespace rules and SELinux labels. There is no Debian package for "the Android runtime". |
| `binder` (`/dev/binder`, `/dev/hwbinder`) | Binder is a **kernel driver**, not a library. A non-rooted phone exposes no binder node to you, and proot cannot create device nodes. |
| `surfaceflinger` + `gralloc`/HWC | Rendering path is binder + hardware composer HAL. VNC-ing a Debian container gives you a Debian desktop, not Android surfaces. |
| `installd`, `system_server`, `PackageManager` | Needed even to unpack and register an app; all built on binder + Android's init/rc system. |
| SELinux domain per app | Non-root can't write policy; Android-x86/Bliss ship permissive mode precisely because they are *systems*, not containers. |

proot specifically only does two things: `chroot` (which unprivileged users can't do without
interception) and path/UID translation via `ptrace`. It intercepts syscalls; it does not give you
kernel drivers, namespaces with privileges, or a foreign init. Running an Android framework inside
proot would be "Android-in-a-box with no binder, no zygote spawn, no surfaces" — i.e. nothing boots.

There is one narrow exception worth knowing: **Termux itself runs** on a non-rooted phone because
Android's kernel *is* Linux and Termux's binaries are plain aarch64 ELF. That's exactly the boundary
— plain Linux userland yes, Android framework no.

---

## 2. The four real routes

| Route | Needs | Speed on a locked phone | Verdict |
|---|---|---|---|
| **A. Waydroid / redroid** (LXC container of a real Android system image) | host kernel with `binder`/`ashmem` modules + root (Waydroid) or a Linux host (redroid) | native | **Impossible** on stock Android — the phone's kernel has binder but you can't get a container with the privileges to use it |
| **B. Android Studio emulator / Cuttlefish** | KVM (x86) or a patched kernel; hardware virtualisation | native-ish | **Impossible** — no `/dev/kvm` access without root; Cuttlefish needs a Linux host |
| **C. Native-bridge on an Android-x86 VM** (what droideum does) | a VM to run it in | TCG ~10–30 % | **Works today, unrooted** |
| **D. Cloud device farm** (remote phone, scrcpy in a browser) | internet + account | full speed | Best UX/speed, but needs a server and a subscription; not offline |

Also worth naming: **Genymotion/Anbox-style** approaches are all (A) or (B) in disguise, and
**"APK emulator" apps on the Play Store** are almost always thin front-ends around (A)/(B) that
require root, or they just ship a browser.

### Why route C, and why x86_64 inside an aarch64 phone

QEMU **full-system** emulation does not need any host kernel features, so it is the only engine that
works inside proot on a locked device. Inside that VM we run **x86_64 Android-x86** rather than
arm64 Android for two reasons:

1. **TCG cost.** TCG translates guest→host instructions in software. aarch64 guest on aarch64 host is
   *not* accelerated (no KVM), and QEMU's TCG for arm64 guests is noticeably slower and less mature
   in system mode than x86_64, whose TCG backend is the most developed.
2. **App availability.** x86_64 Android-x86 has a long-tested userspace, a working `enable_nativebridge`
   path (libhoudini), and an installer that is happy on IDE/virtio disks. arm64 Android images are
   built for phones with device trees, vendor blobs and specific bootloaders — not for a generic VM.

The cost is well known: apps whose natives are arm64-only can't install until you enable the
translator, and the translator itself is 32-bit-first (`9_y`), with `9_z` giving partial arm64.

---

## 3. Why proot-distro still earns its place

Even though it can't host Android, the container is doing real work:

- **A real Debian userland** for `adb`, `qemu-system-x86_64`, `qemu-img`, `curl`, `apktool`, `aapt`,
  `unzip` — none of which you want to hand-compile for Termux.
- **An APK workbench** (`droideum lab`) that answers "will this even install?" in one second, offline.
- **Process isolation + FHS paths** so scripts and paths are the same as on a desktop Linux box.
- **Image management**: the whole thing is disposable and re-creatable, while your VM disk lives on
  the Termux side (`~/droideum`, bind-mounted to `/root/.droideum`) and survives rebuilds.

Networking note: proot-distro shares the network namespace with Termux, and Android's loopback is
device-wide, so a VNC client *app* on the phone can reach `127.0.0.1:5900` inside the container with
zero port exposure and zero root.

---

## 4. Performance model (what is actually the bottleneck)

Random-read speed from the virtual disk and the software VGA blit dominate — not raw CPU.

- `cache=writeback` on the qcow2 drive (already used) hides a lot of the eMMC latency.
- **Resolution is the biggest lever.** 1600×900 software-blitted over VNC is ~2.5× the pixel work of
  1024×600. Start low; raise it only if the app is comfortable.
- More **cores** help TCG (each vCPU thread translates independently); beyond `nproc/2` you starve
  Termux and the VNC client.
- RAM above ~2 GB mostly buys you fewer app reloads, not throughput.
- Don't enable KVM options in `EXTRA_QEMU_ARGS` — there is no `/dev/kvm`, so `-accel kvm` fails
  outright. The engine hardcodes `-accel tcg,thread=multi`.

## 5. Upgrade path if you later get root or a Linux box

`bin/droideum` is deliberately a thin QEMU driver, so the same disk file works elsewhere:

- **Linux PC with KVM** — run `qemu-system-x86_64 -enable-kvm` on `android.img` and you get
  near-native Android; copy the qcow2 over and keep using it.
- **Rooted phone** — `redroid` or Waydroid (real binder, native speed), or QEMU with `-enable-kvm`
  if the kernel exposes KVM to userspace, which many ARM kernels do not.
- **Cloud** — an x86 cloud VM with `/dev/kvm` hosting the same image, reached with scrcpy in a
  browser, is the fastest legitimate way to run arbitrary APKs from a phone.

## Console and transfer routes (added after the Android run)

| Situation | Use | Why not adb |
|---|---|---|
| adbd offline / unauthorised / absent | `droideum console bridge` then `console send` | the serial port does not authenticate |
| need a root shell before the UI exists | `console send` (append `console=ttyS0,115200` at boot menu) | adb shell needs a working framework |
| move a file in with no networking | `droideum transfer file.apk` | adb push needs the same handshake that failed |
| try the image without touching the disk | `droideum live` | install is a GUI wizard |
| debugging a boot that never completes | `droideum console read`, `shots/` | `adb logcat` is unavailable until boot finishes |
