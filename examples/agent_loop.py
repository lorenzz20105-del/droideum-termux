#!/usr/bin/env python3
"""
A dependency-free agent loop for droideum.

Two modes:

  # 1. No LLM at all — prove the control plane works:
  python3 examples/agent_loop.py smoke

  # 2. Hand the same tools to any OpenAI-compatible model
  #    (any local/remote endpoint that implements /chat/completions with tools):
  export OPENAI_API_KEY=...            # or OPENAI_BASE_URL=http://localhost:8080/v1
  python3 examples/agent_loop.py llm "Open Settings and tell me the Android version"

It talks to the same tools the MCP server exposes — here over the one-shot CLI
(`droideum agent call`), which is all you need if your agent is a script rather
than an MCP client.
"""

import json
import os
import subprocess
import sys
import time

DROIDEUM = os.environ.get("DROIDEUM_BIN", "droideum")


def tool(tool_name, **args):
    """Call one droideum tool. Raises on tool errors, with the server's hint."""
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
    print("1. device.status")
    st = tool("device.status")
    print("   ", json.dumps({k: st.get(k) for k in
                             ("vm_running", "adb", "boot_completed", "android", "screen")}))
    if not st.get("adb"):
        print("    device not reachable — run `droideum up` first")
        return 1

    print("2. task.checkpoint before-task")
    try:
        print("   ", tool("task.checkpoint", name="before-task"))
    except RuntimeError as e:
        print("    (skipped)", e)

    print("3. observe (interactive nodes only)")
    obs = tool("observe", only_interactive=True)
    fg = (obs.get("meta") or {}).get("foreground")
    nodes = (obs.get("ui") or {}).get("nodes", [])
    print(f"    foreground={fg} nodes={len(nodes)}")
    for n in nodes[:8]:
        label = n.get("text") or n.get("desc") or n.get("id") or n.get("cls")
        print(f"      {'[tap]' if n.get('tap') else '     '} {str(label)[:40]:<40} {n.get('xy')}")

    print("4. find something clickable and press it")
    label = None
    for n in nodes:
        if n.get("tap") and (n.get("text") or n.get("desc")):
            label = n.get("text") or n.get("desc")
            break
    if label:
        hit = tool("ui.find", query=label)["matches"][0]
        print(f"    '{label}' -> {hit['center']}")
        tool("input.tap", x=hit["center"][0], y=hit["center"][1])
        print("    tap sent; waiting for the UI to settle")
        print("   ", tool("wait.idle", timeout_s=10))
        print("    new foreground:", tool("app.current"))
    else:
        print("    nothing tappable on screen (lock screen? try input.key HOME first)")

    print("5. logcat tail")
    try:
        log = tool("logcat.tail", lines=5)
        print("   ", (log.get("log") or "").replace("\n", " | ")[:300])
    except RuntimeError as e:
        print("    (skipped)", e)
    print("\nsmoke run complete")
    return 0


# ---------------------------------------------------------------------------
# mode 2: real LLM tool-calling loop
# ---------------------------------------------------------------------------
TOOL_SPECS = [
    ("device.status", "Device health: VM/adb/boot, Android version, ABI list, screen, foreground app.", {}),
    ("observe", "One-shot perception: foreground app + compact UI tree with tap coordinates.",
     {"only_interactive": {"type": "boolean"}, "include_screenshot": {"type": "boolean"}}),
    ("ui.find", "Find a UI element by text/desc/id; returns tap coordinates.",
     {"query": {"type": "string"}, "by": {"type": "string", "enum": ["any", "text", "desc", "id"]}},
     ["query"]),
    ("input.tap", "Tap device pixel coordinates.", {"x": {"type": "integer"}, "y": {"type": "integer"}},
     ["x", "y"]),
    ("input.text", "Type ASCII text into the focused field.", {"text": {"type": "string"}}, ["text"]),
    ("input.key", "Send a key: BACK, HOME, ENTER, DPAD_*, APP_SWITCH.", {"key": {"type": "string"}}, ["key"]),
    ("input.swipe", "Swipe/scroll between two points.",
     {"x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"},
      "y2": {"type": "integer"}, "ms": {"type": "integer"}}, ["x1", "y1", "x2", "y2"]),
    ("app.launch", "Launch an app by package name.", {"package": {"type": "string"}}, ["package"]),
    ("app.list", "List installed third-party packages.", {}),
    ("wait.idle", "Block until the UI stops changing.", {"timeout_s": {"type": "number"}}),
    ("logcat.tail", "Recent logcat lines.", {"lines": {"type": "integer"}, "filter": {"type": "string"}}),
]


def openai_tools():
    out = []
    for spec in TOOL_SPECS:
        name, desc, props = spec[0], spec[1], spec[2]
        required = spec[3] if len(spec) > 3 else []
        out.append({"type": "function",
                    "function": {"name": name, "description": desc,
                                 "parameters": {"type": "object", "properties": props,
                                                "required": required}}})
    return out


def llm(goal, model=None, max_steps=15):
    try:
        from urllib.request import Request, urlopen
    except ImportError:
        print("no urllib?", file=sys.stderr)
        return 2
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "")
    model = model or os.environ.get("DROIDEUM_MODEL", "gpt-4o-mini")
    if not key and "localhost" not in base and "127.0.0.1" not in base:
        print("set OPENAI_API_KEY (or OPENAI_BASE_URL to a local server)", file=sys.stderr)
        return 2

    messages = [
        {"role": "system", "content":
            "You control an Android device through tools. Coordinates are device pixels and must come "
            "from observe/ui.find — never guess. After an action, call wait.idle then observe to "
            "verify. Be concise; stop as soon as the goal's success condition is visible."},
        {"role": "user", "content": goal},
    ]

    for step in range(max_steps):
        body = json.dumps({"model": model, "messages": messages, "tools": openai_tools(),
                           "tool_choice": "auto"}).encode()
        req = Request(f"{base}/chat/completions", data=body,
                      headers={"Content-Type": "application/json",
                               **({"Authorization": f"Bearer {key}"} if key else {})})
        with urlopen(req, timeout=180) as r:
            reply = json.loads(r.read())
        msg = reply["choices"][0]["message"]
        messages.append({k: msg.get(k) for k in ("role", "content", "tool_calls") if msg.get(k) is not None})

        if not msg.get("tool_calls"):
            print(f"\n[agent] {msg.get('content')}")
            return 0

        for call in msg["tool_calls"]:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"[step {step + 1}] {name}({json.dumps(args)[:120]})")
            try:
                result = tool(name, **args)
                out = json.dumps(result)
            except RuntimeError as e:
                out = json.dumps({"error": str(e)})
            print(f"          -> {out[:200]}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out[:12000]})

    print("step budget exhausted", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "smoke":
        sys.exit(smoke())
    if sys.argv[1] == "llm":
        sys.exit(llm(" ".join(sys.argv[2:]) or "Describe what app is in the foreground."))
    print(__doc__)
    sys.exit(2)
