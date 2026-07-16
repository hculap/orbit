# BDD scenarios for issue #91 — Session Teleport (export/import session files).
#
# "Teleport" = move/recreate a Claude Code session as a portable FILE.
#   GET  → download a self-contained teleport bundle (the session transcript + provenance).
#   POST → upload that bundle, "plug it in" to a chosen agent (path-substituted),
#          minting a fresh, resumable session.
#
# Owner constraints (from #91 + comment):
#   - Must travel AS FILES (GET = download, POST = upload).
#   - New session is UNSAVED by default (no manual title unless one is passed).
#   - The target AGENT (lib_id) must be specified at teleport time.
#   - Endpoint is SKILL-DRIVEN — an agent reads the `teleport` skill and runs it.
#   - Paths inside the file are substituted (absolute cwd rewritten) so it plugs in.
#
# These scenarios are the contract verified by tests/test_orchestrator_teleport.py
# and by the live curl/workflow verification pass.

Feature: Session teleport — export and import Claude Code sessions as files

  Background:
    Given the dashboard is running with the orchestrator routes mounted
    And a source session "src-uuid" exists on disk with a multi-line JSONL transcript
    And the artifact auth token file exists

  # ---- EXPORT (GET, download) -------------------------------------------

  Scenario: Export returns a self-contained teleport bundle
    When I GET /api/orchestrator/sessions/src-uuid/teleport
    Then the response status is 200
    And the body is a JSON envelope with version 1 and kind "hetzner-session-teleport"
    And the envelope carries source_session_id, source_cwd, source_lib_id, model, title, git_branch, exported_at
    And the envelope transcript contains every distinct JSONL line of the source session
    And the response sets Content-Disposition attachment with a .json filename

  Scenario: Export of an unknown session is a 404
    When I GET /api/orchestrator/sessions/does-not-exist/teleport
    Then the response status is 404

  Scenario: Export merges and de-duplicates a session split across slug dirs
    Given the source session also has a stub line under a second cwd-slug directory
    When I export the session
    Then each transcript line appears exactly once, keyed by its line uuid
    And original line ordering is preserved

  # ---- IMPORT (POST, upload) --------------------------------------------

  Scenario: Import mints a fresh resumable session plugged into the chosen agent
    Given a teleport bundle exported from "src-uuid"
    When I POST /api/orchestrator/sessions/teleport with that bundle and lib_id "projects/orbit" and a valid token
    Then the response status is 200
    And the response returns ok true and a brand-new new_session_id distinct from src-uuid
    And a JSONL file is written under the target agent's cwd-slug directory named <new_session_id>.jsonl
    And every line's sessionId equals new_session_id
    And every line's cwd equals the target agent's absolute cwd
    And the sidecar for new_session_id records lib_id, cwd, and teleported_from "src-uuid"
    And no manual title is stamped (the session is unsaved by default)

  Scenario: Import without the auth token is rejected
    When I POST /api/orchestrator/sessions/teleport without the x-artifact-token header
    Then the response status is 401
    And no new session file is written

  Scenario: Import to the global agent places the session under HOME
    Given a teleport bundle and lib_id "" (global)
    When I import it with a valid token
    Then the new session's cwd equals HOME
    And the JSONL lands in the HOME cwd-slug directory

  Scenario: Import rejects a malformed bundle
    When I import a body whose envelope is not a dict, or has the wrong version, or lacks a transcript list
    Then the response status is 400
    And no new session file is written

  Scenario: Import rejects an invalid agent or model
    When I import with a lib_id that resolves to no PARA directory
    Then the response status is 400
    When I import with a model outside opus/sonnet/haiku
    Then the response status is 400

  Scenario: Optional title and model survive the import
    Given a teleport bundle
    When I import it with title "Resumed elsewhere" and model "opus"
    Then the sidecar title is "Resumed elsewhere" and the model is "opus"

  # ---- ROUND-TRIP / SKILL ------------------------------------------------

  Scenario: Round-trip export then import yields a valid, plugged-in transcript
    When I export "src-uuid" and import the bundle into another agent
    Then re-reading the new session JSONL yields well-formed JSON on every line
    And the transcript content (messages) is preserved while ids and cwd are rewritten

  Scenario: The teleport skill ships so it is auto-seeded on boot
    Given the repo skills/teleport directory
    Then it contains a SKILL.md so seed_bundled_skills installs and global-enables it
    And scripts/teleport_cli.py exposes export and import subcommands driven by HD_* env vars
