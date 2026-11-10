# ring-router

Listener for Pebble Index 01 ring transcripts. It verifies each webhook, routes the
transcript by prefix, and runs a handler (iMessage, Notes, Reminders, Calendar, models, ...).
Plan and decisions: [Ring Router — Build Plan](https://claude.ai/artifact/62NfbWZRpseoMRFfKrjXw6).

**Status: phases 1–2 skeleton, everything in dry run.** No ring is needed: `ring-router sim`
emulates the Pebble app's signed multipart requests.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml     # edit; config.toml is git-ignored
.venv/bin/ring-router init             # path token + signing secret -> .env (mode 600)
```

Secrets live in `.env` in this folder (git-ignored, mode 600; see `.env.example`).
Real environment variables with the same names override it.

## Try it

```sh
.venv/bin/ring-router route "Ask Terri when she'll be home"     # offline: route + dry-run handler
.venv/bin/ring-router serve                                       # terminal 1
.venv/bin/ring-router sim "Tell Terri I'm on my way"              # terminal 2: as the ring would send
.venv/bin/ring-router sim --file tests/phrases.toml               # the whole phrase list
.venv/bin/ring-router sim --test-event --replay --bad-signature "Tell Terri hi"
.venv/bin/ring-router recent                                      # delivery log
.venv/bin/pytest -q                                               # routing spec + protocol + server tests
```

`tests/phrases.toml` is the answer key: each case is a phrase, how it must route, the text the
router extracts (`extracted`), and for messages the exact text that goes out (`sends`, after the
on-device "ask" rewrite). `pytest -m live` checks `sends` against the real model; `fm` runs in
greedy mode, so the same phrase always produces the same message. Add phrases as you think of them.

## Model instructions

Every instruction given to a model (Apple's on-device `fm` or OpenRouter) is in
[`prompts.toml`](prompts.toml), marked LIVE (used now) or DRAFT (route not built yet).
Edit it there; nothing is hard-coded in the Python.

## What's real vs stubbed

| Piece | State |
|---|---|
| Webhook checks (secret path, HMAC v1, ±5 min, dedupe by delivery ID, test events) | Done |
| Cancel detection, scenes (exact), timer refusal, recipient tiers, weak-match confirm | Done |
| "Ask" rewrite, note titles, shopping item split, reminder/event date extraction (`/usr/bin/fm`) | Done, real on-device model |
| iMessage / Notes / Reminders / Calendar via mcp-macos | Written, never run live (dry run) |
| Pushcut notifier + confirm buttons | Written, untested (console notifier by default) |
| OpenRouter "ask a model" | Written; needs API key + model IDs |
| Home, research, questions agent, general agent, Claude Code per host, location reminders | Stubs that report what they would do |
| Tunnel, launchd | Not yet |
