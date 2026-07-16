#!/usr/bin/env python3
"""
artifacts_cli.py — argparse CLI for the `artifact` skill.

A thin wrapper over artifacts_lib. Claude runs this inside its tmux shell to
create rich, persistent artifacts (charts / maps / youtube / images / audio /
video / interactive HTML / files) that the orbit surfaces as a
toast / modal + gallery.

Usage:
    python3 ~/.orchestrator/skills-registry/artifacts/scripts/artifacts_cli.py <command> [opts]

Commands:
    create --type <t> --title <s> [--open] [--mime <m>] [<file>|--spec-file <f>|--stdin]
    open   <id>
    list   [--session|--agent] [--json]
    dup    <id>
    edit   <id> [--title <s>] [--type <t>]
    delete <id>

Env discovery (see artifacts_lib): HD_SESSION_ID / ORCHESTRATOR_SESSION_ID /
tmux for the session id; HD_LIB_ID for the PARA library; HD_NOTIFY_URL /
config.json for the dashboard URL; HD_ARTIFACT_TOKEN_FILE / ~/.orchestrator/
artifact_token for the notify auth token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make the importable lib resolvable whether run as a script or module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts_lib as lib  # noqa: E402


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _cmd_create(args: argparse.Namespace) -> int:
    result = lib.create(
        art_type=args.type,
        title=args.title,
        file=args.file,
        spec_file=args.spec_file,
        use_stdin=args.stdin,
        mime=args.mime,
        open_after=args.open,
    )
    manifest = result["manifest"]
    print(
        f"saved · id={manifest['id']} · type={manifest['type']} · "
        f"pushed={_yn(result['pushed'])}"
    )
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    result = lib.open_artifact(args.id)
    print(f"opened · id={args.id} · pushed={_yn(result['pushed'])}")
    return 0


def _print_table(manifests: list[dict]) -> None:
    if not manifests:
        print("No artifacts found.")
        return
    for m in manifests:
        parts = [
            m.get("id", "?"),
            f"[{m.get('type', '?')}]",
            m.get("title", ""),
        ]
        lib_id = m.get("lib_id")
        if lib_id:
            parts.append(f"lib:{lib_id}")
        src = m.get("src")
        if src:
            parts.append(f"src:{src}")
        created = m.get("created_at", "")
        if created:
            parts.append(created)
        print("  ".join(str(p) for p in parts))


def _cmd_list(args: argparse.Namespace) -> int:
    manifests = lib.list_artifacts(session_only=args.session)
    if args.json_out:
        print(json.dumps(manifests, indent=2, ensure_ascii=False, default=str))
    else:
        _print_table(manifests)
    return 0


def _cmd_dup(args: argparse.Namespace) -> int:
    result = lib.duplicate(args.id)
    print(f"duplicated · id={result['manifest']['id']}")
    return 0


def _cmd_edit(args: argparse.Namespace) -> int:
    lib.edit(args.id, title=args.title, art_type=args.type)
    print(f"edited · id={args.id}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    lib.delete(args.id)
    print(f"deleted · id={args.id}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artifact",
        description="Create rich, persistent artifacts surfaced in the dashboard.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a new artifact")
    p_create.add_argument("--type", required=True, choices=list(lib.ARTIFACT_TYPES))
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--open", action="store_true",
                          help="Pop a modal in the browser (else: toast + gallery only)")
    p_create.add_argument("--mime", help="Explicit MIME type for file-backed artifacts")
    p_create.add_argument("--spec-file", dest="spec_file",
                          help="Path to a JSON spec (chart/map/youtube) or raw HTML")
    p_create.add_argument("--stdin", action="store_true",
                          help="Read the spec / HTML from stdin")
    p_create.add_argument("file", nargs="?",
                          help="Source file (image/audio/video/file) OR inline spec/HTML")
    p_create.set_defaults(func=_cmd_create)

    p_open = sub.add_parser("open", help="Re-open an existing artifact (pops the modal)")
    p_open.add_argument("id")
    p_open.set_defaults(func=_cmd_open)

    p_list = sub.add_parser("list", help="List artifacts in the current dir")
    scope = p_list.add_mutually_exclusive_group()
    scope.add_argument("--session", action="store_true",
                       help="Only artifacts from the current session")
    scope.add_argument("--agent", action="store_true",
                       help="All artifacts in the dir (default)")
    p_list.add_argument("--json", dest="json_out", action="store_true",
                        help="Dump the manifest list as JSON")
    p_list.set_defaults(func=_cmd_list)

    p_dup = sub.add_parser("dup", help="Duplicate an artifact")
    p_dup.add_argument("id")
    p_dup.set_defaults(func=_cmd_dup)

    p_edit = sub.add_parser("edit", help="Edit an artifact's title / type")
    p_edit.add_argument("id")
    p_edit.add_argument("--title")
    p_edit.add_argument("--type", choices=list(lib.ARTIFACT_TYPES))
    p_edit.set_defaults(func=_cmd_edit)

    p_delete = sub.add_parser("delete", help="Delete an artifact + its payload")
    p_delete.add_argument("id")
    p_delete.set_defaults(func=_cmd_delete)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        rc = args.func(args)
    except lib.ArtifactError as exc:
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
