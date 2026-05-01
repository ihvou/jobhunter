# Jobhunter OpenClaw Jobbot

This repository contains a safe, low-cost MVP for the OpenClaw job-search workflow described in
[`OPENCLAW_JOB_SEARCH_SPEC.md`](OPENCLAW_JOB_SEARCH_SPEC.md).

The implementation is intentionally conservative:

- no LinkedIn logged-in browser automation
- no browser profile or cookie mounts
- no auto-apply
- no recruiter messaging
- Telegram-only human feedback loop
- local SQLite audit log
- local daily/monthly LLM budget gate

## What Runs

| Component | Purpose |
|---|---|
| `jobbot` | Deterministic job collector/ranker/Telegram bot |
| `openclaw-gateway` | Optional full OpenClaw Gateway container, isolated to local volumes |
| SQLite | Jobs, scores, feedback, drafts, usage logs |
| Telegram Bot | Digest and inline feedback buttons |

## Quick Local Check

```bash
python3 -m jobbot init
python3 -m jobbot collect
python3 -m jobbot digest
```

Without Telegram credentials, digest output prints to stdout.

## Docker

```bash
cp .env.example .env
docker compose build jobbot
docker compose run --rm jobbot python -m jobbot init
docker compose up -d jobbot
```

To also start the OpenClaw Gateway container after you approve its onboarding/configuration:

```bash
docker compose --profile openclaw up -d openclaw-gateway
```

OpenClaw's official Docker docs recommend onboarding and gateway-token setup before relying on the
gateway service. See [`docs/OPENCLAW_DOCKER_APPROVAL_STEPS.md`](docs/OPENCLAW_DOCKER_APPROVAL_STEPS.md).

## Telegram Buttons

Every job card uses the required inline actions:

| Button | Effect |
|---|---|
| `Irrelevant` | Marks job rejected and down-ranks similar/source signals |
| `Remind me tomorrow` | Snoozes the job for 24 hours |
| `Give me cover note` | Generates a draft cover note, stores it, sends it back |
| `Applied` | Marks job applied and boosts source/actionability signals |

## Config Files

| File | Purpose |
|---|---|
| `config/profile.json` | Target roles, keywords, exclusions, salary floor |
| `input/profile.md` | Text CV/profile used by ranking and cover notes |
| `config/sources.json` | Public RSS/API/email sources |
| `config/jobbot.json` | Budgets, model, digest size, collection interval |
| `.env` | Secrets and runtime paths |

## Commands

```bash
python3 -m jobbot init
python3 -m jobbot collect
python3 -m jobbot digest
python3 -m jobbot run-once
python3 -m jobbot telegram-poll
python3 -m jobbot discover-sources
python3 -m jobbot usage
python3 -m jobbot serve
```

## Safety Notes

Docker isolation lowers blast radius, but the real guardrail is removing harmful capabilities:

- do not mount browser profiles
- do not mount your home directory
- do not enable email sending
- do not give the bot recruiter messaging credentials
- keep OpenClaw bound to localhost
- keep OpenAI API key scoped to a dedicated low-budget project

