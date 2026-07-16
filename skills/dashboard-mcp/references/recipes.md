# Orchestration recipes

Worked patterns for driving other agent sessions via `mcp__dashboard__*`.
All verified against the live box.

## 1. Delegate one task and collect the answer (headless)

```
id = create_session(cwd="~/Areas/Dom")["id"]
start_session(id)
r = send_and_wait(id, "Summarise today's heating schedule in 3 bullets.")
# r.text == the assistant's reply
stop_session(id)
```

`send_and_wait` sends, then long-polls `/wait` until the turn lands, handling
`busy`, 429 back-off and `timeout` re-polls. Use it whenever the child won't need
to ask you anything.

## 2. Delegate to an interactive child and answer its question

When the child may call `AskUserQuestion`, run it on the tmux runner and watch the
pane (there is **no** HTTP way to answer — keystrokes into `hd-<id>` are the only
mechanism):

```
send = send_message(id, "<task that asks a question>", interactive_mode=true)
since = send.expected_turn_idx
# poll until the menu is STABLE (don't answer a mid-render pane)
loop:
    sleep 2
    p = capture_pane(id)
    if p.pending_question.likely_waiting and <your option label in p.pane>:
        # require it twice in a row before answering
answer_question(id, mode="option_index", option_index=K)   # 0-based visible order
# then collect:
loop wait_for_reply(id, since_turn=since) until status=="done"
```

Tips:
- `option_index` is the **visible 0-based** position (`Down`×K then `Enter`, with a
  ~0.35 s gap so the menu registers each move). Option "1." → index 0, "2." → 1, …
- For the free-text field, pick "Type something" then `answer_question(mode="text", text=...)`.
- **Verify the answer took.** A single keystroke can be dropped under load — poll the
  expected side-effect (a file, a status, the pane changing) and re-`answer_question`
  if the same menu is still up. The self-test does exactly this.
- A permission prompt ("Do you want to proceed? 1. Yes 2. No") is answered the same
  way with `answer_question`/`send_keys`.

## 3. Fan out to several agents and gather

```
ids = [ create_session(cwd=a)["id"] for a in agent_dirs ]
for i in ids: start_session(i)
for i in ids: send_message(i, task_for[i])        # fire all, don't block
results = { i: wait_for_reply(i, since_turn=exp[i]) for i in ids }  # collect
```

Each session is independent; there is no cross-session lock, so you can run many
in parallel (mind the tmux pool size — see `capabilities`/`pool`).

## 4. Find prior context before delegating

```
hits = search_sessions("heating schedule winter")   # hybrid BM25+vector
# reuse the best session instead of starting cold:
send_and_wait(hits[0].session_id, "Continue: …")
```

## 5. Interrupt a child that's off the rails

`cancel_turn` only stops the dashboard's SSE bookkeeping — it does NOT stop the
claude process in the pane. To truly interrupt, send Escape (claude's
"esc to interrupt"):

```
send_keys(id, keys=["Escape"])      # interrupt the in-flight turn
# or, to fully reclaim resources:
stop_session(id)                    # cancels + tears down the tmux slot, keeps transcript
```

## 6. Recursive delegation (a child sub-orchestrates)

The MCP is currently scoped to the **Global agent only**, so a child you spawn at
another cwd does NOT have `mcp__dashboard__*` natively. It can still
sub-orchestrate via the localhost HTTP API (curl) — hand it a managerial task:

> "You are a sub-orchestrator. Using the orchestrator HTTP API at
>  http://localhost:8766 (curl), create a session at ~/Areas/Praca,
>  send it 'draft X', collect its reply, then report it back to me."

This is the verified "orchestrate through other agents in the user's name" loop
(`scripts/recursion_demo.py`: Global → manager child → worker grandchild → 42).

**Key gotcha — keep each sub-orchestrator turn's shell calls SHORT.** The
dashboard reaps an interactive turn when the pane idles, and a long-blocking
`/wait` curl inside a turn reads as idle → premature turn end. Split the work:
one turn creates+delegates to the grandchild (quick), the parent does the slow
long-poll, a second turn collects+tears down. Always `stop_session` grandchildren
to avoid leaking tmux pool slots.

To give another agent the MCP natively later: add its cwd to `AGENT_CWDS` in
`bundled_mcp.AGENT_CWDS` and add the skill to that agent's allowlist.

## Gotchas (live-verified)

- `turn_idx` is a positional cursor, not a key — re-read it, don't cache.
- Busy is HTTP 200 `{ok:false,status:"busy"}` — branch on the body.
- Assistant prose is block kind `text` (not `markdown`).
- `model` is `opus|sonnet|haiku` only (not full ids).
- `cwd` must already exist under `$HOME` (create the dir first) or you get 400.
- Don't create raw tmux sessions on the `hd-orch` socket — the dashboard's
  `recover_orphans` kills untracked ones on restart. Always go through
  `create_session`/`start_session`.
