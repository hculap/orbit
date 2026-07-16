#!/usr/bin/env python3
"""
recursion_demo — prove RECURSIVE delegation: this driver spawns a MANAGER child
and the manager itself orchestrates a WORKER grandchild via the localhost
orchestrator HTTP API. That is the "orchestrate through other agents in the
user's name" loop, one level deep.

Design note: the dashboard reaps an interactive turn when the pane goes idle, and
a long-blocking Bash call (a 20s `/wait` curl) reads as idle. So we keep every
manager turn's shell calls SHORT and split the work across two manager turns:
  turn 1 — manager creates+starts+messages the worker, reports {wid,e}  (quick)
  driver — does the slow long-poll on the worker itself, gets the answer
  turn 2 — manager reads the worker's transcript, reports it, tears it down (quick)

Run:  python3 recursion_demo.py   → exit 0 on PASS.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "dashboard_mcp.py"
MGR_DIR = Path.home() / ".orchestrator/scratch/orch-manager"
WRK_DIR = Path.home() / ".orchestrator/scratch/orch-worker"
B = "http://localhost:8766/api/orchestrator"


class Client:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, str(SERVER)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                       "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def tool(self, name, args=None):
        r = self.call("tools/call", {"name": name, "arguments": args or {}})
        if "error" in r:
            raise RuntimeError(f"{name}: {r['error']}")
        return json.loads(r["result"]["content"][0]["text"])

    def close(self):
        try:
            self.p.stdin.close(); self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


TURN1 = f"""You are a SUB-ORCHESTRATOR acting for the user. EXECUTE the following with the Bash tool
NOW, one command at a time — actually run them, do NOT print or summarise a script, do NOT
explain. Use only these SHORT curl calls (nothing long-running):

Step 1 — create the worker and capture its id:
  curl -sf -X POST {B}/sessions -H 'Content-Type: application/json' -d '{{"title":"recursion worker (del)","cwd":"{WRK_DIR}","model":"sonnet"}}'
  (read the "id" field from the JSON it returns — that is the worker id)
Step 2 — start it:
  curl -sf -X POST {B}/sessions/<id>/start
Step 3 — give it a task:
  curl -sf -X POST {B}/sessions/<id>/messages -H 'Content-Type: application/json' -d '{{"text":"Reply with ONLY the result of 6 multiplied by 7 as a bare number."}}'

After you have actually run all three, reply with ONLY one line and nothing else:
{{"wid":"<the real worker id you got in step 1>"}}"""

TURN2 = f"""EXECUTE these with the Bash tool NOW (actually run them, do not print a script). The worker
id is {{wid}}. Use only SHORT curl calls:
Step 1 — read the worker's reply:  curl -sf "{B}/sessions/{{wid}}/messages?limit=4"
  (the worker's answer is the assistant message's text — a number)
Step 2 — tear the worker down:  curl -sf -X POST {B}/sessions/{{wid}}/stop && curl -sf -X DELETE {B}/sessions/{{wid}}
After running them, reply with ONLY this line:  WORKER SAID <the number the worker replied>"""


def main():
    MGR_DIR.mkdir(parents=True, exist_ok=True)
    WRK_DIR.mkdir(parents=True, exist_ok=True)
    c = Client()
    mid = None
    wid = None
    try:
        c.call("initialize", {"protocolVersion": "2024-11-05"})
        mid = c.tool("create_session", {"cwd": str(MGR_DIR), "model": "sonnet",
                                        "title": "recursion manager (del)"})["id"]
        print("manager:", mid)
        c.tool("start_session", {"session_id": mid})

        # --- turn 1: manager spawns + delegates to the worker ---
        print("turn 1: manager spawns + delegates to a worker...")
        r1 = c.tool("send_and_wait", {"session_id": mid, "text": TURN1,
                                      "interactive_mode": True, "overall_timeout": 240})
        if not r1.get("text"):
            mm = c.tool("get_messages", {"session_id": mid, "limit": 6})
            r1["text"] = "\n".join(b.get("text", "") for m in mm.get("messages", [])
                                   if m.get("role") == "assistant"
                                   for b in m.get("blocks", []) if b.get("kind") == "text")
        print("  manager reply:", r1["text"][-200:])
        m = re.search(r'"wid"\s*:\s*"([0-9a-f-]{36})"', r1["text"]) or re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', r1["text"])
        assert m, "manager did not report a worker id"
        wid = m.group(1)
        print("  -> manager spawned worker grandchild:", wid)

        # --- driver does the slow wait on the worker, so the manager turn stays short ---
        print("driver waits for the worker to answer...")
        ans = ""
        for _ in range(10):
            w = c.tool("wait_for_reply", {"session_id": wid, "since_turn": -1, "timeout": 20})
            if w.get("status") == "done":
                ans = "\n".join(b.get("text") or b.get("markdown") or ""
                                for mm in w.get("new_messages", []) if mm.get("role") == "assistant"
                                for b in mm.get("blocks", []) if b.get("kind") in ("text", "markdown"))
                if ans.strip():
                    break
        print("  worker answered:", repr(ans.strip()[:60]))

        # --- turn 2: manager collects the worker's answer + tears it down ---
        print("turn 2: manager collects the worker's answer + cleans it up...")
        r2 = c.tool("send_and_wait", {"session_id": mid, "text": TURN2.format(wid=wid),
                                      "interactive_mode": True, "overall_timeout": 240})
        if not r2.get("text"):
            mm = c.tool("get_messages", {"session_id": mid, "limit": 6})
            r2["text"] = "\n".join(b.get("text", "") for m in mm.get("messages", [])
                                   if m.get("role") == "assistant"
                                   for b in m.get("blocks", []) if b.get("kind") == "text")
        report = r2["text"]
        print("\n--- manager's final report ---")
        print(report[-300:])
        print("------------------------------")
        wid = None if "42" in report else wid  # worker torn down by manager on success
        if "42" in report and "WORKER SAID" in report.upper():
            print("\n*** PASS — recursive loop: driver → manager → worker → back, answer 42 ***")
            return 0
        print("\n*** FAIL ***")
        return 1
    finally:
        # belt-and-suspenders cleanup
        for sid in (wid, mid):
            if sid:
                try:
                    c.tool("stop_session", {"session_id": sid})
                    c.tool("delete_session", {"session_id": sid})
                    print("cleaned", sid)
                except Exception:
                    pass
        c.close()


if __name__ == "__main__":
    sys.exit(main())
