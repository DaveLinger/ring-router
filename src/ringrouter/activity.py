"""Activity pages: recent commands, and a step-by-step trace for each one.

  /activity/<token>                  list of recent deliveries
  /activity/<token>/<delivery_id>    every recorded step of one delivery

<token> is the activity_token secret (.env); the pages show transcripts and messages.
"""

from __future__ import annotations

import html
import json
import time
import urllib.parse

PHASES = {
    "received": ("Request received", "Pebble webhook metadata, after signature and timestamp checks passed"),
    "transcript": ("Transcript", "Exactly what the Pebble app sent"),
    "cleaned": ("Cleaned text", "Whitespace and quotes normalised, leading filler words dropped"),
    "route": ("Route decision", "Cancel check → scene → fixed prefixes → refusals → general agent"),
    "message_composed": ("Message composed", "The exact text that will be sent"),
    "on_device_model": ("Apple on-device model", "/usr/bin/fm: input → output"),
    "model_request": ("Sent to model", "OpenRouter request"),
    "model_reply": ("Model reply", "OpenRouter response"),
    "agent": ("General agent", "Tool calls the agent made and its reply"),
    "reroute": ("Rerouted command", "The agent handed a trimmed command back to the router"),
    "command": ("Command sent", "Side effect on the Mac"),
    "command_result": ("Command result", ""),
    "notification": ("Notification", "Sent to your phone via Pushcut"),
    "result": ("Result", ""),
    "response": ("Response to the Pebble app", "HTTP status the app sees in Recent runs"),
}

STATE_COLORS = {"done": "ok", "sent": "ok", "dry_run": "dry", "accepted": "dry", "needs_confirm": "warn",
                "cancelled": "muted", "refused": "warn", "discarded": "muted", "failed": "bad", "unknown": "bad"}

CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1c1c1e; --muted:#6e6e73; --line:#e5e5ea; --card:#f5f5f7;
        --ok:#1f8a3b; --dry:#0a66c2; --warn:#b25e00; --bad:#c62828; --link:#0066cc; }
@media (prefers-color-scheme: dark) { :root { --bg:#1c1c1e; --fg:#f5f5f7; --muted:#a1a1a6; --line:#3a3a3c;
        --card:#2c2c2e; --ok:#4cc26b; --dry:#5aa9ff; --warn:#ffb340; --bad:#ff6b6b; --link:#4da3ff; } }
body { background:var(--bg); color:var(--fg); font:15px/1.45 -apple-system, system-ui, sans-serif;
       max-width:900px; margin:0 auto; padding:16px 16px 48px; }
a { color:var(--link); text-decoration:none; } h1 { font-size:22px; margin:4px 0 12px; }
.muted { color:var(--muted); } .small { font-size:13px; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; font-weight:600;
         border:1px solid currentColor; white-space:nowrap; }
.ok { color:var(--ok); } .dry { color:var(--dry); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.row { display:block; padding:10px 0; border-bottom:1px solid var(--line); color:var(--fg); }
.row .t { font-weight:600; } .row .meta { margin-top:2px; }
.step { background:var(--card); border-radius:10px; padding:10px 12px; margin:10px 0; }
.step h3 { margin:0; font-size:15px; } .step .when { float:right; color:var(--muted); font-size:12px; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:3px 12px; margin:8px 0 0; }
dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; margin:0; font:13px/1.4 ui-monospace, monospace; }
"""


def _page(title: str, body: str, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{meta}'
            f'<meta name="robots" content="noindex"><title>{html.escape(title)}</title>'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


def _badge(state: str | None) -> str:
    s = state or "?"
    return f'<span class="badge {STATE_COLORS.get(s, "muted")}">{html.escape(s)}</span>'


def _when(ts: float) -> str:
    return time.strftime("%b %-d %-I:%M:%S %p", time.localtime(ts))


def list_page(base: str, rows: list[dict]) -> str:
    items = []
    for r in rows:
        cost = f" · ${r['cost']:.4f}" if r.get("cost") else ""
        did = urllib.parse.quote(r["delivery_id"])
        items.append(
            f'<a class="row" href="{base}/{did}"><div class="t">{html.escape(r.get("transcript") or "(no transcript)")}</div>'
            f'<div class="meta small muted">{_badge(r.get("state"))} {html.escape(r.get("route") or "")} · '
            f'{_when(r["first_seen"])}{cost}</div>'
            f'<div class="small muted">{html.escape((r.get("summary") or "")[:160])}</div></a>')
    body = (f'<h1>Ring activity</h1><p class="small muted">Most recent first · refreshes every 30s · '
            f'tap a command for its step-by-step trace</p>' + ("".join(items) or "<p>No activity yet.</p>"))
    return _page("Ring activity", body, refresh=30)


def _value(v) -> str:
    if isinstance(v, (dict, list)):
        return f"<pre>{html.escape(json.dumps(v, indent=2, ensure_ascii=False, default=str))}</pre>"
    s = "" if v is None else str(v)
    if "\n" in s or len(s) > 120:
        return f"<pre>{html.escape(s)}</pre>"
    return html.escape(s)


def detail_page(base: str, delivery: dict | None, steps: list[dict]) -> str:
    if not steps and not delivery:
        return _page("Not found", f'<p><a href="{base}">← Activity</a></p><p>No trace for this delivery.</p>')
    t0 = steps[0]["ts"] if steps else 0
    out = [f'<p class="small"><a href="{base}">← Activity</a></p>']
    if delivery:
        out.append(f'<h1>{html.escape(delivery.get("transcript") or "(no transcript)")}</h1>'
                   f'<p class="small muted">{_badge(delivery.get("state"))} {html.escape(delivery.get("route") or "")} · '
                   f'{_when(delivery["first_seen"])} · delivery {html.escape(delivery["delivery_id"])}</p>')
    for i, st in enumerate(steps, 1):
        name, hint = PHASES.get(st["phase"], (st["phase"], ""))
        rows = "".join(f"<dt>{html.escape(str(k))}</dt><dd>{_value(v)}</dd>" for k, v in st["data"].items())
        out.append(f'<div class="step"><span class="when">+{st["ts"] - t0:.2f}s</span>'
                   f'<h3>{i}. {html.escape(name)}</h3>'
                   + (f'<div class="small muted">{html.escape(hint)}</div>' if hint else "")
                   + (f"<dl>{rows}</dl>" if rows else "") + "</div>")
    return _page(delivery.get("transcript", "Trace")[:60] if delivery else "Trace", "".join(out))
