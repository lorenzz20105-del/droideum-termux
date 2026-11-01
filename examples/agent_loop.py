#!/usr/bin/env python3
"""
A dependency-free agent loop for droideum (v1.5.0).

Two modes:

  # 1. No LLM at all — prove the control plane works:
  python3 examples/agent_loop.py smoke

  # 2. Hand the tools to any OpenAI-compatible model over a persistent MCP stdio
  #    session (keeps geometry caches, `ref` IDs, and `screen_changed` hashes hot):
  export OPENAI_API_KEY=...            # or OPENAI_BASE_URL=http://localhost:8080/v1
  python3 examples/agent_loop.py llm "Open Settings and tell me the Android version"
  python3 examples/agent_loop.py llm --vision "Inspect the current screen visually"
"""

import json
import os
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError

DROIDEUM = os.environ.get("DROIDEUM_BIN", "droideum")
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_AGENTD = os.path.join(os.path.dirname(HERE), "bin", "droideum-agentd")


class McpSession:
    """Persistent JSON-RPC 2.0 MCP client over stdio to `droideum-agentd`."""

    def __init__(self):
        if shutil.which(DROIDEUM):
            cmd = [DROIDEUM, "agent", "serve"]
        else:
            cmd = [sys.executable, LOCAL_AGENTD, "serve"]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._id = 0
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "droideum-agent-loop", "version": "1.5.0"},
        })
        self._notify("notifications/initialized")

    def _notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _rpc(self, method, params=None):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err_tail = self.proc.stderr.read()[-400:] if self.proc.stderr else ""
            raise RuntimeError(f"MCP server closed stdout unexpectedly: {err_tail}")
        resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError(f"MCP RPC error: {resp['error']}")
        return resp.get("result") or {}

    def call_tool(self, tool_name, **args):
        res = self._rpc("tools/call", {"name": tool_name, "arguments": args})
        if res.get("isError"):
            try:
                err = json.loads(res["content"][0]["text"])
            except Exception:
                err = {"error": str(res)}
            raise RuntimeError(f"{tool_name} failed: {err.get('error')} | hint: {err.get('hint')}")
        images = [c.get("data") for c in (res.get("content") or []) if c.get("type") == "image"]
        return res.get("structuredContent"), images

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def tool(tool_name, session=None, **args):
    """Call one droideum tool (over persistent MCP if given, else one-shot CLI)."""
    if session is not None:
        structured, _ = session.call_tool(tool_name, **args)
        return structured
    p = subprocess.run(
        [DROIDEUM, "agent", "call", tool_name, "--json", json.dumps(args)],
        capture_output=True, text=True, timeout=180,
    )
    if p.returncode == 0:
        try:
            return json.loads(p.stdout).get("structured")
        except json.JSONDecodeError:
            return {"raw": p.stdout}
    try:
        payload = json.loads(p.stdout)
        err = json.loads(payload["content"][0]["text"])
    except Exception:
        err = {"error": p.stdout.strip() or p.stderr.strip()}
    raise RuntimeError(f"{tool_name} failed: {err.get('error')} | hint: {err.get('hint')}")


# ---------------------------------------------------------------------------
# mode 1: deterministic smoke run, no model required
# ---------------------------------------------------------------------------
def smoke():
    sess = McpSession()
    try:
        print("1. device.status")
        st = tool("device.status", session=sess)
        print("   ", json.dumps({k: st.get(k) for k in
                                 ("vm_running", "adb", "boot_completed", "android", "screen")}))
        if not st.get("adb"):
            print("    device not reachable — run `droideum up` first")
            return 1

        print("2. task.checkpoint before-task")
        try:
            print("   ", tool("task.checkpoint", session=sess, name="before-task"))
        except RuntimeError as e:
            print("    (skipped)", e)

        print("3. observe (interactive nodes + ref IDs)")
        obs = tool("observe", session=sess, only_interactive=True)
        fg = (obs.get("meta") or {}).get("foreground")
        nodes = (obs.get("ui") or {}).get("nodes", [])
        print(f"    foreground={fg} nodes={len(nodes)}")
        for n in nodes[:8]:
            label = n.get("text") or n.get("desc") or n.get("id") or n.get("cls")
            print(f"      #{n.get('ref', '?'):<2} {'[tap]' if n.get('tap') else '     '} "
                  f"{str(label)[:38]:<38} {n.get('xy')}")

        print("4. tap first clickable element by `ref`")
        tappable = next((n for n in nodes if n.get("tap") and n.get("ref")), None)
        if tappable:
            print("   ", tool("input.tap", session=sess, ref=tappable["ref"]))
            print("   ", tool("wait.idle", session=sess, timeout_s=10))
            print("    new foreground:", tool("app.current", session=sess))
        else:
            print("    nothing tappable on screen (lock screen? try input.key HOME first)")

        print("5. logcat tail")
        try:
            log = tool("logcat.tail", session=sess, lines=5)
            print("   ", (log.get("log") or "").replace("\n", " | ")[:300])
        except RuntimeError as e:
            print("    (skipped)", e)
        print("\nsmoke run complete")
        return 0
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# mode 2: real LLM tool-calling loop
# ---------------------------------------------------------------------------
TOOL_SPECS = [
    ("device.status", "Device health: VM/adb/boot, Android version, ABI list, screen, foreground app.", {}),
    ("observe", "One-shot perception: foreground app + compact UI tree with `ref` IDs, `xy` centers, and `screen_changed`.",
     {"only_interactive": {"type": "boolean"}, "include_screenshot": {"type": "boolean"}}),
    ("screen.capture", "Capture PNG screenshot (falls back to QEMU monitor if adb is unreachable).",
     {"source": {"type": "string", "enum": ["auto", "adb", "monitor"]}}),
    ("ui.find", "Find a UI element by text/desc/id; returns tap coordinates.",
     {"query": {"type": "string"}, "by": {"type": "string", "enum": ["any", "text", "desc", "id"]}},
     ["query"]),
    ("ui.tap_match", "Find a UI element by text/desc/id and tap its center in one step.",
     {"query": {"type": "string"}, "by": {"type": "string", "enum": ["any", "text", "desc", "id"]},
      "index": {"type": "integer"}, "wait_s": {"type": "number"}},
     ["query"]),
    ("ui.scroll_to", "Scroll inside the screen/list until a matching element appears (optionally tap it).",
     {"query": {"type": "string"}, "direction": {"type": "string", "enum": ["down", "up", "left", "right"]},
      "max_swipes": {"type": "integer"}, "tap": {"type": "boolean"}},
     ["query"]),
    ("ui.wait_for", "Block until a matching UI element appears (or disappears when gone=true).",
     {"query": {"type": "string"}, "by": {"type": "string", "enum": ["any", "text", "desc", "id"]},
      "gone": {"type": "boolean"}, "timeout_s": {"type": "number"}},
     ["query"]),
    ("input.tap", "Tap by element `ref` from observe/ui.dump OR by pixel `x` and `y`.",
     {"ref": {"type": "integer"}, "x": {"type": "integer"}, "y": {"type": "integer"}}),
    ("input.text", "Type text into a field (optionally focusing `ref`, clearing first, and submitting).",
     {"text": {"type": "string"}, "ref": {"type": "integer"},
      "clear_first": {"type": "boolean"}, "submit": {"type": "boolean"}},
     ["text"]),
    ("input.key", "Send a key: BACK, HOME, ENTER, DPAD_*, APP_SWITCH.", {"key": {"type": "string"}}, ["key"]),
    ("input.swipe", "Swipe/scroll between two points.",
     {"x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"},
      "y2": {"type": "integer"}, "ms": {"type": "integer"}}, ["x1", "y1", "x2", "y2"]),
    ("app.launch", "Launch an app by package name.", {"package": {"type": "string"}}, ["package"]),
    ("app.intent", "Open a URL, deep link, or Android Intent action.",
     {"action": {"type": "string"}, "uri": {"type": "string"}, "package": {"type": "string"}}, []),
    ("app.permissions", "Inspect or grant_all runtime permissions for a package.",
     {"package": {"type": "string"}, "action": {"type": "string", "enum": ["list", "grant_all"]}},
     ["package"]),
    ("app.list", "List installed third-party packages.", {}),
    ("device.notifications", "Read active status-bar notifications (OTPs, 2FA, push alerts) or clear them.",
     {"action": {"type": "string", "enum": ["list", "clear"]}}),
    ("action.batch", "Run multiple sequential {tool, args} steps in one round-trip.",
     {"steps": {"type": "array", "items": {"type": "object", "properties": {
         "tool": {"type": "string"}, "args": {"type": "object"}}, "required": ["tool"]}},
      "observe_after": {"type": "boolean"}},
     ["steps"]),
    ("wait.idle", "Block until the UI stops changing.", {"timeout_s": {"type": "number"}}),
    ("logcat.tail", "Recent logcat lines.", {"lines": {"type": "integer"}, "filter": {"type": "string"}}),
]


def _to_fn_name(tool_name):
    """OpenAI /chat/completions requires function names matching ^[a-zA-Z0-9_-]+$."""
    return tool_name.replace(".", "__")


def _from_fn_name(fn_name):
    return fn_name.replace("__", ".")


def openai_tools():
    out = []
    for spec in TOOL_SPECS:
        name, desc, props = spec[0], spec[1], spec[2]
        required = spec[3] if len(spec) > 3 else []
        out.append({"type": "function",
                    "function": {"name": _to_fn_name(name), "description": desc,
                                 "parameters": {"type": "object", "properties": props,
                                                "required": required}}})
    return out


def _chat_completion(base, key, payload, retries=2):
    from urllib.request import Request, urlopen
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for attempt in range(retries + 1):
        try:
            req = Request(f"{base}/chat/completions", data=body, headers=headers)
            with urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except (HTTPError, URLError) as e:
            if attempt >= retries or (isinstance(e, HTTPError) and e.code not in (429, 500, 502, 503, 504)):
                raise
            time.sleep(1.5 * (attempt + 1))


def llm(goal, model=None, max_steps=18, vision=False):
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "")
    model = model or os.environ.get("DROIDEUM_MODEL", "gpt-4o-mini")
    if not key and "localhost" not in base and "127.0.0.1" not in base:
        print("set OPENAI_API_KEY (or OPENAI_BASE_URL to a local server)", file=sys.stderr)
        return 2

    messages = [
        {"role": "system", "content":
            "You control an Android device through tools. Prefer tapping by `ref` from `observe` "
            "(e.g. input__tap({ref: 3}) or input__text({ref: 2, text: '...', submit: true})), or use "
            "ui__tap_match / ui__scroll_to. Never guess pixel coordinates. Check `screen_changed` in "
            "observe; if an action had no effect, scroll or try another approach. Stop once the goal is met."},
        {"role": "user", "content": goal},
    ]

    sess = McpSession()
    unchanged_streak = 0
    try:
        for step in range(max_steps):
            reply = _chat_completion(base, key, {
                "model": model, "messages": messages,
                "tools": openai_tools(), "tool_choice": "auto",
            })
            msg = reply["choices"][0]["message"]
            messages.append({k: msg.get(k) for k in ("role", "content", "tool_calls") if msg.get(k) is not None})

            if not msg.get("tool_calls"):
                print(f"\n[agent] {msg.get('content')}")
                return 0

            pending_images = []
            for call in msg["tool_calls"]:
                raw_name = call["function"]["name"]
                name = _from_fn_name(raw_name)
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                print(f"[step {step + 1}] {name}({json.dumps(args)[:120]})")
                try:
                    result, images = sess.call_tool(name, **args)
                    if isinstance(result, dict):
                        sc = result.get("screen_changed")
                        if sc is None and isinstance(result.get("meta"), dict):
                            sc = result["meta"].get("screen_changed")
                        if sc is False:
                            unchanged_streak += 1
                            if unchanged_streak >= 2:
                                result = dict(result)
                                result["_loop_warning"] = (
                                    f"Screen did not change for {unchanged_streak} consecutive observations. "
                                    "Try ui__scroll_to, input__key BACK, or screen__capture."
                                )
                        elif sc is True:
                            unchanged_streak = 0
                    out = json.dumps(result)
                    if vision and images:
                        pending_images.extend(images[:1])
                except RuntimeError as e:
                    out = json.dumps({"error": str(e)})
                print(f"          -> {out[:200]}")
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": out[:12000]})

            if vision and pending_images:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Attached screenshot from latest tool call:"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{pending_images[-1]}"}},
                    ],
                })
    finally:
        sess.close()

    print("step budget exhausted", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "smoke":
        sys.exit(smoke())
    if sys.argv[1] == "llm":
        rest = sys.argv[2:]
        use_vision = False
        if "--vision" in rest:
            use_vision = True
            rest = [a for a in rest if a != "--vision"]
        sys.exit(llm(" ".join(rest) or "Describe what app is in the foreground.", vision=use_vision))
    print(__doc__)
    sys.exit(2)
