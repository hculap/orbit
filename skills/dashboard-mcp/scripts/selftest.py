#!/usr/bin/env python3
"""
selftest — drive the orchestrator MCP server end-to-end against a REAL throwaway
child session, proving the full delegation loop:

  create_session -> start_session -> send_message(interactive)
   -> detect the child's AskUserQuestion via capture_pane
   -> answer_question (pick a NON-default option)
   -> wait_for_reply / read result -> verify side-effect -> stop+delete

Run:  python3 selftest.py
Exit 0 = PASS. Leaves nothing behind (deletes the test session + scratch file).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "dashboard_mcp.py"
WORKER_DIR = Path.home() / ".orchestrator/scratch/orch-selftest"
ANSWER_FILE = WORKER_DIR / "choice.txt"


class Client:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                       "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        return json.loads(line)

    def tool(self, name, args=None):
        r = self.call("tools/call", {"name": name, "arguments": args or {}})
        if "error" in r:
            raise RuntimeError(f"{name} rpc error: {r['error']}")
        return json.loads(r["result"]["content"][0]["text"])

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def main():
    WORKER_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_FILE.unlink(missing_ok=True)
    c = Client()
    sid = None
    try:
        init = c.call("initialize", {"protocolVersion": "2024-11-05"})
        print("init:", init["result"]["serverInfo"])

        caps = c.tool("capabilities")
        assert caps.get("ok"), "capabilities not ok"
        print("capabilities ok, version", caps.get("version"))

        sess = c.tool("create_session", {"cwd": str(WORKER_DIR), "model": "sonnet",
                                          "title": "ORCH selftest (delete me)"})
        sid = sess["id"]
        print("created session", sid)

        started = c.tool("start_session", {"session_id": sid})
        print("start:", started)

        task = (f"Use the AskUserQuestion tool to ask me exactly one question with two options "
                f"labelled APPLE and CHERRY. After I choose, write my chosen word (uppercase) to "
                f"the file {ANSWER_FILE} and then reply with the single word DONE.")
        snd = c.tool("send_message", {"session_id": sid, "text": task, "interactive_mode": True})
        print("send -> expected_turn_idx", snd.get("expected_turn_idx"))
        since = snd.get("expected_turn_idx", -1)

        # --- detect the pending question (require a STABLE menu) ---
        print("polling pane for the question menu...")
        import re
        opt_apple = re.compile(r"\b1\.\s*APPLE", re.I)
        opt_cherry = re.compile(r"\b2\.\s*CHERRY", re.I)
        answered = False
        stable = 0
        for i in range(40):
            time.sleep(2)
            pane = c.tool("capture_pane", {"session_id": sid})
            txt = pane.get("pane", "")
            pq = pane.get("pending_question", {})
            menu_here = pq.get("likely_waiting") and opt_apple.search(txt) and opt_cherry.search(txt)
            stable = stable + 1 if menu_here else 0
            if stable >= 2:  # two consecutive confirmations → really rendered
                print(f"  stable question menu at iter {i}; signals={pq.get('signals')}")
                # CHERRY is the 2nd visible option → index 1 (0-based) → Down + Enter
                res = c.tool("answer_question", {"session_id": sid,
                                                 "mode": "option_index", "option_index": 1})
                print("  answered (picked CHERRY); after-pane tail:")
                print("   " + "\n   ".join(res["after"].splitlines()[-4:]))
                answered = True
                break
        assert answered, "never detected/answered the question"

        # --- collect: poll the side-effect file; re-answer if the menu lingers ---
        # A real orchestrator verifies the answer was consumed and retries if the
        # menu is still up (a single Down/Enter can be dropped under load).
        print("waiting for the child to act on the answer...")
        got = None
        for j in range(40):
            time.sleep(2)
            if ANSWER_FILE.exists():
                got = ANSWER_FILE.read_text().strip()
                break
            pane = c.tool("capture_pane", {"session_id": sid})
            if pane.get("pending_question", {}).get("likely_waiting") and "CHERRY" in pane.get("pane", ""):
                print(f"  menu still up at iter {j}; re-answering CHERRY")
                c.tool("answer_question", {"session_id": sid, "mode": "option_index", "option_index": 1})
        # surface the assistant's final text too (best-effort)
        msgs = c.tool("get_messages", {"session_id": sid, "limit": 3})
        text = "\n".join(b.get("text") or b.get("markdown") or ""
                         for m in msgs.get("messages", []) if m.get("role") == "assistant"
                         for b in m.get("blocks", []) if b.get("kind") in ("text", "markdown"))
        print("assistant reply:", (text or "(none)")[:200])

        print(f"side-effect file = [{got}]  (expected CHERRY)")
        assert got == "CHERRY", f"expected CHERRY, got {got!r}"
        print("\n*** PASS — full delegate→ask→answer→collect→verify loop works ***")
        return 0
    finally:
        if sid:
            try:
                c.tool("stop_session", {"session_id": sid})
                c.tool("delete_session", {"session_id": sid})
                print("cleaned up session", sid)
            except Exception as e:
                print("cleanup warning:", e)
        ANSWER_FILE.unlink(missing_ok=True)
        c.close()


if __name__ == "__main__":
    sys.exit(main())
