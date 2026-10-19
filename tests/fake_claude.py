#!/usr/bin/env python3
"""A stand-in for `claude -p` that the tests and the demo drive, so nothing here spends real usage.

Each call takes the first line of the file in $FAKE_CLAUDE_SCRIPT (and removes it) and acts it out:
  ok:<reply>          a normal session that ends with <reply>
  limit:<reset>       "You've hit your session limit · resets <reset>"   (e.g. limit:3am)
  auth                the OAuth login expired
  transient           API Error: 500 after a couple of turns
  fail:<text>         a real failure
  edit:<file>:<text>  appends <text> to <file> in the cwd, then ok
With no script (or an empty one) every call is `ok:done`. Every call's argv is appended to $FAKE_CLAUDE_CALLS.
"""
import json
import os
import sys
import time
import uuid


def pop() -> str:
    p = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not p or not os.path.exists(p):
        return "ok:done"
    lines = open(p, encoding="utf-8").read().splitlines()
    if not lines:
        return "ok:done"
    open(p, "w", encoding="utf-8").write("\n".join(lines[1:]) + ("\n" if lines[1:] else ""))
    return lines[0]


def emit(d):
    print(json.dumps(d), flush=True)


def main():
    calls = os.environ.get("FAKE_CLAUDE_CALLS")
    prompt = sys.stdin.read()
    if calls:
        with open(calls, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt[:200], "cwd": os.getcwd()}) + "\n")
    step = pop()
    kind, _, arg = step.partition(":")
    sid = "--resume" in sys.argv and sys.argv[sys.argv.index("--resume") + 1] or str(uuid.uuid4())
    delay = float(os.environ.get("FAKE_CLAUDE_DELAY", "0"))
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "fake"})
    if kind == "auth":
        print("Failed to authenticate: OAuth session expired and could not be refreshed", flush=True)
        sys.exit(1)
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}})
    time.sleep(delay)
    if kind == "edit":
        fname, _, text = arg.partition(":")
        with open(fname, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        kind, arg = "ok", text
    if kind == "ok":
        emit({"type": "result", "subtype": "success", "is_error": False, "result": arg or "done", "session_id": sid,
              "num_turns": 6, "total_cost_usd": 0.42})
        return 0
    if kind == "limit":
        emit({"type": "result", "subtype": "success", "is_error": True, "session_id": sid, "num_turns": 3,
              "result": f"You've hit your session limit · resets {arg or '3am'}", "total_cost_usd": 0.31,
              "api_error_status": 429})
        return 1
    if kind == "transient":
        emit({"type": "result", "subtype": "success", "is_error": True, "session_id": sid, "num_turns": 4,
              "result": "API Error: 500 Internal server error", "total_cost_usd": 0.12, "api_error_status": 500})
        return 1
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": sid, "num_turns": 9,
          "result": arg or "the tests still fail", "total_cost_usd": 0.6})
    return 1


if __name__ == "__main__":
    sys.exit(main())
