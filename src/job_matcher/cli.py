import argparse
import json
import sys
from pathlib import Path

from job_matcher.errors import error_message
from job_matcher.models import LEGACY_STATUSES
from job_matcher.service import Service


def read_json(path):
    return json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())


DISCOVERY_COMMANDS = {
    "board-add",
    "boards",
    "board-remove",
    "boards-import",
    "boards-from-history",
    "find",
}


def discovery_commands(sub):
    add = sub.add_parser("board-add", help="Add a public job board by careers URL or company")
    add.add_argument("--company")
    add.add_argument("--url", help="Careers, job or API URL on a supported ATS")
    boards = sub.add_parser("boards", help="List saved job boards")
    boards.add_argument("--active-only", action="store_true")
    remove = sub.add_parser("board-remove", help="Delete or deactivate job boards")
    remove.add_argument("board_id", nargs="?", type=int)
    remove.add_argument("--company")
    remove.add_argument("--deactivate", action="store_true", help="Keep but skip in find")
    imported = sub.add_parser("boards-import", help="Import boards from a JSONL file")
    imported.add_argument("--input", required=True, help="Absolute JSONL path")
    from_history = sub.add_parser(
        "boards-from-history", help="Add boards for companies in application history"
    )
    from_history.add_argument("--limit", type=int, default=20)
    find = sub.add_parser("find", help="Find fresh postings on saved boards (no evaluation)")
    find.add_argument("--since-days", type=int, default=7)
    find.add_argument("--keyword", action="append", help="Title keyword; repeatable")
    find.add_argument("--remote-only", action=argparse.BooleanOptionalAction, default=None)
    find.add_argument("--location", action="append", help="Location keyword; repeatable")
    find.add_argument("--limit", type=int, default=25)
    find.add_argument("--exclude-known", action="store_true")
    find.add_argument("--refresh", action="store_true", help="Ignore the 6-hour cache")


def run_discovery(service, args):
    if args.command == "board-add":
        return service.add_job_board(args.company, args.url)
    if args.command == "boards":
        return service.list_job_boards(not args.active_only)
    if args.command == "board-remove":
        return service.remove_job_board(args.board_id, args.company, args.deactivate)
    if args.command == "boards-import":
        return service.import_job_boards(args.input)
    if args.command == "boards-from-history":
        return service.add_boards_from_history(args.limit)
    return service.find_jobs(
        since_days=args.since_days,
        title_keywords=args.keyword,
        remote_only=args.remote_only,
        location_keywords=args.location,
        limit=args.limit,
        exclude_known=args.exclude_known,
        refresh=args.refresh,
    )


def ask_consent(service):
    """Show the TypeSafe disclosure on stderr and record consent only on an explicit yes."""
    print(service.consent()["disclosure"], file=sys.stderr)
    print("Do you agree? [y/N]: ", file=sys.stderr, end="", flush=True)
    if input().strip().lower() in {"y", "yes"}:
        return service.record_consent(True)
    return service.consent()


def main():
    parser = argparse.ArgumentParser(description="Job evaluator; all results are JSON")
    parser.add_argument("--data-dir", help="Private storage directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("onboarding", help="Return onboarding questions, defaults, and schema")
    save = sub.add_parser("profile-save", help="Save complete profile JSON")
    save.add_argument("--input", default="-")
    experience = sub.add_parser("experience-save", help="Merge confirmed interview answers JSON")
    experience.add_argument("--input", default="-")
    init = sub.add_parser("init", help="Interactive onboarding; prompts go to stderr")
    init.add_argument("--resume", required=True, help="UTF-8 plain text resume path")
    for name in ("evaluate", "batch", "history-import"):
        command = sub.add_parser(name)
        command.add_argument("--input", default="-")
        command.add_argument("--force", action="store_true", help="Bypass evaluation cache")
    shortlist = sub.add_parser("shortlist")
    shortlist.add_argument("--limit", type=int, default=50)
    shortlist.add_argument(
        "--compact", action="store_true", help="Return compact summaries, not full records"
    )
    history = sub.add_parser("history")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument(
        "--compact", action="store_true", help="Return compact summaries, not full records"
    )
    history.add_argument("--status", help="Keep only rows with this status")
    history.add_argument("--query", help="Match company or title, case-insensitive")
    agreement = sub.add_parser("consent", help="Show, grant or revoke consent to use TypeSafe")
    agreement.add_argument("action", choices=["show", "grant", "revoke"])
    agreement.add_argument("--yes", action="store_true", help="Grant without the prompt")
    strictness = sub.add_parser("strictness", help="Set the benefit-of-the-doubt dial")
    strictness.add_argument(
        "strictness", choices=["wide_net", "generous", "balanced", "selective", "strict"]
    )
    status = sub.add_parser("status")
    status.add_argument("job_id")
    status.add_argument(
        "status",
        choices=["new", "shown", "applied", "dismissed", "closed", *LEGACY_STATUSES],
        help="Legacy aliases: surfaced/saved mean shown; interview/in_progress mean applied",
    )
    status.add_argument("--reason", help="Why, stored with dismissed")
    discovery_commands(sub)
    daily = sub.add_parser("daily", help="Daily run: find, evaluate new postings and rank")
    daily.add_argument("--since-days", type=int, default=7)
    daily.add_argument("--max-evaluations", type=int, default=15, help="Cap on paid calls")
    daily.add_argument("--top", type=int, default=10, help="New matches to return")
    daily.add_argument("--open", type=int, default=5, help="Still-open matches to return")
    daily.add_argument("--refresh", action="store_true", help="Ignore the 6-hour cache")
    args = parser.parse_args()
    try:
        service = Service(args.data_dir)
        if args.command == "onboarding":
            result = service.onboarding()
        elif args.command == "profile-save":
            result = service.save_profile(read_json(args.input))
        elif args.command == "experience-save":
            result = service.save_experience_answers(read_json(args.input))
        elif args.command == "init":
            profile = service.onboarding()["profile"]
            profile["resume_text"] = Path(args.resume).read_text()
            for key in ("name", "location", "salary_floor", "salary_target"):
                print(f"{key} [{profile[key]}]: ", file=sys.stderr, end="", flush=True)
                value = input().strip()
                if value:
                    profile[key] = int(value) if key.startswith("salary_") else value
            print(
                "Target roles, separated by commas [keep defaults]: ",
                file=sys.stderr,
                end="",
                flush=True,
            )
            roles = input().strip()
            if roles:
                profile["target_roles"] = [r.strip() for r in roles.split(",") if r.strip()]
            for key in ("preferences", "dealbreakers"):
                print(
                    f"{key}, separated by semicolons [keep defaults]: ",
                    file=sys.stderr,
                    end="",
                    flush=True,
                )
                value = input().strip()
                if value:
                    profile[key] = [v.strip() for v in value.split(";") if v.strip()]
            print(json.dumps(profile, indent=2), file=sys.stderr)
            print("Save this profile? [y/N]: ", file=sys.stderr, end="", flush=True)
            result = service.save_profile(profile) if input().lower() == "y" else {"saved": False}
            if result["saved"] and not service.consent()["granted"]:
                result["consent"] = ask_consent(service)
        elif args.command == "evaluate":
            result = service.evaluate(read_json(args.input), args.force)
        elif args.command == "batch":
            result = service.batch(read_json(args.input), args.force)
        elif args.command == "history-import":
            result = service.import_history(read_json(args.input))
        elif args.command == "consent":
            if args.action == "grant":
                result = service.record_consent(True) if args.yes else ask_consent(service)
            elif args.action == "revoke":
                result = service.revoke_consent()
            else:
                result = service.consent()
        elif args.command == "strictness":
            result = service.set_strictness(args.strictness)
        elif args.command == "daily":
            result = service.daily_run(
                args.since_days, args.max_evaluations, args.top, args.refresh, args.open
            )
        elif args.command in DISCOVERY_COMMANDS:
            result = run_discovery(service, args)
        elif args.command == "status":
            result = service.status(args.job_id, args.status, args.reason)
        elif args.command == "history":
            result = service.history(
                args.limit, detail=not args.compact, status=args.status, query=args.query
            )
        else:
            result = service.shortlist(args.limit, detail=not args.compact)
        print(json.dumps(result, indent=2, allow_nan=False))
        if args.command in {"batch", "history-import", "boards-import"} and result["errors"]:
            sys.exit(1)
    except (ValueError, RuntimeError, OSError, EOFError) as exc:
        print(json.dumps({"error": error_message(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
