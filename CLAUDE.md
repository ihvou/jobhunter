# Claude Instructions

This repository contains a human-in-the-loop job-search assistant for an OpenClaw-assisted workflow.

Read and follow `AGENTS.md` first. It is the canonical project contract for safety, privacy, source discovery, development, and validation.

## Short Version

Preserve the core product boundary:

- the bot may scout, rank, summarize, draft, and report
- the human applies, messages, approves sources, and provides secrets
- LinkedIn and other logged-in platforms are email-alert sources only, not browser automation targets

## Non-Negotiables

- Do not add logged-in LinkedIn/Wellfound browser automation.
- Do not mount browser cookies, real browser profiles, the host home directory, SSH keys, or Docker socket.
- Do not add auto-apply or recruiter messaging.
- Do not commit real CV/profile data, API keys, Telegram tokens, IMAP credentials, SQLite databases, or generated drafts.
- Keep Source Discovery recommendation-only unless the user explicitly asks to change that design.
- Keep Telegram actions compatible with `Irrelevant`, `Remind me tomorrow`, `Give me cover note`, and `Applied`.

## Private Profile Files

Use local ignored files for real user data:

```bash
cp input/profile.example.md input/profile.local.md
cp config/profile.example.json config/profile.local.json
```

Committed example files are templates only.

## Common Commands

```bash
python3 -m unittest discover -s tests
docker compose --profile openclaw config --quiet
git diff --check
python3 -m jobbot init
python3 -m jobbot discover-sources
```

If you change behavior, update `README.md` and tests where appropriate.

