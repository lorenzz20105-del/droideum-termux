#!/usr/bin/env python3
"""
Protocol-level tests for droideum-agentd. No phone, no VM, no Android.

  test/mcp-test.py                 # offline: JSON-RPC contract + parsers
  STUB_ADB=1 test/mcp-test.py      # also drive the tools against a fake device

The stub mode is the interesting one: it starts a fake `adb` that returns canned
`screencap`, `uiautomator dump`, `dumpsys window`, `wm size` output, then runs a
complete agent loop (observe -> find -> tap -> wait -> observe) and asserts the
server turned it into the right UI tree, coordinates and adb command lines.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "bin", "droideum-agentd")

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


class Client:
    """Minimal MCP client: newline-delimited JSON-RPC over stdio."""

    def __init__(self, env=None):
        self.p = subprocess.Popen(
            [sys.executable, SERVER, "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env or os.environ.copy(),
        )
        self.next_id = 1

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self.next_id
            self.next_id += 1
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        line = self.p.stdout.readline()
        return json.loads(line)

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()


STUB_ADB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import base64, os, sys
    args = sys.argv[1:]
    calls = os.environ.get("STUB_CALLS")
    if calls:
        with open(calls, "a") as fh:
            fh.write(" ".join(args) + "\\n")
    joined = " ".join(args)
    UI = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
    <hierarchy rotation="0">
      <node class="android.widget.FrameLayout" package="com.example.app" bounds="[0,0][1080,2340]">
        <node class="android.widget.TextView" package="com.example.app" text="Welcome back"
              bounds="[60,180][1020,240]" resource-id="com.example.app:id/title" />
        <node class="android.widget.EditText" package="com.example.app" text=""
              content-desc="Email address" bounds="[60,320][1020,420]"
              resource-id="com.example.app:id/email" clickable="true" focused="true" />
        <node class="android.widget.Button" package="com.example.app" text="Continue"
              bounds="[60,480][1020,600]" resource-id="com.example.app:id/continue"
              clickable="true" />
        <node class="androidx.recyclerview.widget.RecyclerView" package="com.example.app"
              bounds="[0,620][1080,2300]" scrollable="true" />
      </node>
    </hierarchy>"""
    if "devices" in joined:
        print("List of devices attached")
        print("127.0.0.1:5555\\tdevice")
    elif "connect" in joined:
        print("connected to 127.0.0.1:5555")
    elif joined.startswith("-s 127.0.0.1:5555 shell getprop sys.boot_completed"):
        print("1")
    elif joined.startswith("-s 127.0.0.1:5555 shell getprop ro.build.version.release"):
        print("9")
    elif "getprop ro.product.cpu.abilist" in joined:
        print("x86_64,x86,armeabi-v7a,armeabi")
    elif "getprop" in joined:
        print("")
    elif "wm size" in joined:
        print("Physical size: 1080x2340")
    elif "wm density" in joined:
        print("Physical density: 440")
    elif "mCurrentFocus" in joined:
        print("  mCurrentFocus=Window{1a2b3c u0 com.example.app/com.example.app.MainActivity}")
    elif "uiautomator dump" in joined:
        print("UI hierchary dumped to: /sdcard/window_dump.xml")
    elif "exec-out cat" in joined:
        print(UI)
    elif "exec-out screencap" in joined:
        sys.stdout.buffer.write(base64.b64decode(
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="))
    elif "settings get global window_animation_scale" in joined:
        print("0")
    elif "settings get" in joined:
        print("1")
    elif "settings put" in joined:
        pass
    elif "locksettings" in joined:
        print("Lock screen disabled: true")
    elif "pm list packages" in joined:
        print("package:com.example.app")
        print("package:org.mozilla.firefox")
    elif "df -m /data" in joined:
        print("/dev/block/dm-2 16000 4000 12000 25% /data")
    elif "input " in joined:
        pass
    elif "monkey" in joined:
        print("Events injected: 1")
    else:
        pass
''')


def offline_tests():
    print("== offline: protocol contract ==")
    c = Client()
    init = c.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}})
    check("initialize returns protocolVersion", init["result"]["protocolVersion"] == "2024-11-05")
    check("initialize advertises tools", "tools" in init["result"]["capabilities"])
    check("serverInfo named", init["result"]["serverInfo"]["name"] == "droideum-agentd")
    check("instructions included", len(init["result"].get("instructions", "")) > 80)

    c.rpc("notifications/initialized", notify=True)
    tools = c.rpc("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    check("tools/list returns the catalogue", len(tools) >= 20, str(len(tools)))
    for expected in ("observe", "ui.dump", "ui.find", "input.tap", "input.text",
                     "app.launch", "wait.idle", "task.checkpoint", "task.rollback"):
        check(f"tool present: {expected}", expected in names)
    schemas_ok = all(t["inputSchema"]["type"] == "object" for t in tools)
    check("all inputSchemas are objects", schemas_ok)
    tap = next(t for t in tools if t["name"] == "input.tap")
    check("input.tap x is integer", tap["inputSchema"]["properties"]["x"]["type"] == "integer")
    check("input.tap requires x,y", set(tap["inputSchema"].get("required", [])) == {"x", "y"})

    err = c.rpc("tools/call", {"name": "does.not.exist", "arguments": {}})
    body = json.loads(err["result"]["content"][0]["text"])
    check("unknown tool is an error result", err["result"]["isError"] is True)
    check("unknown tool has a code", body["code"] == "UNKNOWN_TOOL")
    check("unknown tool lists alternatives", "observe" in body.get("hint", ""))

    bad = c.rpc("not.a.method", {})
    check("unknown method -> JSON-RPC error", "error" in bad)

    check("tools/list is stable across calls",
          [t["name"] for t in c.rpc("tools/list")["result"]["tools"]] == names)
    check("no tool name has spaces", all(" " not in n for n in names))

    prompts = c.rpc("prompts/list")["result"]["prompts"]
    check("prompt template exists", prompts[0]["name"] == "drive_android_app")
    got = c.rpc("prompts/get", {"name": "drive_android_app",
                                "arguments": [{"name": "app_package", "value": "com.example.app"},
                                              {"name": "goal", "value": "sign in"}]})
    text = got["result"]["messages"][0]["content"]["text"]
    check("prompt names the app", "com.example.app" in text)
    check("prompt names the goal", "sign in" in text)
    check("prompt describes the loop", "observe" in text and "wait.idle" in text)

    # out-of-bounds is rejected before it reaches the device
    # no device is attached here, so the failure must be reported as such rather
    # than silently "succeeding"; the bounds check itself is asserted in stub mode
    oob = c.rpc("tools/call", {"name": "input.tap", "arguments": {"x": 5000, "y": 10}})
    body = json.loads(oob["result"]["content"][0]["text"])
    check("tap without a device is an error", oob["result"]["isError"] is True)
    check("error carries an actionable code and hint",
          body.get("code") in ("NO_ADB", "NO_DEVICE") and "droideum" in body.get("hint", ""),
          json.dumps(body))
    c.close()
    check("server exits when stdin closes", c.p.poll() == 0)


def stub_tests():
    print("\n== stubbed device: full agent loop ==")
    tmp = tempfile.mkdtemp()
    stubdir = os.path.join(tmp, "bin")
    os.makedirs(stubdir)
    adb_path = os.path.join(stubdir, "adb")
    with open(adb_path, "w") as fh:
        fh.write(STUB_ADB)
    os.chmod(adb_path, 0o755)
    calls = os.path.join(tmp, "calls.log")

    env = os.environ.copy()
    env["PATH"] = stubdir + os.pathsep + env["PATH"]
    env["STUB_CALLS"] = calls
    env["DROIDEUM_HOME"] = tmp

    c = Client(env)
    c.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "0"}})

    names26 = [t["name"] for t in c.rpc("tools/list")["result"]["tools"]]
    check("device.tune is exposed", "device.tune" in names26)

    st = c.rpc("tools/call", {"name": "device.status", "arguments": {}})
    data = st["result"]["structuredContent"]
    check("status: adb connected", data.get("adb") is True)
    check("status: boot completed", data.get("boot_completed") is True)
    check("status: android 9", data.get("android") == "9")
    check("status: abilist reported", "x86_64" in (data.get("abis") or ""))
    check("status: screen parsed", data["screen"]["w"] == 1080 and data["screen"]["h"] == 2340)
    check("status: density parsed", data["screen"]["density"] == 440)
    check("status: data free parsed", data.get("data_free_mb") == 12000)
    check("status: reports tuning state", data.get("tuned") is True and data.get("animations") == "0")

    tune = c.rpc("tools/call", {"name": "device.tune", "arguments": {}})
    td = tune["result"]["structuredContent"]
    check("tune reports applied keys", "window_animation_scale" in td["applied"])
    check("tune returns previous values", "window_animation_scale" in td["previous"])
    with open(calls) as fh:
        tlog = fh.read()
    check("tune put animation scale 0", "settings put global window_animation_scale 0" in tlog)
    check("tune kept the screen on", "svc power stayon true" in tlog)
    check("tune disabled the lock screen", "locksettings set-disabled true" in tlog)

    ob = c.rpc("tools/call", {"name": "observe", "arguments": {"only_interactive": True}})
    ui = ob["result"]["structuredContent"]["ui"]
    check("observe returns nodes", ui["count"] >= 3, str(ui["count"]))
    check("observe reports foreground",
          ob["result"]["structuredContent"]["meta"]["foreground"]["package"] == "com.example.app")
    labels = [n.get("text") or n.get("desc") for n in ui["nodes"]]
    check("observe sees 'Continue'", "Continue" in labels)
    check("observe sees the email field desc", "Email address" in labels)
    cont = next(n for n in ui["nodes"] if n.get("text") == "Continue")
    check("continue button has tap coords", cont["xy"] == [540, 540], str(cont.get("xy")))
    check("continue button flagged tappable", cont.get("tap") is True)
    check("nodes carry short keys only",
          all(set(n) <= {"cls", "text", "desc", "id", "xy", "bounds", "tap", "scroll",
                         "focused", "checked", "disabled"} for n in ui["nodes"]))
    check("no layout frames in dump", all(n.get("cls") != "FrameLayout" for n in ui["nodes"]))

    found = c.rpc("tools/call", {"name": "ui.find", "arguments": {"query": "continue"}})
    m = found["result"]["structuredContent"]["matches"][0]
    check("ui.find is case-insensitive", m["center"] == [540, 540])
    check("ui.find returns center", "center" in m)

    miss = c.rpc("tools/call", {"name": "ui.find", "arguments": {"query": "delete account"}})
    check("ui.find miss is an error", miss["result"]["isError"] is True)
    body = json.loads(miss["result"]["content"][0]["text"])
    check("miss suggests ui.dump", "ui.dump" in body.get("hint", ""))

    tap = c.rpc("tools/call", {"name": "input.tap", "arguments": {"x": 540, "y": 540}})
    check("tap succeeds", tap["result"]["isError"] is False)
    check("tap reports coordinates", tap["result"]["structuredContent"]["tapped"] == [540, 540])
    with open(calls) as fh:
        log = fh.read()
    check("tap reached adb as input tap", "shell input tap 540 540" in log, log[-300:])

    oob = c.rpc("tools/call", {"name": "input.tap", "arguments": {"x": 5000, "y": 10}})
    body = json.loads(oob["result"]["content"][0]["text"])
    check("out-of-bounds tap rejected", oob["result"]["isError"] is True)
    check("out-of-bounds code is OUT_OF_BOUNDS", body.get("code") == "OUT_OF_BOUNDS", json.dumps(body))
    check("out-of-bounds explains coordinates", "device pixels" in body.get("hint", ""))
    check("out-of-bounds tap never reached adb", "input tap 5000" not in open(calls).read())

    txt = c.rpc("tools/call", {"name": "input.text", "arguments": {"text": "me@example.com"}})
    check("ascii text accepted", txt["result"]["isError"] is False)
    with open(calls) as fh:
        log = fh.read()
    check("text reached adb", "input text me@example.com" in log)

    uni = c.rpc("tools/call", {"name": "input.text", "arguments": {"text": "café ☕"}})
    check("non-ascii rejected with hint", uni["result"]["isError"] is True)
    body = json.loads(uni["result"]["content"][0]["text"])
    check("non-ascii hint mentions a workaround", "ADBKeyboard" in body.get("hint", ""))

    key = c.rpc("tools/call", {"name": "input.key", "arguments": {"key": "back"}})
    check("key lowercased accepted", key["result"]["isError"] is False)
    check("key sent as BACK", "keyevent BACK" in open(calls).read())

    idle = c.rpc("tools/call", {"name": "wait.idle",
                                "arguments": {"timeout_s": 5, "quiet_ms": 300, "poll_ms": 100}})
    check("wait.idle settles on a static screen",
          idle["result"]["structuredContent"]["idle"] is True)

    pull = c.rpc("tools/call", {"name": "app.launch", "arguments": {"package": "com.example.app"}})
    check("app.launch ok", pull["result"]["isError"] is False)
    check("launch used monkey", "monkey -p com.example.app" in open(calls).read())

    pkgs = c.rpc("tools/call", {"name": "app.list", "arguments": {}})
    check("app.list returns packages",
          pkgs["result"]["structuredContent"]["packages"] == ["com.example.app", "org.mozilla.firefox"])

    shell = c.rpc("tools/call", {"name": "shell.exec", "arguments": {"command": "echo hi"}})
    check("shell.exec returns rc", "rc" in shell["result"]["structuredContent"])

    missing = c.rpc("tools/call", {"name": "app.launch", "arguments": {}})
    check("missing required arg is an error", missing["result"]["isError"] is True)

    c.close()


def main():
    offline_tests()
    if os.environ.get("STUB_ADB") == "1":
        stub_tests()
    else:
        print("\n(set STUB_ADB=1 to also run the stubbed-device loop)")
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
