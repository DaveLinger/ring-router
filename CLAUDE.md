# CLAUDE.md: notes for AI assistants working in this repo

**ring-router** turns voice transcripts from a Pebble Index 01 ring into actions on a headless Mac
mini: iMessages, Notes, Reminders, Calendar, Hubitat/HomeKit devices, OpenRouter model questions, and
a general agent. Start with [README.md](README.md) (what it does, setup, status) and
[docs/PLAN.md](docs/PLAN.md) (design and the reasoning behind every decision).

## Deliberate decisions: don't "fix" these without asking

| Looks odd | Why it's intentional |
|---|---|
| Fixed prefix routes never call a cloud LLM | Speed, cost, predictability. Only Apple's on-device `fm` is used there. |
| `fm` always runs `--greedy` | Same input → same output; rewrites are testable (`pytest -m live`). |
| No confirmation on unlock / garage open by default | Push-to-talk means intent (owner's explicit call). Opt-in via `[home] confirm_actions`. |
| Messages only to allowlisted recipients; near-miss names need a Send tap | The one built-in safety check is about *who*, not *what*. |
| The general agent can't text or email anyone | Enforced in code: tool filter plus a per-call action guard in `agents.py`. Not just the prompt. |
| `run_command` only accepts a word-for-word excerpt of the transcript | The agent may trim background noise, never invent or reword a command. |
| Hubitat calls go through `/usr/bin/curl`, not urllib | macOS Local Network privacy silently blocks the launchd-run Homebrew Python ("No route to host"). Curl under Ring Router.app gets through. |
| A Swift launcher, `Ring Router.app`, wraps a Python service | One stable macOS identity for every privacy grant (Local Network, Calendars, Contacts, Automation, HomeKit). Signed with an Apple Development cert so grants survive rebuilds. |
| Vendored, patched `mcp-macos` and HomeMCPBridge (`vendor/patches/`) | iCloud-only calendars (a Google account is mirrored in by calsync), one HomeKit home, lookup by exact device id, Fan v2 support. |
| Core is standard library only | Fewer moving parts on a headless box. `pydantic-ai` is imported only when the agent runs. |
| `ask` vs `tell` | For messages: "ask" is rewritten into a direct question, "tell" is sent verbatim. For Claude Code and models they mean the same thing. |
| Timers and alarms are refused | No substitute is acceptable to the owner (no Pushcut timers, no phone Shortcuts). |

## Running and testing

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q -m "not live"     # unit + integration (fakes for fm, OpenRouter, MCP, Hubitat, HomeKit)
.venv/bin/pytest -q -m live           # exact message text via the real on-device model (macOS 27 only)
.venv/bin/ring-router route "phrase"  # offline routing decision (always dry run)
```

- `tests/phrases.toml` is the **answer key**: each phrase, its route, the extracted text and, for
  messages, the exact text sent. New routing behavior should come with cases there.
- `config.toml`, `.env` and `profile.md` are git-ignored and absent from the repo. Tests use
  `config.example.toml`.

## Safety rules for assistants

- **Never send real messages or control real devices** while reviewing. The live service on the Mac
  is in live mode. Use tests, fakes, `route`, or a dry-run config. Don't `sim` against the running
  service without the owner's say-so.
- Never print or commit secrets (`.env`), the real `config.toml`, phone numbers or addresses.
- Don't rebuild `Ring Router.app` or HomeMCPBridge, or change launchd jobs, without asking. Rebuilds
  can reset macOS permissions that need someone at the screen.

## Can only be verified on the Mac

macOS privacy prompts, mcp-macos / AppleScript side effects, Hubitat and HomeKit devices, Pushcut
delivery, the Tailscale Funnel URL, launchd behavior, and the real Pebble webhook format (the ring
hasn't arrived; the protocol comes from a reference project and still needs checking against real
traffic). Code-level review can't confirm these. Flag assumptions instead.

## Known gaps and parked ideas (don't spend time here unless asked)

- **HomeKit fan speed:** RotationSpeed writes are acknowledged but ignored by the bridged fans, so
  Hubitat stays on for fan speeds.
- **Cameras:** HomeMCPBridge snapshots crash on Catalyst.
- **Send delay / Undo Send:** deferred.
- **Claude Code per host:** a stub (needs SSH to a VPS).
- `live_recipients` still limits real sends to the owner.

## What a useful review looks like

Report findings **ranked by impact before changing code**, each with file:line, the concrete
failure (input → wrong result), and a suggested fix. Areas most worth a look:

1. **Router correctness:** misroutes on natural or noisy speech, cancel-phrase false positives,
   recipient tier mistakes. Propose new `phrases.toml` cases.
2. **Safety and security:** webhook verification, the token-gated pages (`/answers`, `/activity`,
   `/confirm`), agent tool and action guards, anything that could message the wrong person or
   control the wrong device.
3. **Error handling:** timeouts, partial failures (multi-device home commands), a backend down,
   duplicate deliveries and retries.
4. **Structure:** `handlers.py` has grown large. Suggest a split, but keep behavior identical and
   the tests green.
5. **Test gaps:** behavior that isn't covered.

Keep changes small and reviewable, keep the core standard-library only, and update README/PLAN
when behavior changes. Commits end with the attribution line used in this repo's history.
