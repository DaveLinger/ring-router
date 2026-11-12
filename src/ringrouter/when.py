"""When something is meant to happen, worked out in code from the words that were said.

The on-device model is good at pulling a title out of a sentence and poor at calendar arithmetic:
asked for "Tuesday" it has returned a Saturday, and "in 20 minutes" came back as 29. So the common
ways of saying when are computed here, and the model's answer is kept only for what this module
doesn't recognise (a holiday, "the end of the month"), and flagged so the owner checks it:

    today, tonight, tomorrow, the day after tomorrow
    Tuesday / on Tuesday / this Tuesday      the coming one (today if it is Tuesday)
    next Tuesday                             the Tuesday of next week (weeks start on Monday)
    December 3rd, the 3rd of December, the 20th, a week from Friday
    3pm, 4:30 pm, at 5, at five, 10:30, half past three, quarter to five, noon, midnight,
    morning / afternoon / evening / tonight
    from 6 to 8pm, 9am until 1pm, for two hours
    in 20 minutes, in an hour and a half, in 3 days, in two weeks

Pure functions of (text, now): no model, no clock, no side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = {m: i + 1 for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"))}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())} | {"sept": 9})
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
           "thirty": 30, "forty": 40, "forty-five": 45, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
DAY_PARTS = {"morning": time(9), "lunchtime": time(12), "afternoon": time(15), "evening": time(18),
             "tonight": time(20), "night": time(21)}
UNITS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 7 * 86400}

_NUM = r"\d+|" + "|".join(sorted(NUMBERS, key=len, reverse=True))
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_HOUR_WORD = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_MERIDIEM = r"a\.?\s?m\b\.?|p\.?\s?m\b\.?"


@dataclass
class When:
    start: datetime | None = None
    end: datetime | None = None          # only when an end or a length was said
    all_day: bool = False                # a day with no time of day
    notes: list[str] = field(default_factory=list)   # anything the owner should be told

    def stamp(self, dt: datetime | None = None) -> str:
        """'YYYY-MM-DD HH:mm:ss' (or just the date when all-day), as mcp-macos wants it."""
        dt = dt or self.start
        return dt.strftime("%Y-%m-%d") if self.all_day else dt.strftime("%Y-%m-%d %H:%M:%S")


def human(dt: datetime, *, all_day: bool = False, now: datetime | None = None) -> str:
    """'Tue Nov 17, 3:00 PM': the weekday is there so a wrong date is obvious at a glance."""
    out = dt.strftime("%a %b %-d")
    if now and dt.year != now.year:
        out += dt.strftime(" %Y")
    return out if all_day else out + dt.strftime(", %-I:%M %p")


def parse_stamp(value: str | None) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime((value or "").strip(), fmt)
        except ValueError:
            continue
    return None


def _number(word: str) -> float:
    return float(word) if word.isdigit() else float(NUMBERS[word.lower()])


def _words_to_digits(text: str) -> str:
    """'at five thirty' -> 'at 5:30', 'seven pm' -> '7 pm': only where a number is clearly a time."""
    minutes = {"thirty": "30", "fifteen": "15", "forty five": "45", "forty-five": "45", "o'clock": "00", "oclock": "00"}

    def past_to(m: re.Match) -> str:      # "half past three" -> "3:30", "quarter to five" -> "4:45"
        hour = int(m.group(3)) if m.group(3).isdigit() else int(_number(m.group(3)))
        mins = 30 if m.group(1).lower() == "half" else 15
        if m.group(2).lower() == "to":
            hour, mins = (hour - 2) % 12 + 1, 60 - mins
        return f"{hour}:{mins:02d}"

    text = re.sub(rf"\b(half|quarter)\s+(past|to)\s+({_HOUR_WORD}|\d{{1,2}})\b", past_to, text, flags=re.IGNORECASE)

    def repl(m: re.Match) -> str:
        hour = str(int(_number(m.group("h"))))
        mins = minutes.get((m.group("m") or "").lower())
        return f"{m.group('pre') or ''}{hour}{':' + mins if mins else ''}"

    pat = (rf"(?P<pre>\b(?:at|from|to|until|till|til|by|through)(?:\s+(?:about|around))?\s+)?\b(?P<h>{_HOUR_WORD})\b"
           rf"(?:\s+(?P<m>thirty|fifteen|forty[- ]five|o'?clock))?"
           rf"(?=\s*(?:{_MERIDIEM})|\s|$|[,.;!?])")

    def guarded(m: re.Match) -> str:      # a bare number word is a time only after "at"/"from"/... or before am/pm
        after = text[m.end():]
        if m.group("pre") or m.group("m") or re.match(rf"\s*(?:{_MERIDIEM})", after, re.IGNORECASE):
            return repl(m)
        return m.group(0)

    return re.sub(pat, guarded, text, flags=re.IGNORECASE)


# ------------------------------------------------------------------ pieces ----

def offset(text: str) -> tuple[timedelta, str] | None:
    """'in 20 minutes' / 'in an hour and a half' / 'in 3 days' -> (how long, unit)."""
    t = text.lower()
    if re.search(r"\bin\s+(?:about\s+)?half\s+an\s+hour\b", t):
        return timedelta(minutes=30), "minute"
    m = re.search(rf"\bin\s+(?:about\s+)?({_NUM})\s+(?:(and\s+a\s+half)\s+)?(minute|min|hour|hr|day|week)s?\b"
                  rf"(\s+and\s+a\s+half)?", t)
    if not m:
        return None
    n = _number(m.group(1)) + (0.5 if m.group(2) or m.group(4) else 0)
    unit = {"min": "minute", "hr": "hour"}.get(m.group(3), m.group(3))
    return timedelta(seconds=n * UNITS[unit]), unit


def named_day(text: str, now: datetime) -> tuple[date, str] | None:
    """The calendar day the words name, and how: 'today' | 'weekday' | 'date'. Earliest mention wins."""
    t, today = text.lower(), now.date()
    found: list[tuple[int, date, str]] = []

    def add(m: re.Match | None, d: date | None, kind: str) -> None:
        if m and d:
            found.append((m.start(), d, kind))

    m = re.search(rf"\b({_NUM})\s+weeks?\s+from\s+(today|now|tomorrow)\b", t)
    if m:
        return today + timedelta(weeks=_number(m.group(1)), days=1 if m.group(2) == "tomorrow" else 0), "date"
    add(re.search(r"\bday\s+after\s+tomorrow\b", t), today + timedelta(days=2), "date")
    m = re.search(r"\btomorrow\b", t)
    if m and not re.search(r"\bday\s+after\s+tomorrow\b", t):
        add(m, today + timedelta(days=1), "date")
    add(re.search(r"\b(today|tonight|this\s+(morning|afternoon|evening))\b", t), today, "today")

    for m in re.finditer(rf"\b(?:(this|next|on)\s+(?:coming\s+)?)?({'|'.join(WEEKDAYS)})\b", t):
        wd = WEEKDAYS.index(m.group(2))
        before = t[:m.start()]
        if re.search(r"\b(after|before|following)\s+$", before):
            return None                           # "the day after Tuesday": not ours to compute
        weeks = re.search(rf"\b({_NUM})\s+weeks?\s+from\s+$", before)
        if weeks or re.search(r"\bfrom\s+$", before):
            if not weeks:
                return None
            d = today + timedelta(days=(wd - today.weekday()) % 7) + timedelta(weeks=_number(weeks.group(1)))
            found.append((weeks.start(), d, "date"))
            continue
        if m.group(1) == "next":
            d = today - timedelta(days=today.weekday()) + timedelta(days=7 + wd)
        else:
            d = today + timedelta(days=(wd - today.weekday()) % 7)
        add(m, d, "weekday")

    def month_day(month: int, day: int) -> date | None:
        for year in (today.year, today.year + 1):
            try:
                d = date(year, month, day)
            except ValueError:
                return None
            if d >= today:
                return d
        return None

    m = re.search(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    add(m, m and month_day(MONTHS[m.group(1)], int(m.group(2))), "date")
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+of\s+({_MONTH})\b", t)
    add(m, m and month_day(MONTHS[m.group(2)], int(m.group(1))), "date")
    m = re.search(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b(?!\s+of\b)", t)
    if m:
        day, d = int(m.group(1)), None
        for k in range(0, 3):                     # this month if still ahead, else the next that has it
            y, mo = divmod(today.month - 1 + k, 12)
            try:
                d = date(today.year + y, mo + 1, day)
            except ValueError:
                continue
            if d >= today:
                break
            d = None
        add(m, d, "date")

    if not found:
        return None
    _, d, kind = min(found)
    return d, kind


def clock_times(text: str) -> list[dict]:
    """Times of day in the text, in order: {'pos', 'end', 'hour' (as said), 'minute', 'meridiem' am|pm|None}."""
    t = _words_to_digits(text).lower()
    out: list[dict] = []
    for m in re.finditer(r"\b(noon|midday|midnight)\b", t):
        out.append({"pos": m.start(), "end": m.end(), "hour": 0 if m.group(1) == "midnight" else 12, "minute": 0,
                    "meridiem": "am" if m.group(1) == "midnight" else "pm", "fixed": True})
    pat = (rf"(?P<lead>\b(?:at|from|by|until|till|til)(?:\s+(?:about|around))?\s+)?"
           rf"(?P<range>\b(?:to|through)\s+|[-–—]\s*)?"
           rf"\b(?P<h>\d{{1,2}})(?::(?P<m>\d{{2}}))?\s*(?P<ap>{_MERIDIEM})?")
    for m in re.finditer(pat, t):
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        ap = (m.group("ap") or "").replace(".", "").replace(" ", "")[:2] or None
        # A bare number is a time only after "at"/"from"/"by"/"until", or as the far end of a range
        # that began with a time ("6 to 8"). "The 20th", "level 3" and "about 5 things" are not times.
        ranged = bool(m.group("range")) and any(c["end"] <= m.start() and not t[c["end"]:m.start()].strip() for c in out)
        ranged = ranged or bool(re.match(                      # "9-9:30am", "6 to 8pm" with no "from"
            rf"\s*(?:to|until|till|til|through|[-–—])\s*\d{{1,2}}(?::\d{{2}}\b|(?::\d{{2}})?\s*(?:{_MERIDIEM}))", t[m.end():]))
        if not (ap or m.group("m") or m.group("lead") or ranged) or hour > 23 or minute > 59 or (ap and hour > 12):
            continue
        if not ap and re.match(r"\s*(?:st|nd|rd|th|minutes?|mins?|hours?|hrs?|days?|weeks?|percent|%|dollars?)\b", t[m.end():]):
            continue            # "at 20 minutes past", "by 3 percent", "until the 3rd"
        out.append({"pos": m.start("h"), "end": m.end(), "hour": hour, "minute": minute, "meridiem": ap})
    return sorted(out, key=lambda c: c["pos"])


def day_part(text: str) -> tuple[str, time] | None:
    m = re.search(r"\b(" + "|".join(DAY_PARTS) + r")\b", text.lower())
    return (m.group(1), DAY_PARTS[m.group(1)]) if m else None


def length(text: str) -> timedelta | None:
    """'for two hours' / 'for 90 minutes' / 'for an hour and a half' / 'for half an hour'."""
    t = text.lower()
    if re.search(r"\bfor\s+half\s+an\s+hour\b", t):
        return timedelta(minutes=30)
    m = re.search(rf"\bfor\s+({_NUM})\s+(?:(and\s+a\s+half)\s+)?(minute|min|hour|hr)s?\b(\s+and\s+a\s+half)?", t)
    if not m:
        return None
    n = _number(m.group(1)) + (0.5 if m.group(2) or m.group(4) else 0)
    return timedelta(seconds=n * UNITS[{"min": "minute", "hr": "hour"}.get(m.group(3), m.group(3))])


def hints_at_when(text: str) -> bool:
    """Do the words mention a time or day at all (whether or not we can compute it)?"""
    t = _words_to_digits(text).lower()
    return bool(offset(t) or clock_times(t) or day_part(t) or re.search(
        rf"\b(today|tonight|tomorrow|{'|'.join(WEEKDAYS)}|{_MONTH}\s+\d|\d(st|nd|rd|th)\b|next\s+(week|month|year)|"
        rf"this\s+(week|weekend|month)|weekend|later|o'?clock|half\s+past|quarter\s+(to|past)|end\s+of)", t))


# Words that suggest a time of day was said, even if not in a form parsed above.
_TIME_HINT = re.compile(rf"\d|\b(?:{_HOUR_WORD})\b|o'?clock|\bhalf\b|\bquarter\b|sunrise|sunset|bedtime|dinnertime",
                        re.IGNORECASE)


def _time_hinted(text: str) -> bool:
    """Might a time of day have been said? (The day of the month in "December 3rd" isn't one.)"""
    t = re.sub(rf"\b\d{{1,2}}(?:st|nd|rd|th)\b|\b(?:{_MONTH})\.?\s+\d{{1,2}}\b", " ", text.lower())
    return bool(_TIME_HINT.search(t))


def _time_of(c: dict, *, model: datetime | None, part: str | None, later: dict | None = None) -> time:
    """24-hour time for a spoken clock time; am/pm settled by, in order: what was said, a later time
    in the same range ('6 to 8pm'), the part of day, the model's reading, then habit (1-6 -> pm)."""
    h, mnt, ap = c["hour"], c["minute"], c["meridiem"]
    if c.get("fixed") or h > 12 or h == 0:
        return time(h % 24, mnt)
    if not ap and later and later["meridiem"]:
        ap = later["meridiem"]                      # "6 to 8pm": both in the evening...
        if (h % 12) > (later["hour"] % 12):
            ap = "am" if ap == "pm" else "pm"       # ...but "11 to 1pm" starts in the morning
    if not ap and part:
        ap = "am" if part == "morning" else "pm"
    if not ap and model and model.hour % 12 == h % 12 and model.minute == mnt and (model.hour, model.minute) != (0, 0):
        ap = "pm" if model.hour >= 12 else "am"
    if not ap:
        ap = "pm" if h == 12 or 1 <= h <= 6 else "am"
    return time((h % 12) + (12 if ap == "pm" else 0), mnt)


# ------------------------------------------------------------------ settle ----

def settle(text: str, now: datetime, *, model_start: str = "", model_end: str = "", event: bool = False) -> When:
    """When `text` says something should happen. `model_*` are the on-device model's answers, used
    for whatever isn't recognised here. A day with no time of day is all-day (for a reminder: due
    that day, no alert time). Ends and lengths are only looked for when event=True."""
    now = now.replace(second=0, microsecond=0, tzinfo=None)
    w = When()
    m_start, m_end = parse_stamp(model_start), parse_stamp(model_end)
    off = offset(text)
    if off and off[1] in ("minute", "hour"):                   # "in 20 minutes": no calendar involved
        w.start = now + off[0]
        w.end = w.start + length(text) if event and length(text) else None
        return w

    named = named_day(text, now)
    day, how = named if named else (None, "")
    if off:                                                    # "in 3 days", "in two weeks"
        day, how = now.date() + off[0], "date"
    times, part = clock_times(text), day_part(text)

    # time of day: said as a clock time, else a part of the day, else whatever the model heard
    tod: time | None = None
    if times:
        later = times[1] if len(times) > 1 and _joined(text, times[0], times[1]) else None
        tod = _time_of(times[0], model=m_start, part=part and part[0], later=later)
    elif part:
        tod = part[1]
    elif (m_start and len(model_start.strip()) > 10 and _time_hinted(text)
          and (m_start.hour, m_start.minute) not in ((0, 0), (now.hour, now.minute))):
        tod = m_start.time()        # a time said in words we don't parse ("ten past three"), as the model read it

    # the day: said in a way we compute, else a future date from the model, else today if a time was said
    if day is None and m_start and m_start.date() > now.date():
        day, how = m_start.date(), "model"
        w.notes.append("check the date")           # worked out by the model, which is poor at calendars
    if day is None and tod is None:
        if hints_at_when(text):
            w.notes.append("couldn't work out when")
        return w
    implicit_today = day is None
    day = day or now.date()

    w.all_day = tod is None
    w.start = datetime.combine(day, tod or time(0))
    if w.start <= now and not w.all_day:
        if implicit_today:
            w.start += timedelta(days=1)                       # "at 5" said at six: tomorrow
        elif how == "weekday":
            w.start += timedelta(days=7)                       # "Thursday at 9" said on Thursday afternoon
        else:
            w.notes.append("that time has already passed")
    if w.all_day and w.start.date() < now.date():
        w.notes.append("that day has already passed")

    if event and not w.all_day:
        if len(times) > 1 and _joined(text, times[0], times[1]):
            end = datetime.combine(w.start.date(), _time_of(times[1], model=m_end, part=part and part[0]))
            if times[1]["meridiem"] is None and end <= w.start and end + timedelta(hours=12) > w.start:
                end += timedelta(hours=12)                     # "from 10 to 1": one in the afternoon
            w.end = end if end > w.start else end + timedelta(days=1)
        elif length(text):
            w.end = w.start + length(text)
        elif m_end and m_end > w.start and re.search(r"\b(until|till|til|through|to|for)\b", text.lower()):
            w.end = m_end                                      # an end was said in a way only the model read
    return w


def _joined(text: str, a: dict, b: dict) -> bool:
    """Are two clock times a range ('6 to 8pm', '9am until 1pm', '10-11')?"""
    between = _words_to_digits(text).lower()[a["end"]:b["pos"]]
    return bool(re.fullmatch(r"\s*(?:to|until|till|til|through|-|–|—)\s*", between))
