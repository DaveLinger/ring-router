"""Notes, shopping lists, reminders (timed and geofenced) and calendar events."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime

from .. import places, when
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

NOTE_MCP_LIMIT = 2000    # characters mcp-macos accepts in a note body


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
    cut = ""
    if len(r.body) > NOTE_MCP_LIMIT:    # mcp-macos caps a note body; the AppleScript writer doesn't
        try:
            import html as _html  # noqa: PLC0415
            ctx.notes_writer(title, "".join(f"<p>{_html.escape(p)}</p>" for p in r.body.split("\n") if p.strip()),
                             folder)
            ctx.notify("Note created", f"{title} ({folder})")
            return Result("done", f"note {title!r} in {folder!r} ({len(r.body)} characters)")
        except Exception as e:
            log.warning("long note via AppleScript failed (%s); saving the first %d characters", e, NOTE_MCP_LIMIT)
            cut = f", cut at {NOTE_MCP_LIMIT:,} of {len(r.body):,} characters"
    ctx.mcp_call("notes_items", {"action": "create", "title": title, "body": r.body[:NOTE_MCP_LIMIT], "folder": folder})
    ctx.notify("Note created", f"{title} ({folder}{cut})")
    return Result("done", f"note {title!r} in {folder!r}{cut}")


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
    added, failed, skipped = [], [], []
    for n, it in enumerate(items):
        try:
            ctx.mcp_call("reminders_tasks", {"action": "create", "title": it, "targetList": rl})
            added.append(it)
        except TimeoutError as e:       # Reminders isn't answering: don't wait a minute per item
            failed.append(f"{it} (no reply; it may have been added)")
            skipped = items[n + 1:]
            log.warning("list add timed out on %r: %s", it, e)
            break
        except Exception as e:
            failed.append(f"{it} ({e})")
    if not failed:
        ctx.notify(f"Added to {rl}", ", ".join(items))
        return Result("done", f"added {items} to {rl!r}")
    parts = ([f"Added: {', '.join(added)}."] if added else []) + [f"Not added: {'; '.join(failed)}."] \
        + ([f"Not tried: {', '.join(skipped)}."] if skipped else [])
    ctx.notify(f"{rl}: partly added" if added else f"{rl}: not added", " ".join(parts))
    # "failed" means nothing happened (safe to retry); anything else must not be run again as a whole
    state = "failed" if not added and not skipped else "partial" if not skipped else "unknown"
    return Result(state, f"list {rl!r}: " + " ".join(parts), 422)


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

def my_card_address(label: str = "home") -> str:
    """An address from the Contacts 'My Card', read fresh every time. For "home" a home-labelled
    address is preferred and any address will do; any other label must exist on the card."""
    out = subprocess.run(["/usr/bin/osascript", "-e", MY_CARD_SCRIPT], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    rows = [line.split("|") for line in out.splitlines() if line.count("|") == 4]
    if not rows:
        raise RuntimeError("no address on your My Card in Contacts")
    want = label.lower()
    if want != "home":
        rows = [r for r in rows if want in r[0].lower()]
        if not rows:
            raise RuntimeError(f"no {label!r} address on your My Card in Contacts")
    rows.sort(key=lambda r: 0 if want in r[0].lower() else 1)
    _label, street, city, state, zip_ = (x.strip() for x in rows[0])
    return ", ".join(p for p in (street, city, f"{state} {zip_}".strip()) if p and p != "missing value")


def parse_geo(body: str, raw: dict | None = None) -> tuple[str, str, str] | None:
    """'when I get home to take out the trash' -> ('Take out the trash', 'home', 'enter').
    The place is a [places] key, or None when the place named isn't configured."""
    g = places.parse(body, raw or {})
    return (g.title, g.place.key if g.place else None, g.proximity) if g else None


def h_geo_reminder(r: Route, ctx: Context) -> Result:
    g = places.parse(r.body, ctx.cfg.raw)
    if not g or not g.title:
        msg = "Couldn't tell what to remind you about, or where. Nothing set."
        ctx.notify("Not done", msg)
        return Result("refused", msg)
    known = ", ".join(p.name for p in places.places(ctx.cfg.raw))
    if g.place is None and g.heard == "here":
        msg = (f"I don't have access to your location, so I can't tell where \u201chere\u201d is. No reminder "
               f"was set. Name the place instead (known places: {known}).")
        ctx.notify("Not done", msg)
        return Result("refused", "location reminder for 'here': no access to your location")
    if g.place is None:
        msg = (f"Don't know where “{g.heard}” is, so no reminder was set. Known places: {known} "
               f"(add others under [places] in config.toml).")
        ctx.notify("Not done", msg)
        return Result("refused", f"unknown place {g.heard!r}")
    p, when_ = g.place, places.describe(g)
    rc = ctx.cfg.section("reminders")
    radius = p.radius_m or rc.get("geofence_radius_m", 100)
    if p.lat is not None and p.lon is not None:
        where, source = ["--lat", str(p.lat), "--lon", str(p.lon)], "coordinates in config"
    elif p.address:
        where, source = ["--address", p.address], "address in config"
    else:
        label = p.contact_label or "home"
        try:
            address = ctx.address_lookup() if label == "home" else ctx.address_lookup(label)
        except Exception as e:
            ctx.notify("Not done", f"Couldn't read the {p.name} address from your My Card in Contacts: {e}")
            return Result("failed", f"my card address ({label}): {e}")
        where, source = ["--address", address], f"Contacts My Card ({label})"
    ctx.trace("geo_reminder", title=g.title, place=p.key, proximity=g.proximity, address_source=source)
    if ctx.dry("reminder"):
        ctx.notify("Dry run", f"Would remind you to “{g.title}” when you {when_}")
        return Result("dry_run", f"DRY RUN: would create {g.proximity} geofence reminder {g.title!r} ({p.key})")
    helper = rc.get("georeminder", str(PROJECT_DIR / "build" / "georeminder"))
    cmd = [helper, "--title", g.title, *where, "--proximity", g.proximity, "--place", p.name,
           "--radius", str(radius)]
    ctx.trace("command", target="georeminder (EventKit)", title=g.title, proximity=g.proximity, place=f"{p.name} ({source})")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        out = {"ok": False, "error": (proc.stderr or proc.stdout or "no output").strip()[:300]}
    ctx.trace("command_result", ok=out.get("ok"), **{k: v for k, v in out.items() if k not in ("ok", "lat", "lon")})
    if not out.get("ok"):
        ctx.notify("Not done", f"Location reminder failed: {out.get('error')}")
        return Result("failed", f"georeminder: {out.get('error')}", 422)
    ctx.notify("Reminder set", f"{g.title}: when you {when_}")
    return Result("done", f"geofence reminder {g.title!r} ({g.proximity} {p.key}, list {out.get('list')!r})")


def _title(model_title, body: str) -> str:
    """The model's title, tidied ("to buy stamps" -> "Buy stamps"); the spoken words if it gave none."""
    t = (model_title or "").strip().strip('"') or body.strip()
    return capitalize_first(re.sub(r"^(?:to|that)\s+", "", t, flags=re.IGNORECASE))


def _when_text(w: when.When, now: datetime, end: datetime | None = None) -> str:
    """'Tue Nov 17, 3:00 PM' (+ ' to 5:00 PM') (+ ' (check the date)')."""
    out = when.human(w.start, all_day=w.all_day, now=now)
    if end and not w.all_day:
        out += " to " + (end.strftime("%-I:%M %p") if end.date() == w.start.date() else when.human(end, now=now))
    return out + (f" ({'; '.join(w.notes)})" if w.notes else "")


def h_reminder(r: Route, ctx: Context) -> Result:
    if r.params.get("location_trigger"):
        return h_geo_reminder(r, ctx)
    now = ctx.spoken_at() or datetime.now()
    try:
        data = ctx.fm.extract(r.body, prompt("reminder_extract"), REMINDER_SCHEMA, now=now)
    except Exception as e:      # the title falls back to the spoken words; the time is worked out in code
        log.warning("fm extract failed: %s", e)
        data = {}
    title = _title(data.get("title"), r.body)
    # The model supplies the title; the due time is settled in code (the model's date is only a fallback).
    w = when.settle(r.body, now, model_start=data.get("due") or "")
    ctx.trace("when", said=r.body, model=data.get("due") or "", settled=w.stamp() if w.start else "",
              all_day=w.all_day, notes=w.notes)
    if w.start:
        shown, summary = f"{title}: {_when_text(w, now)}", f"reminder {title!r} due {w.stamp()}"
    else:                       # nothing said about when, or said in a way nothing could read
        shown = title + (f" (no time set: {w.notes[0]})" if w.notes else "")
        summary = f"reminder {title!r}" + (f" with no due time ({w.notes[0]})" if w.notes else "")
    if ctx.dry("reminder"):
        ctx.notify("Dry run", f"Would create reminder “{shown}”")
        return Result("dry_run", f"DRY RUN: would create {summary}")
    args = {"action": "create", "title": title}
    if w.start:
        args["dueDate"] = w.stamp()
    ctx.mcp_call("reminders_tasks", args)
    ctx.notify("Reminder set" if w.start or not w.notes else "Reminder set, no time", shown)
    return Result("done", summary)


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
    now = ctx.spoken_at() or datetime.now()
    try:
        data = ctx.fm.extract(r.body, prompt("calendar_extract"), EVENT_SCHEMA, now=now)
    except Exception as e:
        log.warning("fm extract failed: %s", e)
        data = {}
    title = _title(data.get("title"), r.body)
    w = when.settle(r.body, now, model_start=data.get("start") or "", model_end=data.get("end") or "", event=True)
    ctx.trace("when", said=r.body, model=data.get("start") or "", settled=w.stamp() if w.start else "",
              end=w.stamp(w.end) if w.end else "", all_day=w.all_day, notes=w.notes)
    if not w.start:
        msg = f"Couldn't work out when “{r.body}” is. Nothing added."
        ctx.notify("Not done", msg)
        return Result("failed", msg)
    start = w.stamp()
    end = w.stamp(w.end) if w.end else default_end(start, w.all_day,       # mcp-macos requires an end
                                                   int(ctx.cfg.section("calendars").get("default_minutes", 60)))
    shown = f"“{title}” {_when_text(w, now, w.end)}"
    desc = f"{title!r} {start} to {end}" + (" (all day)" if w.all_day else "")
    if ctx.dry("calendar") or not cal or cal == "CHANGE-ME":
        ctx.notify("Dry run", f"Would add {shown} to {cal}")
        return Result("dry_run", f"DRY RUN: would add {desc} to calendar {cal!r}")
    args = {"action": "create", "title": title, "startDate": start, "endDate": end, "targetCalendar": cal}
    if w.all_day:
        args["isAllDay"] = True
    ctx.mcp_call("calendar_events", args)
    ctx.notify("Event added", f"{shown} ({cal})")
    return Result("done", f"event {desc} on {cal!r}")
