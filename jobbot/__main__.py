import argparse

from .app import JobBot, run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Job search bot for the OpenClaw job-search workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Initialize database and source registry")
    subparsers.add_parser("collect", help="Collect jobs from configured sources")
    subparsers.add_parser("digest", help="Send current Telegram digest")
    subparsers.add_parser("run-once", help="Initialize, collect, score, and send one digest")
    subparsers.add_parser("telegram-poll", help="Poll Telegram callbacks once")
    subparsers.add_parser("discover-sources", help="Ask the LLM for source-discovery recommendations")
    subparsers.add_parser("serve", help="Run collection and Telegram polling loop")
    subparsers.add_parser("usage", help="Print local usage summary")
    args = parser.parse_args()

    if args.command == "run-once":
        run_once()
        return

    bot = JobBot.from_environment()

    if args.command == "init":
        bot.initialize()
    elif args.command == "collect":
        bot.initialize()
        bot.collect()
    elif args.command == "digest":
        bot.send_digest()
    elif args.command == "telegram-poll":
        bot.poll_telegram_once()
    elif args.command == "discover-sources":
        bot.discover_sources()
    elif args.command == "serve":
        bot.serve()
    elif args.command == "usage":
        usage = bot.database.usage_summary()
        print("Spend today: $%.4f" % usage["today"])
        print("Spend this month: $%.4f" % usage["month"])


if __name__ == "__main__":
    main()

