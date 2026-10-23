"""Database and workflow commands.

    python -m agent.cli init-db           # migrate + add missing seed rows (idempotent)
    python -m agent.cli reset-demo --yes  # wipe all rows and restore the synthetic seed
    python -m agent.cli db-health         # schema version, foreign keys, integrity, row counts
    python -m agent.cli workflow --requester P-ARJUN --claim CLM-2026-0142 [--intent upload]
    python -m agent.cli workflow --requester P-ARJUN --resume RUN-...   # continue a saved run

`workflow` is interactive: it opens the portal in a browser, stops for the OTP (you type it) and
for each upload (you approve or reject the card). Stop it at any prompt; `--resume` picks the run
up from its last saved step.

Paths come from DATABASE_PATH and VAULT_DIR (see .env.example).
"""

from __future__ import annotations

import argparse
import json
import sys

from agent import repository as repo
from agent.config import load_settings
from agent.db import Database, DatabaseError, health
from agent.seed import clear_evidence, init_database, reset_demo_data
from shared.log import configure_logging


def main(argv: list[str] | None = None) -> int:
    """Run one command; returns a process exit code."""
    parser = argparse.ArgumentParser(prog="python -m agent.cli", description="ClaimSaathi database commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create or migrate the schema and add missing seed data")
    reset = sub.add_parser("reset-demo", help="delete all rows and restore the synthetic seed")
    reset.add_argument("--yes", action="store_true", help="confirm the wipe")
    reset.add_argument("--keep-evidence", action="store_true", help="keep evidence/RUN-* screenshots and HTML")
    sub.add_parser("db-health", help="check schema, foreign keys and integrity")
    flow = sub.add_parser("workflow", help="ask about a claim interactively (start or resume a run)")
    flow.add_argument("--requester", required=True, help="person id, e.g. P-ARJUN")
    flow.add_argument("--claim", help="claim id for a new run, e.g. CLM-2026-0142")
    flow.add_argument("--intent", choices=("status", "upload"), default="status")
    flow.add_argument("--resume", metavar="RUN_ID", help="continue a saved run instead of starting one")
    args = parser.parse_args(argv)
    if args.command == "workflow":
        return _workflow(args)

    configure_logging("agent-cli", "WARNING", stream=sys.stderr)  # stdout carries only the JSON result
    settings = load_settings()
    db = Database(settings.database_path)
    try:
        if args.command == "init-db":
            result = init_database(db, settings.vault_dir)
        elif args.command == "reset-demo":
            if not args.yes:
                print("Refusing to wipe the database without --yes.", file=sys.stderr)
                return 2
            result = reset_demo_data(db, settings.vault_dir)
            if not args.keep_evidence:
                result["evidence_runs_removed"] = clear_evidence(settings.evidence_dir)
        else:
            result = health(db)
            if result["ok"]:
                with db.read() as conn:
                    result["counts"] = repo.table_counts(conn)
    except DatabaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"command": args.command, "database": str(db.path), **result}, indent=2))
    return 0 if result.get("ok", True) else 1


def _workflow(args: argparse.Namespace, ask=input) -> int:  # noqa: ANN001 - input() or a test double
    """Drive one run in the terminal: start or resume it, answer its interrupts, print the answer."""
    from agent.graph import ClaimWorkflow, WorkflowDeps, WorkflowError
    from agent.actions import UploadPolicy
    from agent.llm import llm_from_settings
    from agent.portal_client import build_portal_client

    if not args.resume and not args.claim:
        print("give --claim for a new run, or --resume RUN_ID", file=sys.stderr)
        return 2
    configure_logging("agent-cli", "WARNING", stream=sys.stderr)
    settings = load_settings()
    db = Database(settings.database_path)
    init_database(db, settings.vault_dir)
    client = build_portal_client(settings, db)
    flow = ClaimWorkflow(WorkflowDeps(db=db, portal_for=lambda _person: client,
                                      policy=UploadPolicy.from_settings(settings), llm=llm_from_settings(settings)))
    try:
        if args.resume:
            view = flow.continue_run(args.resume, person_id=args.requester)
        else:
            view = flow.start(args.requester, args.claim, intent=args.intent)
        print(f"run {view.run_id} (resume with --resume {view.run_id})")
        while view.waiting is not None:
            w = view.waiting
            if w["type"] == "otp":
                if view.portal_state != "otp_required":
                    print("The portal sign-in was lost; asking the portal for a new OTP.")
                    view = flow.resume(view.run_id, person_id=args.requester, answer={"otp": ""})
                    continue
                if w.get("last_error"):
                    print(f"  {w['last_error']}")
                answer = {"otp": ask("Portal OTP (from the phone): ").strip()}
            else:
                card = w["card"]
                file = card["file"] or {}
                print(f"\n{w['question']}\n"
                      f"  patient {card['patient']['name']}, claim {card['claim']['claim_id']}, "
                      f"request {card['requirement']['requirement_id']}\n"
                      f"  file {file.get('filename')} sha256 {file.get('sha256')}\n"
                      f"  to {card['destination']['upload_url']}\n"
                      f"  why: {card['matching_evidence']['reason']}")
                choice = ask("approve / reject: ").strip().lower()
                answer = {"decision": "approve" if choice == "approve" else "reject",
                          "card_sha256": w["card_sha256"], "action_id": w["action_id"]}
            view = flow.resume(view.run_id, person_id=args.requester, answer=answer)
        for step in view.steps:
            print(f"  {step['seq']:>2} {step['node']:<22} {step['result']:<12} {step['decision']}")
        final = view.final_response or {}
        print("\n" + (final.get("summary_text") or f"run is {view.status}"))
        return 0 if view.status in ("completed", "blocked") else 1
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        flow.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
