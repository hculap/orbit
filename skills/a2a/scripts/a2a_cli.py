#!/usr/bin/env python3
"""
a2a_cli.py — argparse CLI for the `a2a` (agent-to-agent) skill.

A thin wrapper over a2a_lib. Claude runs this inside its tmux shell to talk to
OTHER Claude Code agents on the same host: send a message (a PURE ENQUEUE into
the target's maildir — no revive, no push; the target's human drains it later),
discover who is around + where they live, and drain its OWN inbox.

Usage:
    python3 ~/.orchestrator/skills-registry/a2a/scripts/a2a_cli.py <command> [opts]

Commands:
    list  [--json]            (each agent: lib_id, warm/cold, name, description,
                               PARA dir, and every session's title + transcript)
    whois <lib_id> [--json]   (one agent's full identity summary, PARA dir, and
                               ALL sessions incl. transcript .jsonl paths)
    send  --to <lib_id|global> [--type message|reply] [--correlation-id ID]
          [--reply-to LIB] [--session <uuid>] (<text> | --stdin)
          (pure enqueue into the target's maildir; --session routes into a
           specific session sub-maildir. The human drains via `inbox --drain`.)
    inbox [--drain] [--json]  (covers the agent inbox AND this session's inbox)
    read  <id>                (searches both inboxes)

Env discovery (see a2a_lib): HD_SESSION_ID / ORCHESTRATOR_SESSION_ID / tmux for
the session id; HD_LIB_ID for THIS agent's PARA library; HD_NOTIFY_URL /
config.json for the dashboard URL; HD_ARTIFACT_TOKEN_FILE / ~/.orchestrator/
artifact_token for the X-A2A-Token auth token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make the importable lib resolvable whether run as a script or module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import a2a_lib as lib  # noqa: E402


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    agents = lib.http_get_agents()
    if args.json_out:
        print(json.dumps(agents, indent=2, ensure_ascii=False, default=str))
        return 0
    if not agents:
        print("No agents found.")
        return 0
    for a in agents:
        lib_id = str(a.get("lib_id", "?"))
        state = "warm" if a.get("warm") else "cold"
        name = str(a.get("name", "") or "")
        print(f"{lib_id}  [{state}]  {name}")
        directory = a.get("dir")
        if isinstance(directory, str) and directory:
            print(f"  dir: {directory}")
        description = a.get("description")
        if isinstance(description, str) and description.strip():
            print(f"  {description.strip()}")
        # Every LIVE session: its title, id (for `send --session <sid>`), and
        # transcript path (so a peer can read it directly).
        sessions = a.get("sessions")
        if isinstance(sessions, list):
            for s in sessions:
                if not isinstance(s, dict):
                    continue
                sid = s.get("session_id")
                if not isinstance(sid, str) or not sid:
                    continue
                title = str(s.get("title") or "").strip() or "(untitled)"
                print(f"  ↳ {title} — {sid}")
                transcript = s.get("transcript")
                if isinstance(transcript, str) and transcript:
                    print(f"      transcript: {transcript}")
    return 0


# ---------------------------------------------------------------------------
# whois
# ---------------------------------------------------------------------------


def _cmd_whois(args: argparse.Namespace) -> int:
    agent = lib.http_get_whois(args.lib_id)
    if args.json_out:
        print(json.dumps(agent, indent=2, ensure_ascii=False, default=str))
        return 0
    if not agent:
        print(f"No agent record for {args.lib_id}.")
        return 0
    lib_id = str(agent.get("lib_id", args.lib_id))
    name = str(agent.get("name", "") or "")
    state = "warm" if agent.get("warm") else "cold"
    print(f"{lib_id}  [{state}]  {name}")
    directory = agent.get("dir")
    if isinstance(directory, str) and directory:
        print(f"dir: {directory}")
    identity = agent.get("identity")
    if isinstance(identity, str) and identity.strip():
        print("\nidentity:")
        print(identity.strip())
    sessions = agent.get("sessions")
    if isinstance(sessions, list) and sessions:
        print(f"\nsessions ({len(sessions)}):")
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sid = s.get("session_id")
            if not isinstance(sid, str) or not sid:
                continue
            title = str(s.get("title") or "").strip() or "(untitled)"
            live = "live" if s.get("live") else "cold"
            print(f"  ↳ [{live}] {title} — {sid}")
            transcript = s.get("transcript")
            if isinstance(transcript, str) and transcript:
                print(f"      transcript: {transcript}")
    else:
        print("\nNo sessions yet (cold agent).")
    return 0


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def _resolve_send_text(args: argparse.Namespace) -> str:
    if args.stdin:
        if args.text is not None:
            raise lib.A2AError("provide text via --stdin OR a positional arg, not both")
        return sys.stdin.read()
    if args.text is None:
        raise lib.A2AError("no message text (pass a positional <text> or --stdin)")
    return args.text


def _cmd_send(args: argparse.Namespace) -> int:
    text = _resolve_send_text(args)
    resp = lib.http_post_send(
        to=args.to,
        text=text,
        msg_type=args.type,
        correlation_id=args.correlation_id,
        reply_to=args.reply_to,
        session_id=lib.resolve_session_id(),
        session=args.session,
    )
    msg_id = resp.get("id", "?")
    delivery = resp.get("delivery", "?")
    target = args.to
    if args.session:
        target += f" · session={args.session}"
    print(f"sent · id={msg_id} · to={target} · delivery={delivery}")
    return 0


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


def _print_envelope(env: dict, *, full: bool) -> None:
    msg_id = env.get("id") or env.get("_id", "?")
    sender = env.get("from", "?")
    mtype = env.get("type", "message")
    ts = env.get("ts", "")
    payload = env.get("payload") or {}
    text = payload.get("text", "") if isinstance(payload, dict) else ""
    header = f"[{msg_id}] from={sender} type={mtype}"
    if ts:
        header += f" ts={ts}"
    corr = env.get("correlation_id")
    if corr:
        header += f" corr={corr}"
    print(header)
    if full:
        print(text)
        print("---")
    else:
        # One-line preview for a non-drain listing.
        preview = text.replace("\n", " ")
        if len(preview) > 100:
            preview = preview[:99] + "…"
        print(f"  {preview}")


def _cmd_inbox(args: argparse.Namespace) -> int:
    if args.drain:
        envelopes = lib.drain_inbox()
    else:
        envelopes = lib.list_inbox()
    if args.json_out:
        clean = [{k: v for k, v in e.items() if not k.startswith("_")} for e in envelopes]
        print(json.dumps(clean, indent=2, ensure_ascii=False, default=str))
        return 0
    if not envelopes:
        print(f"Inbox empty for {lib.this_agent_label()}.")
        return 0
    for env in envelopes:
        _print_envelope(env, full=args.drain)
    if not args.drain:
        print(f"\n{len(envelopes)} message(s). Use `inbox --drain` to read + clear.")
    return 0


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def _cmd_read(args: argparse.Namespace) -> int:
    env = lib.read_message(args.id)
    _print_envelope(env, full=True)
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a2a",
        description="Agent-to-agent messaging between Claude Code agents on this host.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser(
        "list",
        help="List known agents (warm/cold) + their PARA dir + sessions",
    )
    p_list.add_argument("--json", dest="json_out", action="store_true",
                        help="Dump the agent roster as JSON")
    p_list.set_defaults(func=_cmd_list)

    p_whois = sub.add_parser(
        "whois",
        help="Full record for ONE agent: identity, PARA dir, all sessions",
    )
    p_whois.add_argument("lib_id", help="Target lib_id (e.g. areas/Dom) or 'global'")
    p_whois.add_argument("--json", dest="json_out", action="store_true",
                         help="Dump the agent record as JSON")
    p_whois.set_defaults(func=_cmd_whois)

    p_send = sub.add_parser("send", help="Send a message to another agent")
    p_send.add_argument("--to", required=True,
                        help="Target lib_id (e.g. areas/Dom, projects/foo) or 'global'")
    p_send.add_argument("--type", choices=list(lib.MESSAGE_TYPES), default="message",
                        help="Message type (default: message)")
    p_send.add_argument("--correlation-id", dest="correlation_id",
                        help="Correlate this message with an earlier one (its id)")
    p_send.add_argument("--reply-to", dest="reply_to",
                        help="lib_id the recipient should reply to (default: you)")
    p_send.add_argument("--session", dest="session", default=None,
                        help="Route into a SPECIFIC session's sub-maildir (uuid) "
                             "of --to instead of the agent-level inbox. Pure "
                             "enqueue either way (delivery=enqueued). Use "
                             "`a2a list` / `a2a whois` to discover session ids.")
    p_send.add_argument("--stdin", action="store_true",
                        help="Read the message text from stdin")
    p_send.add_argument("text", nargs="?", help="Message text (or use --stdin)")
    p_send.set_defaults(func=_cmd_send)

    p_inbox = sub.add_parser("inbox", help="List THIS agent's inbox (oldest-first)")
    p_inbox.add_argument("--drain", action="store_true",
                         help="Print each message in full and move it to cur/ (mark read)")
    p_inbox.add_argument("--json", dest="json_out", action="store_true",
                         help="Dump the inbox as JSON")
    p_inbox.set_defaults(func=_cmd_inbox)

    p_read = sub.add_parser("read", help="Read one message by id (inbox or cur)")
    p_read.add_argument("id")
    p_read.set_defaults(func=_cmd_read)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        rc = args.func(args)
    except lib.A2AError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `| head`); exit quietly.
        try:
            sys.stdout.close()
        except OSError:
            pass
        os._exit(0)
    sys.exit(rc)


if __name__ == "__main__":
    main()
