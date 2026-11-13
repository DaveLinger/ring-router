"""Transcript cleanup and phrase matching.

Matching runs on the original transcript (case-insensitive, punctuation tolerated
between words) so the extracted body keeps the speaker's casing and punctuation.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

SEP = r"[\s,.:;!?\-–—…]"
FILLER = ("um", "uh", "umm", "uhh", "er", "okay", "ok", "so", "alright", "all right")
_SMART = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def clean(transcript: str) -> str:
    """Normalise whitespace and quotes, drop leading filler words."""
    t = unicodedata.normalize("NFKC", transcript).translate(_SMART)
    t = re.sub(r"\s+", " ", t).strip()
    while True:
        m = re.match(r"(?i)(" + "|".join(re.escape(f) for f in FILLER) + r")(?![\w'])" + SEP + r"*", t)
        if not m or m.end() == len(t):
            break
        t = t[m.end():]
    return t.strip()


def simple(text: str) -> str:
    """Lowercase, apostrophes kept, other punctuation -> space, collapsed."""
    t = clean(text).lower()
    t = re.sub(r"[^\w'\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def phrase_pattern(phrase: str) -> str:
    """Regex for a spoken phrase; punctuation allowed between its words."""
    words = [re.escape(w) for w in phrase.split()]
    return (SEP + "+").join(words) + r"(?![\w'])"


def match_prefix(text: str, phrases, pos: int = 0) -> tuple[str, int] | None:
    """Longest phrase that appears at `pos`. Returns (phrase, end_index_after_separators)."""
    for phrase in sorted(phrases, key=len, reverse=True):
        m = re.compile(phrase_pattern(phrase) + SEP + "*", re.IGNORECASE).match(text, pos)
        if m:
            return phrase, m.end()
    return None


def strip_body(s: str) -> str:
    return s.strip().lstrip(",.:;-–— ").strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def soundex(word: str) -> str:
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return ""
    codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
             **dict.fromkeys("dt", "3"), "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}
    out, last = word[0].upper(), codes.get(word[0], "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != last:
            out += code
        if ch not in "hw":
            last = code
    return (out + "000")[:4]


def sound_key(word: str) -> str:
    """Soundex with the first letter coded by sound too, so spellings of one sound share a key:
    Erin/Aaron, Sean/Shawn, Geoff/Jeff, Kris/Chris, Phil/Fil. Vowels are ignored, so it over-matches
    (Tom/Tim): use it where a match only adds caution, never where it picks a recipient."""
    w = re.sub(r"[^a-z]", "", word.lower())
    w = re.sub(r"^ph", "f", w)
    w = re.sub(r"^(kn|gn|wr)", lambda m: m.group(0)[1], w)       # silent first letter: Knox, Wright
    s = soundex(w)
    if not s:
        return ""
    first = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"),
             "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}.get(w[0], "0")
    return first + s[1:]


def capitalize_first(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s
