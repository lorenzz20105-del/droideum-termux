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
        print("Physical size: 1920x1080")
        print("Override size: 1080x2340")
    elif "wm density" in joined:
        print("Physical density: 320")
        print("Override density: 440")
    elif "mCurrentFocus" in joined:
        print("  mCurrentFocus=Window{1a2b3c u0 com.example.app/com.example.app.MainActivity}")
    elif "uiautomator dump" in joined:
        print("UI hierchary dumped to: /sdcard/window_dump.xml")
    elif "exec-out cat" in joined:
        print(UI)
    elif "exec-out screencap" in joined:
        sys.stdout.buffer.write(base64.b64decode(
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="))
    elif "ime list" in joined:
        ime_flag = os.path.join(os.environ.get("DROIDEUM_HOME", ""), "enable_adb_ime")
        if os.path.exists(ime_flag):
            print("com.android.adbkeyboard/.AdbIME")
    elif "ADB_INPUT_B64" in joined:
        print("Broadcasting: Intent { act=ADB_INPUT_B64 }")
    elif "cmd clipboard set-primary-clip" in joined:
        clip_file = os.path.join(os.environ.get("DROIDEUM_HOME", ""), "clip.txt")
        with open(clip_file, "w") as fh:
            fh.write(args[-1].split("set-primary-clip ", 1)[-1].strip("'\\""))
    elif "cmd clipboard get-primary-clip" in joined:
        clip_file = os.path.join(os.environ.get("DROIDEUM_HOME", ""), "clip.txt")
        print(open(clip_file).read() if os.path.exists(clip_file) else "")
    elif "install-multiple" in joined or " install " in joined:
        print("Performing Streamed Install")
        print("Success")
    elif "am start " in joined:
        print("Starting: Intent { ... }")
    elif "logcat -d" in joined:
        print("11-01 12:00:00.000 I/ActivityManager( 1000): Displayed com.example.app/.MainActivity")
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
    check("tools/list returns all 30 tools", len(tools) == 30, str(len(tools)))
    for expected in ("observe", "ui.dump", "ui.find", "ui.wait_for", "ui.tap_match",
                     "input.tap", "input.text", "app.launch", "app.intent",
                     "device.clipboard", "wait.idle", "task.checkpoint", "task.rollback"):
        check(f"tool present: {expected}", expected in names)
    schemas_ok = all(t["inputSchema"]["type"] == "object" for t in tools)
    check("all inputSchemas are objects", schemas_ok)
    arrays_ok = all(
        "items" in prop
        for t in tools
        for prop in t["inputSchema"]["properties"].values()
        if prop.get("type") == "array"
    )
    check("all array properties define items schema", arrays_ok)
    tap = next(t for t in tools if t["name"] == "input.tap")
    check("input.tap x is integer", tap["inputSchema"]["properties"]["x"]["type"] == "integer")
    check("input.tap requires x,y", set(tap["inputSchema"].get("required", [])) == {"x", "y"})
    lp = next(t for t in tools if t["name"] == "input.long_press")
    sw = next(t for t in tools if t["name"] == "input.swipe")
    check("input.long_press requires x,y", set(lp["inputSchema"].get("required", [])) == {"x", "y"})
    check("input.swipe requires x1,y1,x2,y2",
          set(sw["inputSchema"].get("required", [])) == {"x1", "y1", "x2", "y2"})

    resources = c.rpc("resources/list")["result"]["resources"]
    r_uris = {r["uri"] for r in resources}
    check("resources/list exposes live resources",
          {"droideum://status", "droideum://ui", "droideum://logcat"} <= r_uris)

    # Verify examples/agent_loop.py produces strict OpenAI-compatible function names
    import importlib.util
    import re as _re
    spec = importlib.util.spec_from_file_location(
        "agent_loop", os.path.join(ROOT, "examples", "agent_loop.py")
    )
    agent_loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_loop)
    oa_tools = agent_loop.openai_tools()
    check("agent_loop OpenAI tool names match ^[a-zA-Z0-9_-]+$",
          all(_re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["function"]["name"]) for t in oa_tools))
    check("agent_loop OpenAI tool names round-trip back to MCP tools",
          all(agent_loop._from_fn_name(t["function"]["name"]) in names for t in oa_tools))

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

    inj = c.rpc("tools/call", {"name": "app.launch", "arguments": {"package": "com.bad; reboot"}})
    inj_body = json.loads(inj["result"]["content"][0]["text"])
    check("shell metacharacters in package rejected",
          inj["result"]["isError"] is True and inj_body.get("code") == "BAD_ARGS")

    # v1.4.0: screen_changed delta detection on consecutive identical observations
    ob1 = c.rpc("tools/call", {"name": "ui.dump", "arguments": {}})["result"]["structuredContent"]
    ob2 = c.rpc("tools/call", {"name": "ui.dump", "arguments": {}})["result"]["structuredContent"]
    check("ui.dump includes deterministic ui_hash", len(ob1.get("ui_hash", "")) == 12)
    check("identical consecutive ui.dump sets screen_changed=False", ob2.get("screen_changed") is False)

    # v1.4.0: ui.wait_for & ui.tap_match
    wf = c.rpc("tools/call", {"name": "ui.wait_for",
                              "arguments": {"query": "Continue", "timeout_s": 2}})
    check("ui.wait_for finds visible element",
          wf["result"]["isError"] is False and wf["result"]["structuredContent"]["found"] is True)
    wf_to = c.rpc("tools/call", {"name": "ui.wait_for",
                                 "arguments": {"query": "NeverVisible", "timeout_s": 0.3, "poll_ms": 100}})
    wf_err = json.loads(wf_to["result"]["content"][0]["text"])
    check("ui.wait_for times out with WAIT_TIMEOUT",
          wf_to["result"]["isError"] is True and wf_err.get("code") == "WAIT_TIMEOUT")

    tm = c.rpc("tools/call", {"name": "ui.tap_match",
                              "arguments": {"query": "Continue", "wait_s": 1}})
    check("ui.tap_match finds and taps center in one step",
          tm["result"]["isError"] is False
          and tm["result"]["structuredContent"]["tapped"] == [540, 540])

    # v1.4.0: bounds checks on long_press and swipe
    oob_lp = c.rpc("tools/call", {"name": "input.long_press", "arguments": {"x": 5000, "y": 100}})
    check("out-of-bounds long_press rejected", oob_lp["result"]["isError"] is True)
    oob_sw = c.rpc("tools/call", {"name": "input.swipe",
                                  "arguments": {"x1": 100, "y1": 100, "x2": 100, "y2": 9000}})
    check("out-of-bounds swipe rejected", oob_sw["result"]["isError"] is True)

    # v1.4.0: Unicode input via ADBKeyboard base64 broadcast + submit=True
    with open(os.path.join(tmp, "enable_adb_ime"), "w") as fh:
        fh.write("1")
    uni_ok = c.rpc("tools/call", {"name": "input.text",
                                  "arguments": {"text": "café ☕", "submit": True}})
    check("unicode input succeeds when ADBKeyboard is installed",
          uni_ok["result"]["isError"] is False
          and uni_ok["result"]["structuredContent"]["method"] == "adbkeyboard_b64"
          and uni_ok["result"]["structuredContent"]["submitted"] is True)
    check("unicode broadcast sent as ADB_INPUT_B64", "ADB_INPUT_B64" in open(calls).read())

    # v1.4.0: split APK bundle (.apks) installation with ABI filtering
    import zipfile
    bundle_path = os.path.join(tmp, "sample.apks")
    with zipfile.ZipFile(bundle_path, "w") as zf:
        zf.writestr("base.apk", b"BASE")
        zf.writestr("split_config.arm64_v8a.apk", b"ARM64")
        zf.writestr("split_config.x86_64.apk", b"X86_64")
    inst = c.rpc("tools/call", {"name": "app.install", "arguments": {"path": bundle_path}})
    check("app.install installs split .apks bundle (2 splits kept, arm64 filtered)",
          inst["result"]["isError"] is False
          and inst["result"]["structuredContent"].get("splits") == 2)

    # v1.4.0: app.intent & device.clipboard & resources/read
    intent_res = c.rpc("tools/call", {"name": "app.intent",
                                      "arguments": {"uri": "https://example.com", "settle_s": 0.05}})
    check("app.intent dispatches VIEW intent",
          intent_res["result"]["isError"] is False and "am start" in open(calls).read())

    clip_set = c.rpc("tools/call", {"name": "device.clipboard",
                                    "arguments": {"action": "set", "text": "otp-482910"}})
    clip_get = c.rpc("tools/call", {"name": "device.clipboard", "arguments": {"action": "get"}})
    check("device.clipboard set+get round-trip",
          clip_set["result"]["isError"] is False
          and clip_get["result"]["structuredContent"].get("text") == "otp-482910")

    res_status = c.rpc("resources/read", {"uri": "droideum://status"})
    res_ui = c.rpc("resources/read", {"uri": "droideum://ui"})
    res_log = c.rpc("resources/read", {"uri": "droideum://logcat"})
    check("resources/read serves status, ui, and logcat",
          json.loads(res_status["result"]["contents"][0]["text"])["adb"] is True
          and json.loads(res_ui["result"]["contents"][0]["text"])["count"] >= 3
          and "Displayed com.example.app" in res_log["result"]["contents"][0]["text"])

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
