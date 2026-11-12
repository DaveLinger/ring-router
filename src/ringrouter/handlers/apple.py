"""Notes, shopping lists, reminders (timed and geofenced) and calendar events."""

from __future__ import annotations

import json
import logging
import re
import subprocess

from ..config import PROJECT_DIR
from ..prompts import prompt
from ..router import Route
from ..text import capitalize_first
from .context import Context, Result

log = logging.getLogger(__name__)


NOTE_SCRIPT = """on run argv
  set noteTitle to item 1 of argv
  set noteBody to item 2 of argv
  set folderName to item 3 of argv
  tell application "Notes"
    if not (exists folder folderName) then make new folder with properties {name:folderName}
    make new note at folder folderName with properties {name:noteTitle, body:noteBody}
  end tell
end run"""


def applescript_note(title: str, body_html: str, folder: str) -> None:
    """Create an Apple Note directly (no 2,000-character cap, unlike mcp-macos).

    Text goes in as osascript arguments, never spliced into the script, so model output
    can't inject AppleScript.
    """
    import html as _html  # noqa: PLC0415
    full = f"<h1>{_html.escape(title)}</h1>\n{body_html}"
    subprocess.run(["/usr/bin/osascript", "-e", NOTE_SCRIPT, title, full, folder],
                   check=True, capture_output=True, text=True, timeout=60)


# ------------------------------------------------------------- Apple apps ----

def h_note(r: Route, ctx: Context, folder: str | None = None, route_name: str = "note") -> Result:
    folder = folder or r.params.get("folder", "Notes")
    try:
        title = ctx.fm.title(r.body)
    except Exception as e:
        log.warning("fm title failed: %s", e)
        title = " ".join(r.body.split()[:6])
    title = title.strip().strip('"')[:200]
    if ctx.dry(route_name):
        ctx.notify("Dry run", f"Would create note “{title}” in {folder}")
        return Result("dry_run", f"DRY RUN: would create note {title!r} in {folder!r}: {r.body[:2000]!r}")
    ctx.mcp_call("notes_items", {"action": "create", "title": title, "body": r.body[:2000], "folder": folder})
    ctx.notify("Note created", f"{title} ({folder})")
    return Result("done", f"note {title!r} in {folder!r}")


def h_remember(r: Route, ctx: Context) -> Result:
    return h_note(r, ctx, folder=r.params.get("folder", "Remember"), route_name="remember")


def h_list_add(r: Route, ctx: Context) -> Result:
    lst = ctx.cfg.raw["lists"][r.params["list"]]
    rl = lst["reminders_list"]
    try:
        items = ctx.fm.split_items(r.body) or [r.body]
    except Exception as e:
        log.warning("fm split failed, adding as one item: %s", e)
        items = [r.body]
    items = [i[:1].upper() + i[1:] for i in items]
    if ctx.dry("list_add"):
        ctx.notify("Dry run", f"Would add to {rl}: {', '.join(items)}")
        return Result("dry_run", f"DRY RUN: would add to {rl!r}: {items}")
    for it in items:
        ctx.mcp_call("reminders_tasks", {"action": "create", "title": it, "targetList": rl})
    ctx.notify(f"Added to {rl}", ", ".join(items))
    return Result("done", f"added {items} to {rl!r}")


REMINDER_SCHEMA = {"title": "Reminder", "type": "object", "additionalProperties": False,
                   "required": ["title"], "x-order": ["title", "due"],
                   "properties": {"title": {"type": "string", "description": "What to be reminded of, short"},
                                  "due": {"type": "string", "description":
                                          "Local due time 'YYYY-MM-DD HH:mm:ss', or empty if none was said"}}}


MY_CARD_SCRIPT = """tell application "Contacts"
  set out to ""
  repeat with a in (addresses of my card)
    set out to out & (label of a) & "|" & (street of a) & "|" & (city of a) & "|" & (state of a) & "|" & (zip of a) & linefeed
  end repeat
  return out
end tell"""

GEO_PATTERNS = [  # (regex on the reminder text, place, proximity)
    (r"\bwhen i (?:get|arrive|come|am|'m)\s+(?:back\s+)?home\b|\bwhen i get back\b|\bwhen i arrive\b", "home", "enter"),
    (r"\bwhen i (?:leave|head out|walk out)(?:\s+(?:the\s+)?(?:home|house))?\b", "home", "leave"),
]


def my_card_address() -> str:
    """Home address from the Contacts 'My Card' (label 'home' preferred). Read fresh every time."""
    out = subprocess.run(["/usr/bin/osascript", "-e", MY_CARD_SCRIPT], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    rows = [line.split("|") for line in out.splitlines() if line.count("|") == 4]
    if not rows:
        raise RuntimeError("no address on your My Card in Contacts")
    rows.sort(key=lambda r: 0 if "home" in r[0].lower() else 1)
    label, street, city, state, zip_ = (x.strip() for x in rows[0])
    return ", ".join(p for p in (street, city, f"{state} {zip_}".strip()) if p and p != "missing value")


def parse_geo(body: str) -> tuple[str, str, str] | None:
    """'when I get home to take out the trash' -> ('Take out the trash', 'home', 'enter')."""
    for pat, place, prox in GEO_PATTERNS:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            title = (body[:m.start()] + " " + body[m.end():]).strip(" ,.")
            title = re.sub(r"^(?:to|that|about)\s+", "", title, flags=re.IGNORECASE).strip()
            return capitalize_first(title), place, prox
    return None


def h_geo_reminder(r: Route, ctx: Context) -> Result:
    parsed = parse_geo(r.body)
    if not parsed or not parsed[0]:
        msg = "Only “when I get home” / “when I leave” reminders are supported."
        ctx.notify("Not done", msg)
        return Result("refused", msg)
    title, place, prox = parsed
    try:
        address = ctx.address_lookup()
    except Exception as e:
        ctx.notify("Not done", f"Couldn't read your home address from Contacts: {e}")
        return Result("failed", f"my card address: {e}")
    when = "arrive home" if prox == "enter" else "leave home"
    ctx.trace("geo_reminder", title=title, place=place, proximity=prox, address_source="Contacts My Card")
    if ctx.dry("reminder"):
        ctx.notify("Dry run", f"Would remind you to “{title}” when you {when}")
        return Result("dry_run", f"DRY RUN: would create {prox} geofence reminder {title!r}")
    helper = ctx.cfg.section("reminders").get("georeminder", str(PROJECT_DIR / "build" / "georeminder"))
    cmd = [helper, "--title", title, "--address", address, "--proximity", prox, "--place", "Home",
           "--radius", str(ctx.cfg.section("reminders").get("geofence_radius_m", 100))]
    ctx.trace("command", target="georeminder (EventKit)", title=title, proximity=prox, place="Home (My Card)")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        out = {"ok": False, "error": (p.stderr or p.stdout or "no output").strip()[:300]}
    ctx.trace("command_result", ok=out.get("ok"), **{k: v for k, v in out.items() if k not in ("ok", "lat", "lon")})
    if not out.get("ok"):
        ctx.notify("Not done", f"Location reminder failed: {out.get('error')}")
        return Result("failed", f"georeminder: {out.get('error')}", 422)
    ctx.notify("Reminder set", f"{title}: when you {when}")
    return Result("done", f"geofence reminder {title!r} ({prox} home, list {out.get('list')!r})")


def h_reminder(r: Route, ctx: Context) -> Result:
    if r.params.get("location_trigger"):
        return h_geo_reminder(r, ctx)
    try:
        data = ctx.fm.extract(r.body, prompt("reminder_extract"), REMINDER_SCHEMA, now=ctx.spoken_at())
    except Exception as e:
        log.warning("fm extract failed: %s", e)
        data = {"title": r.body}
    title, due = data.get("title") or r.body, (data.get("due") or "").strip()
    when = f" due {due}" if due else ""
    if ctx.dry("reminder"):
        ctx.notify("Dry run", f"Would create reminder “{title}”{when}")
        return Result("dry_run", f"DRY RUN: would create reminder {title!r}{when}")
    args = {"action": "create", "title": title}
    if due:
        args["dueDate"] = due
    ctx.mcp_call("reminders_tasks", args)
    ctx.notify("Reminder set", f"{title}{when}")
    return Result("done", f"reminder {title!r}{when}")


EVENT_SCHEMA = {"title": "Event", "type": "object", "additionalProperties": False,
                "required": ["title", "start"], "x-order": ["title", "start", "end", "all_day"],
                "properties": {"title": {"type": "string"},
                               "start": {"type": "string", "description": "Local 'YYYY-MM-DD HH:mm:ss'"},
                               "end": {"type": "string", "description": "Local 'YYYY-MM-DD HH:mm:ss'; empty if not said"},
                               "all_day": {"type": "boolean"}}}


def default_end(start: str, all_day: bool, minutes: int = 60) -> str:
    """End time for an event with none given: start + `minutes`, or the same day if all-day."""
    from datetime import datetime, timedelta  # noqa: PLC0415
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(start.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        return ""
    if all_day or fmt == "%Y-%m-%d":
        return dt.strftime("%Y-%m-%d")
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def h_calendar(r: Route, ctx: Context) -> Result:
    cal = r.params.get("calendar")
    try:
        data = ctx.fm.extract(r.body, prompt("calendar_extract"), EVENT_SCHEMA, now=ctx.spoken_at())
    except Exception as e:
        msg = f"Couldn't work out the event time: {e}"
        ctx.notify("Not done", msg)
        return Result("failed", msg)
    if not (data.get("end") or "").strip():   # mcp-macos requires an end time
        data["end"] = default_end(data.get("start", ""), bool(data.get("all_day")),
                                  int(ctx.cfg.section("calendars").get("default_minutes", 60)))
    desc = f"“{data.get('title')}” {data.get('start')}" + (f" to {data['end']}" if data.get("end") else "")
    if ctx.dry("calendar") or not cal or cal == "CHANGE-ME":
        ctx.notify("Dry run", f"Would add {desc} to {cal}")
        return Result("dry_run", f"DRY RUN: would add {desc} to calendar {cal!r}")
    args = {"action": "create", "title": data["title"], "startDate": data["start"], "targetCalendar": cal}
    if data.get("end"):
        args["endDate"] = data["end"]
    if data.get("all_day"):
        args["isAllDay"] = True
    ctx.mcp_call("calendar_events", args)
    ctx.notify("Event added", f"{desc} ({cal})")
    return Result("done", f"event {desc} on {cal!r}")
