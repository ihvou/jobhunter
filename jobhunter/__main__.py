import argparse
import json

from .app import JobHunter, run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Job search bot for the OpenClaw job-search workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Initialize database and source registry")
    subparsers.add_parser("collect", help="Collect jobs from configured sources")
    subparsers.add_parser("digest", help="Print current ranked digest rows as JSON")
    subparsers.add_parser("leads", help="Print current lead digest rows as JSON")
    subparsers.add_parser("run-once", help="Initialize, collect, and score once")
    subparsers.add_parser("service", help="Run the localhost HTTP service for OpenClaw plugin tools")
    subparsers.add_parser("usage", help="Print local usage summary")
    args = parser.parse_args()

    if args.command == "run-once":
        run_once()
        return
    if args.command == "service":
        from .service import run

        run()
        return

    bot = JobHunter.from_environment()

    if args.command == "init":
        bot.initialize()
    elif args.command == "collect":
        bot.initialize()
        bot.collect()
    elif args.command == "digest":
        from .service import JobHunterService

        print(json.dumps(JobHunterService(bot).digest(mark_sent=False), indent=2, sort_keys=True))
    elif args.command == "leads":
        from .service import JobHunterService

        print(json.dumps(JobHunterService(bot).leads_digest(mark_sent=False), indent=2, sort_keys=True))
    elif args.command == "usage":
        usage = bot.database.usage_summary()
        print("Spend today: $%.4f" % usage["today"])
        print("Spend this month: $%.4f" % usage["month"])


if __name__ == "__main__":
    main()
