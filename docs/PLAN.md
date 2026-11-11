# Ring Router — Build Plan

A small listener on the Mac mini takes voice transcripts from the **Pebble Index 01** ring and routes
each one to the right action: iMessage, Notes, Reminders, Calendar, home devices, or an outside AI
model. Answers and confirmations come back as **Pushcut** notifications.

> **Status (Nov 2026):** phases 1–2 skeleton built and tested in dry run; see the
> [README](../README.md#status) for what's real vs stubbed. This plan records the decisions.

**Contents:** [Goals](#goals) · [How the Index sends data](#how-the-pebble-index-sends-data) ·
[Architecture](#architecture) · [Routing & safety](#routing-and-recipient-safety) ·
[Route catalog](#route-catalog) · [Additional routes](#additional-routes) ·
[General agent](#general-agent) · [Timers](#timers-and-alarms-refused) ·
[On-device model](#on-device-apple-model) · [Home](#home-automation) ·
[External models](#external-models) · [Responses](#responses-and-confirmations) ·
[Security](#security-and-permissions) · [Phases](#build-phases) · [Open questions](#open-questions-and-risks) ·
[Sources](#sources)

---

## Goals

- Common commands ("tell Terri I'm on my way") run with **no cloud LLM** in the path: fast, cheap, predictable.
- Anything that doesn't match a prefix goes to a **general agent** with the macOS tools.
- Messages only go to **allowlisted** people and chats; a near-miss name needs a confirmation.
- **Push-to-talk means intent:** the ring only listens while the button is held, so commands run as
  spoken, including unlock and garage-open. Confirm-first is opt-in per action (`[home] confirm_actions`).
- Both home automation paths, **Hubitat** and **HomeKit**, can be tested side by side.

**Not in scope:** email (the macos-mcp mail tools are left out; Gmail if ever needed), and anything
sent back to the ring itself, which has no screen or speaker.

---

## How the Pebble Index sends data

The phone app transcribes, then POSTs to **one** custom webhook. Nothing comes back to the ring.
Details come from Pebble's docs and community receivers; phase 0 confirms them against real traffic.

| Aspect | What we know |
| --- | --- |
| Where transcription runs | On the phone. Local mode is a one-time ~670 MB download; cloud mode uses Wispr Flow; each can fall back to the other. |
| Webhook slots | One, under the Index tab in the app. |
| Ring button routing | "Webhook only", or a built-in action plus "Also send to webhook". **Webhook only** avoids duplicate notes and reminders. |
| What to send | "Transcription only". "Recording only" sends no text. |
| Format | `multipart/form-data`, protocol v1 |
| Fields | `transcription`, `recordedAt` (Unix ms), `audio` (m4a, ignored), `client`, `test` |
| Headers | `X-Index-Webhook-Version` (`1`), `X-Index-Timestamp` (Unix s), `X-Index-Delivery` (recording ID, reused on retries), `X-Index-Trigger` (`single-click-hold`, `double-click-hold`, `test-event`), `X-Index-Test`, `X-Index-Signature` |
| Signing | HMAC-SHA256 hex over `v1\n{timestamp}\n{delivery}\n{trigger}\n{1\|0}\n` + raw body. Secret pasted into the app's "Sign requests" setting. |
| Retries | A **503** is retried every minute, up to 10×. 200, 4xx and 422 are not retried automatically. |
| Reply to user | None. The app shows only the status code in Recent runs. |

### Request handling

Adapted from [th3walkingdud3/peeble-ring-router](https://github.com/th3walkingdud3/peeble-ring-router):

1. Secret URL path (`/hook/<token>`); a wrong path gets **404**.
2. Constant-time HMAC check and a ±5-minute timestamp window; failure gets **401**.
3. Test events get 200 and run nothing; other triggers are ignored.
4. De-duplicate by `X-Index-Delivery` in SQLite; repeats get 200 and don't run again.
5. Parse the multipart body; drop the audio part.
6. A send that times out is marked `unknown`, gets **422**, and is **never re-sent automatically**.
7. Global and per-route dry run, plus a simulator that sends signed fake requests.

**What we add:** slow routes (models, agents) store the job, reply `200 accepted` immediately, and
finish in the background; the result arrives by Pushcut.

---

## Architecture

One Python service on the Mac mini. It listens on localhost only; Tailscale Funnel brings requests in;
launchd keeps it running, like `calsync`.

```mermaid
flowchart LR
    ring["💍 Index ring<br/><small>hold to talk</small>"] --> app["📱 Pebble app<br/><small>speech to text,<br/>signed POST</small>"]
    app --> tunnel["🔒 Tailscale Funnel<br/><small>HTTPS → localhost</small>"]
    tunnel --> checks["Listener checks<br/><small>path · HMAC · time · dedupe</small>"]
    checks --> router{{"Router<br/><small>cancel · scenes · prefixes</small>"}}
    router -- prefix match --> im["iMessage<br/><small>allowlist + tiers</small>"]
    router -- prefix match --> apple["Apple apps<br/><small>Notes · Reminders · Calendar</small>"]
    router -- prefix match --> home["Home<br/><small>Hubitat · HomeKit</small>"]
    router -- prefix match --> models["Models<br/><small>OpenRouter</small>"]
    router -- no match --> agent["General agent<br/><small>Pydantic AI · macos-mcp · web search</small>"]
    im & apple & home & models & agent --> push["🔔 Pushcut<br/><small>results · answers · confirmations</small>"]
    push -. Send / Cancel tap .-> checks
    fm -.->|"“ask” rewrite"| im
    fm -.->|"titles · item splits · dates"| apple
    fm["🍎 Apple Intelligence<br/><b>on-device model</b><br/><small>/usr/bin/fm · greedy · offline</small>"]
    classDef applefm fill:#f2f2f7,stroke:#8e8e93,stroke-width:2px,color:#1c1c1e
    class fm applefm
```

Fast fixed routes reply to the Pebble app with their real result; slow routes reply "accepted".
A confirm tap in Pushcut calls back into the listener to run the pending action.

---

## Routing and recipient safety

The router is a pure function of (transcript, config). Order:

1. **Cancel phrase** at the end → nothing runs.
2. **Scene**: exact whole-transcript match only.
3. **Fixed prefix routes**, most specific first.
4. **Refusals** (timers, alarms).
5. **Anything else**, question or request → general agent.

### Cancel detection

If a transcript ends with a cancel phrase, nothing runs; Pushcut says "Cancelled, nothing done".

| Strength | Example phrases | Cancels when |
| --- | --- | --- |
| Strong | "cancel cancel", "cancel that", "scrap that", "never mind", "ignore me", "abort" | They end the transcript, or are all of it |
| Weak | "forget that", "don't do that", "stop", "cancel" | They end the transcript **and** follow a sentence break |

"Tell Terri I'll be late. Scrap that." is cancelled; "Tell Terri to forget that" sends.

**Later:** a separate "cancel" recording cancels the most recent unfinished action, and mid-sentence
corrections ("…scrap that, tell Terri I'm on my way") run only the part after the cancel phrase.

### Taking back a sent message (deferred)

**Decision (Nov 2026):** strong-match messages send immediately. Both options below are parked;
revisit if real use shows a need.

| Option | How | Trade-off |
| --- | --- | --- |
| **Send delay** | Each message waits a few seconds; a "cancel" in that window stops it | Reliable for SMS and any device; every message arrives slightly later (per-recipient, can be 0) |
| iMessage **Undo Send** (experiment) | UI-scripts Messages' own Undo Send | Messages' AppleScript only has `send`, so it's fragile; needs Accessibility and a logged-in session; 2-minute window; old OS versions still see the original |

### Recipient tiers

| Tier | Example | Action |
| --- | --- | --- |
| 1. Allowlisted, strong match | "tell terri…", "tell terry…" | Send, then a receipt notification |
| 2. Allowlisted, weak match | "tell sherry…" | Pushcut confirmation: **Send / Cancel** |
| 3. Not allowlisted | "tell harry…" | Never sent: "Didn't recognize 'harry', nothing sent." |
| 3b. *(later)* LLM guess | "tell harry…" | Suggests who you meant from message recency; you confirm |

Names match by spelling **and** sound. "Hey X" / "X, …" is direct speech (sent as spoken); only
"ask X…" is rewritten into a direct question. "Tell X to grab milk" sends "Grab milk".

---

## Route catalog

| # | You say | Handler | LLM | Backend |
| --- | --- | --- | --- | --- |
| 1 | "Ask Terri when she'll be home" | Rewrite into a direct question, iMessage | on-device | macos-mcp `messages_chat` |
| 2 | "Instinct…", "hey / ask / tell Instinct…" | iMessage ("ask" rewritten) | on-device for "ask" | macos-mcp `messages_chat` |
| 3 | "Tell Terri I'm on my way" | iMessage, words as spoken | none | macos-mcp `messages_chat` |
| 4 | "Tell the guys let's hang out tomorrow" | Group chat by chat ID | none | macos-mcp `messages_chat` (`chatId`) |
| 5 | "Make a note: …" | New note, titled by on-device summary | on-device | macos-mcp `notes_items` |
| 6a | "Remind me at 5 to call the vet" | Extract title + due time | on-device | macos-mcp `reminders_tasks` |
| 6b | "Remind me when I get home to…" | Location trigger | on-device | Custom Swift EventKit helper (macos-mcp can't) |
| 7 | "Add dentist Tuesday at 3 to my calendar" | Extract title, time, calendar | on-device | macos-mcp `calendar_events` |
| 8 | "Turn off the living room lights" | Device/room match + action (deterministic) | none | Hubitat Maker API; HomeMCPBridge for HomeKit-native |
| 9 | "Ask ChatGPT / Claude / DeepSeek / Qwen …" | Prefix picks the model | yes | OpenRouter |
| — | anything else (questions too) | General agent | yes | Pydantic AI on OpenRouter + macos-mcp + web search |

Date extraction always includes today's date and time zone. The macos-mcp mail tools are never exposed.

---

## Additional routes

| Route | You say | Output |
| --- | --- | --- |
| **Claude Code per host** | "Ask Claude Code on the RackNerd VPS why the websites are down" | Headless `claude -p` locally or over SSH; Pushcut summary |
| **Shopping list** | "Add milk and eggs to my shopping list" | One reminder per item in the Reminders list "Shopping list" |
| **Remember** | "Remember I parked on level 3, row G" | A new note in the "Remember" folder |
| **Scenes** | "Goodnight" (the entire transcript) | Fixed device actions |
| **Research** | "Research the best robot vacuum under $500" / "Research with DeepSeek…" | A note in the "Research" folder; Pushcut just says it's ready |

- **Claude Code:** hosts and spoken names are in config; unknown hosts are refused; default is the
  Mac mini. "Ask" and "tell" mean the same thing; what Claude Code may do is set per host in config, not by wording.
- **Shopping list:** on-device structured output splits items, so "mac and cheese" stays one item.
- **Remember:** one note each, because macos-mcp caps note bodies at 2,000 characters.
- **Research:** like "ask ChatGPT" but the output is a note; written via AppleScript to avoid the 2,000-character cap.

---

## General agent

**Every transcript that matches no fixed route goes to one general agent**, questions and requests
alike. An earlier design split off a read-only "questions agent", but routing by sentence shape broke
on natural speech: "Can you add the cheapest one to my calendar?" looks like a question and would have
landed on an agent that couldn't write. One agent with a shared conversation thread avoids that.

| Tool | Allowed actions |
| --- | --- |
| Calendar | read, create, update, delete |
| Reminders | read, create, update, delete |
| Notes | read, create, update |
| Contacts | read, create, update |
| web_search | the agent writes its own query; one OpenRouter web-plugin call (`[agents] web_search_model`) |
| run_command | hands a noise-trimmed, word-for-word command back to the router (see below) |
| Messages | read only, and only if `[agents] read_messages = true` (needs Full Disk Access) |
| Mail | none |

- Tools not in the table are invisible. Each mcp-macos tool takes an `action` argument, so a guard checks
  every call and refuses anything not allowed. In dry run, writes return "DRY RUN: would…".
- **Background noise → `run_command`:** if a transcript like "okay Mark, thanks, see you later… ask
  deepseek what color is the sky" misses every prefix, the agent can hand the buried command back to
  the top of the pipeline, where it gets normal routing, allowlist, tiers, cancels and dry run. Safeguards
  in code: the command must be a **word-for-word excerpt** of the transcript (the agent can trim noise,
  never add or change words); a command that routes back to the agent is refused; once per request; a
  rerouted *message* needs a Send/Cancel tap (`[agents] confirm_rerouted_messages`); the rerouted route
  sends the only notification.
- **Follow-ups:** the agent keeps one conversation thread (10-minute window), passed as message history,
  so "Which of those is cheapest?" or "Why isn't the Tesla Model S Plaid on this list?" work naturally.
- Instructions combine the spoken-transcript preamble, `profile.md`, `prompts.toml` and the current date/time.
- Default model: `anthropic/claude-sonnet-5.5` (`[agents] model`), about 2–5¢ per request.
- **Instinct:** the agent can't message anyone. If it can't finish a request (e.g. it needs someone else's
  availability) it suggests a command, such as "ask Instinct to find a time for you and Terri".

**Decisions still open**

- [ ] Messages reading: needs Full Disk Access, granted per program, so a small wrapper app is safer.
- [ ] Private text goes to the model: use OpenRouter's no-training/no-retention provider settings.
- [ ] Gmail and Google Drive: in or out?

## Timers and alarms: refused

The Mac can't set the iPhone's Clock timers or alarms, and substitutes aren't worth having.

- "Set a timer", "timer for", "wake me at", "set an alarm" hit a **refusal** before any agent; Pushcut
  says "Timers and alarms aren't supported from the ring".
- The general agent has no scheduling tool and is told to decline rather than make a reminder instead.
- *Optional experiment:* Focus mode via a Mac Shortcut that syncs to the iPhone ("Share across devices").

---

## On-device Apple model

macOS 27 ships Apple's on-device model as `/usr/bin/fm`: free, offline, ~0.35 s per call. It runs in
**greedy** mode, so the same input always gives the same output (no time cost). It handles "ask"
rewrites, note titles, shopping-item splits, and reminder/event extraction.

| You say | Sent |
| --- | --- |
| ask Terri when she'll be home | "When will you be home?" |
| ask the guys if they want to get beers Friday | "Do you want to get beers Friday?" |
| ask Instinct whether the report is done and what time we're meeting | "Is the report done and when are we meeting?" |

**Lessons:** strip the prefix before calling the model; send only recipient + remainder; strip a
leading recipient name the model sometimes adds.

**Fall back to your exact words when the rewrite** comes back empty, is more than about twice as long,
or talks about the recipient by name. The receipt always shows the exact text sent.

All model instructions live in [`prompts.toml`](../prompts.toml).

---

## Home automation

Two backends behind one set of spoken names. **Hubitat** is built; **HomeKit** (the superset, which
includes HomeKit-native devices like the front door lock) comes via HomeMCPBridge once Xcode is installed.

| Backend | Covers | How | Status |
| --- | --- | --- | --- |
| **Hubitat Maker API** | Z-Wave devices on the hub (24 controllable) | Local HTTP to the hub, no MCP, no LLM | ✅ Built; matched against the real hub |
| **[HomeMCPBridge](https://github.com/coalsi/HomeMCPBridge)** | Everything in HomeKit, incl. HomeKit-native devices | Mac Catalyst app that *is* an MCP server; needs Xcode + Apple ID signing with the HomeKit entitlement | 🟡 Waiting on Xcode |

**How a home command is resolved (deterministic, no LLM):**

1. The router's verb gives the action (`turn on/off`, `set … to`, `dim`, `brighten`, `lock`, `unlock`).
   `set the fan to low` → `setSpeed low`; `dim the sconces to 20%` → `setLevel 20`.
2. Candidates are narrowed by the capability the action needs: unlock considers only locks, fan speed only fans.
3. The spoken target is matched: `[hubitat] aliases` → exact label → room + "lights"
   ("the living room lights" = every light in that room) → "all the lights" → word overlap.
4. If several devices tie, `[hubitat] prefer` breaks it (`["ceiling fan"]`: "the living room fan" →
   Living Room Ceiling Fan). Still tied → **"Which one?"** notification and nothing changes.
5. Sensors, remotes and anything that looks like a detector, alarm or siren are never controllable.

| Action | Behavior |
| --- | --- |
| Lights, fans, switches, locks | Run immediately, then notify |
| Actions in `[home] confirm_actions` | Need a Pushcut tap first. **Empty by default**: the ring only listens while you hold the button, so unlock and garage-open run as spoken |
| Not recognized / ambiguous | Nothing happens; you're told what was heard and the candidates |

**Scenes** (exact phrase, e.g. "Goodnight") run a configured list of the same device actions.

---

## Activity page and down alert

- **Activity page** (`/activity/<activity_token>` through the tunnel): recent commands with outcome and
  model cost; each opens a **step-by-step trace**: request metadata, exact transcript, cleaned text,
  route decision, on-device model input → output, composed message, model request and reply, every
  agent tool call with its result, device matches, each command sent (mcp-macos, AppleScript,
  Hubitat, georeminder) with its result, notifications, result, and the HTTP response the Pebble app
  saw. Kept 90 days.
- **Watchdog** (launchd, every 5 minutes): checks the listener locally and through the tunnel;
  Pushcut alert after two failures ("listener down" vs "tunnel down") and on recovery. It can't
  report the Mac itself being off.

---

## External models

Every "ask a model" request goes through **OpenRouter**, paid per token (a fraction of a cent per
question), with context we keep ourselves.

| Prefix | Family | Note |
| --- | --- | --- |
| ask/tell chatgpt, chat | OpenAI | `openai/gpt-6.1-terra` ($1 / $5 per M tokens) |
| ask/tell claude | Anthropic | `anthropic/claude-sonnet-5.5` ($2 / $10) |
| ask/tell deepseek | DeepSeek | `deepseek/deepseek-v4.1-flash` ($0.05 / $0.57); own blind spots (Chinese politics) |
| ask/tell qwen | Qwen | `qwen/qwen3.8-flash` ($0.15 / $0.47) |

- Your words are sent **verbatim** with a note that they're a speech transcript (no rewrite step).
- **Why not your ChatGPT/Claude subscriptions:** there's no API to ChatGPT memory; Codex and `claude -p`
  don't carry consumer memory; headless billing is unclear.
- **Context we build:** `profile.md` (facts about you, git-ignored), a 10-minute follow-up window per model
  ("ask DeepSeek more about that" continues the thread; the on-device model first rewrites a
  follow-up into a standalone question so the web search finds the right thing), and OpenRouter's `web` plugin (3 results for
  "ask", 8 for research; ~$0.005–0.007 per search).
- **Answers:** the notification shows up to 300 characters. Longer answers, or any with sources, get a
  **Full answer** button that opens `/answers/<id>` on the listener: a phone-friendly page with the
  question, answer, sources, model and cost. The ID is an unguessable token.
- **General agent:** open source, [Pydantic AI](https://ai.pydantic.dev/) on OpenRouter. Starts with no
  tools and gets only the listed macOS tools. Not the Claude Agent SDK for now (Claude-only, separate CLI
  process, ships shell/file tools); revisit if tasks grow long and multi-step.

---

## Responses and confirmations

| Event | Notification | Buttons |
| --- | --- | --- |
| Message sent | "Sent to Terri: 'When will you be home?'" | — |
| Weak recipient match | "Send to Terri: '…'?" | Send / Cancel |
| Unknown recipient | "Didn't recognize 'harry', nothing sent" | — |
| Confirm-first action (if configured) | "Unlock front door?" | Unlock / Cancel |
| Model answer | Short answer | Open full answer |
| Note / reminder / event | "Note created: Grocery ideas" | — |
| Error | What failed and what was heard | — |

Pending actions get a one-time ID that expires in 5 minutes. Expired or cancelled actions never run.

---

## Security and permissions

Anyone who could fake this webhook could text your contacts or unlock your door.

- Listener binds to **localhost** only; the tunnel is the only way in.
- Checks in order: secret path (404) → HMAC (401) → timestamp ±5 min (401) → dedupe.
- Pushcut callbacks use one-time tokens; refusals log the reason but return no detail.
- **Secrets** live in a git-ignored `.env` (mode 600) and are never logged. Real recipients and chat IDs
  live in the git-ignored `config.toml`.
- The activity page sits behind its own unguessable token (`activity_token`), since it shows transcripts.
- **One macOS identity:** the listener runs inside **Ring Router.app**, a tiny signed launcher bundle
  (`local.ring-router`), so every privacy grant lives under "Ring Router" and survives Python upgrades.
  This was required for Local Network access: macOS silently blocks a launchd-run Homebrew Python from
  reaching LAN devices ("No route to host"), a known Sequoia-era behavior.

| Permission | Needed for | Phase |
| --- | --- | --- |
| Automation (Messages, Notes, Contacts) | Sending iMessages, creating notes, reading the My Card address | ✅ granted to Ring Router |
| Reminders, Calendars (Full Access), Contacts | macos-mcp EventKit tools, location helper | ✅ granted to Ring Router |
| Local Network | Hubitat Maker API on the LAN | ✅ granted to Ring Router |
| HomeKit | HomeMCPBridge | 5 (pending Xcode) |
| Full Disk Access | Reading Messages (general agent, tier 3b) | later, optional |

---

## Build phases

Every route starts in dry run until you've watched it behave.

| Phase | Scope | Done when |
| --- | --- | --- |
| **0. Capture** 🟡 | Tailscale Funnel ✅, launchd service ✅; ring setup waits on the ring | Real requests verify; "Webhook only" confirmed |
| **1. Skeleton** ✅ | Checks, store, parser, background jobs, notifier, simulator | Recordings echo back; tests pass |
| **2. Messaging** 🟡 | Router, tiers, on-device rewrite, iMessage send | Live to the test recipient; weak-match Send/Cancel works through the tunnel. Remaining: lift `live_recipients` |
| **3. Models** ✅ | OpenRouter routes, `profile.md`, follow-ups, web search, answer page, research notes | Live-tested with all four models and research (notes wait on the Notes permission) |
| **4. Apple apps** ✅ | Notes, reminders, shopping list, calendar live; location reminders from the My Card address | "When I get home" verified on the iPhone |
| **5. Home** 🟡 | Hubitat ✅ (matching, rooms, fans, dimmers, locks, scenes); HomeKit via HomeMCPBridge | Hubitat live test, then HomeKit once Xcode is installed |
| **6. General agent** ✅ | Pydantic AI over macos-mcp + web search, follow-ups, `run_command`, tool/action guard | Web answers, follow-ups and rerouting live-tested; macOS tools wait on permission prompts |
| **7. Additional routes** | Shopping list & Remember → timer refusal → scenes → research → Claude Code per host → Focus (experimental) | Each live in order |

Tier 3b and the gesture mode switch come later, if real use shows they're worth it.

---

## Open questions and risks

- [x] Tunnel: Tailscale Funnel at the Mac's ts.net address, proxying to localhost:8642; launchd keeps the listener up.
- [x] macOS permissions (Messages, Notes, Reminders, Calendars full access, Contacts): granted to the service and verified.
- [x] Calendars: default "Dave"; shared "Dave/Terri" (our, shared, Dave and Terri…); "Work".
- [x] Home location: read from the Contacts My Card each time; 100 m geofence.
- [x] Devices: pulled from Hubitat automatically; nicknames in `[hubitat] aliases`, tie-breakers in `prefer`.
- [x] OpenRouter model IDs: `gpt-6.1-terra`, `claude-sonnet-5.5`, `deepseek-v4.1-flash`, `qwen3.8-flash`; agent on `claude-sonnet-5.5`.
- [ ] How long the Pebble app waits for a response.
- [x] Pushcut: the notification's webhook URL accepts per-send title, text and action buttons (link and background-URL); confirmed on the phone.

| Risk | Mitigation |
| --- | --- |
| Pebble changes the webhook format | Version header check; unknown fields tolerated and logged |
| A misheard name texts the wrong person | Allowlist, tiers, confirmation on weak matches, receipts |
| A timed-out send gets retried and double-texts | `unknown` sends are never re-sent automatically |
| The on-device rewrite changes meaning | Greedy mode, fallback rules, `sends` tests, receipts; "tell" never rewritten |
| HomeMCPBridge is young | Hubitat path for Z-Wave devices; HomeKit only for HomeKit-native devices |
| macOS Local Network privacy blocks background tools | Ring Router.app wrapper; `check-permissions` verifies from the service's own context |
| macos-mcp changes under opencode's npx cache | The router pins its own `mcp-macos@4.1.0` |

---

## Sources

- [Pebble Index 01 getting started guide](https://help.repebble.com/en/articles/15434751-index-01-getting-started-guide)
- [Pebble Index MCP and webhook notes](https://ndocs.repebble.com/index-mcp-instructions)
- [th3walkingdud3/peeble-ring-router](https://github.com/th3walkingdud3/peeble-ring-router) — reference for request handling
- [xeb/indexd](https://github.com/xeb/indexd) · [parthchandak02/pebble-hermes-bridge](https://github.com/parthchandak02/pebble-hermes-bridge) · [TMTrevisan/pebble-webhook-bridge](https://github.com/TMTrevisan/pebble-webhook-bridge)
- [Index webhook JSON payload issue #475](https://github.com/coredevices/mobileapp/issues/475)
- [krmj22/macos-mcp](https://github.com/krmj22/macos-mcp) (installed as `mcp-macos` 4.1.0)
- [HomeMCPBridge](https://github.com/coalsi/HomeMCPBridge) · [eventkit-rs](https://docs.rs/crate/eventkit-rs/latest)
- [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan) · [Sign in with ChatGPT](https://flaviocopes.com/sign-in-with-chatgpt/)
