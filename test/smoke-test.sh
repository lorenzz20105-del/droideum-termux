#!/usr/bin/env bash
# Smoke test for droideum's plumbing. No VM, no Android: stub qemu/adb/curl
# binaries on PATH plus a fake QEMU monitor log, then assert that the CLI
# assembles the right command lines and lifecycle. Run: ./test/smoke-test.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
STUB="$WORK/stub"
mkdir -p "$STUB"
PASS=0; FAIL=0

t() { # t <name> <expected> <actual>
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); printf '  ok   %s\n' "$1"
  else FAIL=$((FAIL+1)); printf '  FAIL %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; fi
}
has() { if grep -qF -- "$2" "$3" 2>/dev/null; then PASS=$((PASS+1)); printf '  ok   %s\n' "$1";
        else FAIL=$((FAIL+1)); printf '  FAIL %s (no "%s" in %s)\n' "$1" "$2" "$3"; fi; }
hasnt() { if grep -qF -- "$2" "$3" 2>/dev/null; then FAIL=$((FAIL+1)); printf '  FAIL %s (unexpected "%s")\n' "$1" "$2";
          else PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; fi; }

# ---- stubs ------------------------------------------------------------------
cat > "$STUB/qemu-img" <<'EOF'
#!/bin/sh
echo "$@" >> "$STUB_CALLS"
case "$1" in
  create)  for a in "$@"; do case "$a" in *.img|*.qcow2) target="$a" ;; esac; done
           : > "${target:-/dev/null}"; echo '{"virtual-size": 17179869184, "format": "qcow2"}' ;;
  info)    echo '{"virtual-size": 17179869184, "format": "qcow2"}' ;;
  check)   exit 0 ;;
  dd)      # emit a 512-byte MBR; STUB_BOOT=yes gives it a real boot signature
           for a in "$@"; do case "$a" in of=*) dest="${a#of=}" ;; esac; done
           if [ "${STUB_BOOT:-no}" = "yes" ]; then
             { head -c 510 /dev/zero; printf '\125\252'; } > "${dest:-/dev/null}"
           else
             head -c 512 /dev/zero > "${dest:-/dev/null}"
           fi ;;
  snapshot)
      if [ "$2" = "-l" ]; then
        printf 'Snapshot list:\nID        TAG                 VM SIZE                DATE       VM CLOCK\n'
        state=" $(cat "${STUB_PRESEED:-/dev/null}" 2>/dev/null | tr '\n' ' ') "
        while IFS= read -r line; do
          case "$line" in
            "savevm "*)
              n=${line#savevm }
              case "$state" in *" $n "*) ;; *) state="$state$n " ;; esac ;;
            "delvm "*)
              n=${line#delvm }
              state=$(printf '%s' "$state" | sed "s/ $n / /") ;;
          esac
        done < "${STUB_MON_LOG:-/dev/null}" 2>/dev/null
        for n in $state; do
          printf '%-9s %-19s %-22s 2026-10-28 10:00:00 00:00:00.000\n' "1" "$n" "512M"
        done
      fi ;;
  resize)  echo resized ;;
esac
EOF
cat > "$STUB/qemu-system-x86_64" <<'EOF'
#!/bin/sh
echo "$@" > "$STUB_CALLS.qemu"
# Emulate a machine that powers off when the monitor receives 'quit'.
if [ -n "${STUB_MON_LOG:-}" ]; then
  : > "$STUB_MON_LOG"          # a fresh machine starts with an empty monitor log
  i=0
  while [ "$i" -lt 150 ]; do
    grep -q '^quit$' "$STUB_MON_LOG" 2>/dev/null && exit 0
    sleep 0.2; i=$((i + 1))
  done
  exit 0
fi
exec sleep 30
EOF
cat > "$STUB/curl" <<'EOF'
#!/bin/sh
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
[ -n "$out" ] && printf 'FAKE-ISO' > "$out"
exit 0
EOF
cat > "$STUB/adb" <<'EOF'
#!/bin/sh
echo "adb $*" >> "$STUB_CALLS.adb"
case "$*" in
  "devices")
      if [ "${STUB_ADB_MODE:-ok}" = "nodevice" ]; then
        printf 'List of devices attached\n'
      else
        printf 'List of devices attached\n127.0.0.1:5555\tdevice\n'
      fi ;;
  *"install"*)
      case "${STUB_ADB_MODE:-ok}" in
        ok)   echo "Performing Streamed Install"; echo "Success" ;;
        abis) echo "adb: failed to install app.apk: Failure [INSTALL_FAILED_NO_MATCHING_ABIS: Failed to extract native libraries, res=-113]" ;;
      esac ;;
  *"pm list packages -3"*) printf 'package:com.example.game\npackage:org.mozilla.firefox\n' ;;
  *"getprop sys.boot_completed"*) echo "1" ;;
  *"getprop"*) echo "x86_64" ;;
  *"wm size"*) echo "Physical size: 1080x2340" ;;
  *"wm density"*) echo "Physical density: 440" ;;
  *"uiautomator dump"*) echo "UI hierchary dumped to: /sdcard/window_dump.xml" ;;
  *"exec-out cat"*) echo '<?xml version="1.0"?><hierarchy><node class="android.widget.Button" text="OK" clickable="true" bounds="[0,0][10,10]"/></hierarchy>' ;;
  *"exec-out screencap"*) printf '\211PNG\r\n\032\n0000000000' ;;
  *"push"*) echo "1 file pushed." ;;
  *"pull"*) echo "1 file pulled." ;;
  *"settings get"*) echo "0" ;;
  *"df -m"*) echo "/dev/block/dm-2 16000 4000 12000 25% /data" ;;
  *) : ;;
esac
EOF
chmod +x "$STUB"/*

export STUB_CALLS="$WORK/calls.log"
export STUB_CALLS_QEMU="$WORK/calls.log.qemu"
export STUB_MON_LOG="$WORK/monitor.log"
export DROIDEUM_MONITOR_LOG="$WORK/monitor.log"
export STUB_PRESEED="$WORK/preseeded.txt"
: > "$STUB_PRESEED"
export PATH="$STUB:$PATH"
export DROIDEUM_HOME="$WORK/home"
export ISO_SHA1=skip
export VNC_DISPLAY=0
export STUB_BOOT=yes   # the main flow assumes Android is installed on the disk
export STOP_TIMEOUT=6
export MIN_FREE_MB=1   # the test workdir lives on a small tmpfs

D="$HERE/bin/droideum"
run() { bash "$D" "$@" >"$WORK/out" 2>&1; echo $?; }
hard_stop() {                     # ensure no stub VM survives into the next section
  run stop >/dev/null 2>&1
  local p; p=$(cat "$DROIDEUM_HOME/run/qemu.pid" 2>/dev/null || true)
  [ -n "${p:-}" ] && kill -9 "$p" 2>/dev/null
  rm -f "$DROIDEUM_HOME/run/qemu.pid" "$DROIDEUM_HOME/run/monitor.sock"
  : > "$STUB_MON_LOG"
  sleep 0.3
}
reset_snaps() { : > "$STUB_PRESEED"; for n in $1; do echo "$n" >> "$STUB_PRESEED"; done; }

echo "== setup =="
t   "setup exits 0"          "0" "$(run setup)"
run setup >/dev/null
has "iso cached"             "FAKE-ISO" "$DROIDEUM_HOME/cache/android-x86_64-9.0-r2.iso"
has "disk created"           "create" "$STUB_CALLS"
has "default config written" "RAM_MB" "$DROIDEUM_HOME/droideum.conf"

echo "== status =="
run status >/dev/null
has "status shows stopped"   "stopped" "$WORK/out"
has "status shows engine"    "tb-size" "$WORK/out"
has "status shows states"    "saved states" "$WORK/out"

echo "== launch (TCG tuning + drive opts) =="
reset_snaps ""
: > "$STUB_MON_LOG"
t   "start exits 0"          "0" "$(run start)"
sleep 0.3
has "qemu got -m"            "-m " "$STUB_CALLS_QEMU"
has "accel tuned tcg"        "-accel tcg,thread=multi,tb-size=256" "$STUB_CALLS_QEMU"
has "cpu max"                "-cpu max" "$STUB_CALLS_QEMU"
has "qcow2 discard enabled"  "discard=unmap" "$STUB_CALLS_QEMU"
has "qcow2 zero detection"   "detect-zeroes=unmap" "$STUB_CALLS_QEMU"
has "writeback cache"        "cache=writeback" "$STUB_CALLS_QEMU"
has "adb hostfwd"            "hostfwd=tcp:127.0.0.1:5555-:5555" "$STUB_CALLS_QEMU"
has "monitor socket"         "-monitor unix:" "$STUB_CALLS_QEMU"
t   "second start refused"   "1" "$(run start)"
has "refusal is explicit"    "already running" "$WORK/out"

echo "== clean shutdown ordering =="
t   "stop exits 0"           "0" "$(run stop)"
has "stop asked ACPI first"  "system_powerdown" "$STUB_MON_LOG"
hasnt "stop did not SIGKILL first" "SIGKILL" "$WORK/out"
run status >/dev/null
has "status shows stopped"   "stopped" "$WORK/out"

echo "== suspend / resume (warm state) =="
t   "suspend without VM fails" "1" "$(run suspend)"
has "suspend explains why"   "not running" "$WORK/out"
reset_snaps "autosave"
: > "$STUB_MON_LOG"
t   "start again"            "0" "$(run start)"
sleep 0.2
t   "suspend exits 0"        "0" "$(run suspend)"
has "savevm sent"            "savevm autosave" "$STUB_MON_LOG"
has "quit sent after savevm" "quit" "$STUB_MON_LOG"
has "suspend confirms state" "suspended into 'autosave'" "$WORK/out"
: > "$STUB_MON_LOG"
t   "resume exits 0"         "0" "$(run resume)"
sleep 0.2
has "resume passes -loadvm"  "-loadvm autosave" "$STUB_CALLS_QEMU"
has "resume skips the boot"  "state restored from the disk image" "$WORK/out"
run stop >/dev/null
reset_snaps ""
t   "resume unknown state fails" "1" "$(run resume nope)"
has "resume lists states"    "available states" "$WORK/out"

echo "== up (headless, resume-aware) =="
reset_snaps "autosave"
: > "$STUB_MON_LOG"
t   "up resumes warm state"  "0" "$(run up)"
has "up used the snapshot"   "-loadvm autosave" "$STUB_CALLS_QEMU"
has "up waits for boot prop" "adb -s 127.0.0.1:5555 shell getprop sys.boot_completed" "$STUB_CALLS.adb"
run stop >/dev/null
reset_snaps ""
: > "$STUB_MON_LOG"
t   "up --cold boots fresh"  "0" "$(run up --cold)"
has "cold boot is headless"  "-display none" "$STUB_CALLS_QEMU"
hasnt "no -loadvm on cold boot" "-loadvm" "$STUB_CALLS_QEMU"
run down >/dev/null

echo "== android-side tuning =="
t   "tune exits 0"           "0" "$(run tune)"
has "animations disabled"    "settings put global window_animation_scale 0" "$STUB_CALLS.adb"
has "animator scale disabled" "settings put global animator_duration_scale 0" "$STUB_CALLS.adb"
has "screen kept awake"      "svc power stayon true" "$STUB_CALLS.adb"
has "lock screen disabled"   "locksettings set-disabled true" "$STUB_CALLS.adb"
has "logcat buffer raised"   "logcat -G 4M" "$STUB_CALLS.adb"

echo "== benchmark =="
t   "bench exits 0"          "0" "$(run bench 1)"
has "bench reports dump"     "ui dump" "$WORK/out"
has "bench reports settle"   "tap -> UI settled" "$WORK/out"
has "bench saves json"       "bench-" "$WORK/out"

echo "== file transfer =="
printf 'apk' > "$WORK/app.apk"
t   "push exits 0"           "0" "$(run push "$WORK/app.apk")"
has "push created dest dir"  "mkdir -p /sdcard/Download" "$STUB_CALLS.adb"
has "push reported"          "app.apk" "$WORK/out"
t   "push missing file fails" "1" "$(run push "$WORK/nope.apk")"
t   "pull exits 0"           "0" "$(run pull /sdcard/x.png "$WORK")"
has "pull used adb"          "pull /sdcard/x.png" "$STUB_CALLS.adb"

echo "== profiles =="
t   "profile list exits 0"   "0" "$(run profile list)"
has "lists bliss14"          "bliss14" "$WORK/out"
t   "profile use bliss14"    "0" "$(run profile use bliss14)"
has "config records profile" "PROFILE_NAME=bliss14" "$DROIDEUM_HOME/droideum.conf"
has "config skips checksum"  'ISO_SHA1="skip"' "$DROIDEUM_HOME/droideum.conf"
has "config raises RAM"      'RAM_MB="3072"' "$DROIDEUM_HOME/droideum.conf"
t   "profile use android9"   "0" "$(run profile use android9)"
has "profile block replaced" "PROFILE_NAME=android9" "$DROIDEUM_HOME/droideum.conf"
hasnt "no stale profile block" "PROFILE_NAME=bliss14" "$DROIDEUM_HOME/droideum.conf"
run profile use android9 >/dev/null
t   "profile blocks do not pile up" "1" "$(grep -c '>>> droideum profile' "$DROIDEUM_HOME/droideum.conf")"
t   "unknown profile fails"  "1" "$(run profile use wat)"

echo "== snapshots =="
t   "snapshot ok"            "0" "$(run snapshot test1)"
has "snapshot file"          "-b " "$STUB_CALLS"
run snapshots >/dev/null
has "snapshot listed"        "test1" "$WORK/out"
t   "restore ok"             "0" "$(run restore test1)"
t   "restore unknown fails"  "1" "$(run restore nope)"

echo "== apk sideboarding =="
t   "install ok exits 0"     "0" "$(STUB_ADB_MODE=ok run apk install "$WORK/app.apk")"
has "adb install -r -g used" "install -r -g -d" "$STUB_CALLS.adb"
t   "abi failure exits 1"    "1" "$(STUB_ADB_MODE=abis run apk install "$WORK/app.apk")"
has "abi advice"             "armbridge" "$WORK/out"
run apk list >/dev/null
has "apk list strips prefix" "com.example.game" "$WORK/out"

echo "== doctor =="
t   "doctor exits 0 (stubbed)" "0" "$(run doctor)"
has "doctor checks qcow2"    "consistent" "$WORK/out"
has "doctor reports accel"   "TCG" "$WORK/out"

echo "== agent wiring =="
cat > "$STUB/droideum-agentd" <<'EOF'
#!/bin/sh
echo "agentd $*"
EOF
chmod +x "$STUB/droideum-agentd"
t   "agent tool exits 0"     "0" "$(run agent tool)"
has "brief list shows a tool" "input.tap" "$WORK/out"
t   "agent delegates to agentd" "0" "$(AGENTD_BIN="$STUB/droideum-agentd" run agent list)"
has "delegation passes args"  "agentd list" "$WORK/out"
t   "agent config exits 0"   "0" "$(run agent config claude)"
has "config is an MCP block" '"mcpServers"' "$WORK/out"
has "config has autostart"   "--autostart" "$WORK/out"
t   "unknown agent sub fails" "1" "$(run agent frobnicate)"

echo "== misc =="
t   "unknown command fails"  "1" "$(run wat)"
run version >/dev/null
has "version prints"         "droideum 1.3.0" "$WORK/out"
run res 1600x900 320 >/dev/null
has "res passes wm size"     "wm size 1600x900" "$STUB_CALLS.adb"
has "res passes wm density"  "wm density 320" "$STUB_CALLS.adb"

echo "== regressions found by running it for real =="
hard_stop
reset_snaps ""
rm -f "$DROIDEUM_HOME/android.img"
run setup >/dev/null 2>&1 || true          # fresh disk, no boot loader on it
export STUB_BOOT=no
t   "start refuses an uninstalled disk" "1" "$(run start)"
has "uninstalled disk is explained"   "no boot loader" "$WORK/out"
has "uninstalled disk names install"  "droideum install" "$WORK/out"
t   "up refuses the same way"         "1" "$(run up)"
has "up does not wait for an impossible boot" "not a boot timeout" "$WORK/out"

export STUB_BOOT=yes
t   "start boots once a boot loader exists" "0" "$(run start)"
sleep 0.5

# savevm cannot overwrite an existing tag: the stale state must be deleted first
reset_snaps "autosave"
t   "suspend over an existing state" "0" "$(run suspend)"
has "old state deleted first"        "delvm autosave" "$STUB_MON_LOG"
t   "delvm precedes savevm"          "delvm autosave" "$(head -1 "$STUB_MON_LOG")"
has "replacement announced"          "replacing existing state" "$WORK/out"
run stop >/dev/null 2>&1

# a guest with no adb (not our Android image) must not cost minutes
export STOP_QUICK_TIMEOUT=1 STOP_TIMEOUT=30
t   "restart for the shutdown test"  "0" "$(run start)"
sleep 0.5
t   "stop works without adb"         "0" "$(STUB_ADB_MODE=nodevice run stop)"
has "short ACPI grace without adb"   "non-Android guest" "$WORK/out"
hasnt "did not wait the full timeout" "power off (30s)" "$WORK/out"

# resume must not block on an adb that will never appear
export RESUME_ADB_TIMEOUT=1
t   "resume without adb still wakes" "0" "$(STUB_ADB_MODE=nodevice run resume)"
has "resume reports the wake time"   "restored in" "$WORK/out"
has "resume explains missing adb"    "no adbd" "$WORK/out"

hard_stop
export STUB_ADB_MODE=ok RESUME_ADB_TIMEOUT=20 STOP_QUICK_TIMEOUT=12 STOP_TIMEOUT=6

echo "== serial console, live boot, offline transfer =="
stub_mtools() {
  cat > "$STUB/mformat" <<'EOF'
#!/bin/sh
echo "mformat $*" >> "$STUB_CALLS"
exit 0
EOF
  cat > "$STUB/mcopy" <<'EOF'
#!/bin/sh
echo "mcopy $*" >> "$STUB_CALLS"
exit 0
EOF
  cat > "$STUB/truncate" <<'EOF'
#!/bin/sh
echo "truncate $*" >> "$STUB_CALLS"
exec /usr/bin/truncate "$@"
EOF
  chmod +x "$STUB/mformat" "$STUB/mcopy" "$STUB/truncate"
}
stub_mtools
hard_stop
t   "start (serial port wired)"   "0" "$(run start)"
sleep 0.5
has "qemu exposes a serial console" "-serial tcp:127.0.0.1:4555,server,nowait" "$STUB_CALLS.qemu"
t   "console send exits 0"         "0" "$(run console send 'getprop ro.build.version.release')"
has "console command queued"       "getprop ro.build.version.release" "$DROIDEUM_HOME/run/console.in"
t   "console unknown sub fails"    "1" "$(run console frobnicate)"
has "unknown console sub is reported" "unknown console subcommand" "$WORK/out"
run console >/dev/null
has "console help explains why"    "adbd may be down" "$WORK/out"

printf 'APKDATA' > "$WORK/app.apk"
t   "transfer exits 0"             "0" "$(run transfer "$WORK/app.apk")"
has "transfer formats a FAT image" "mformat" "$STUB_CALLS"
has "transfer copies the file"     "mcopy" "$STUB_CALLS"
has "transfer prints guest steps"  "mount -t vfat" "$WORK/out"
t   "transfer without a file fails" "1" "$(run transfer)"
t   "transfer --eject exits 0"     "0" "$(run transfer --eject)"
run stop >/dev/null 2>&1

t   "live boot exits 0"            "0" "$(run live)"
sleep 0.3
has "live boot uses the ISO as cdrom" "-boot d" "$STUB_CALLS.qemu"
has "live boot attaches no disk"   "-cdrom" "$STUB_CALLS.qemu"
hasnt "live boot has no hard disk" "android.img,if=ide" "$STUB_CALLS.qemu"
run stop >/dev/null 2>&1

export RAM_MB=768
run doctor >/dev/null
has "doctor warns about low RAM"   "below what Android 9 needs" "$WORK/out"
export RAM_MB=1536

echo
printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
