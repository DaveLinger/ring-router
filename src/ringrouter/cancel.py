"""Cancel detection: a transcript that ends with a cancel phrase runs nothing.

Strong phrases cancel whenever they end the transcript (or are all of it).
Weak phrases cancel only when they end the transcript AND follow a sentence
break (period, comma, dash, ellipsis...), so "tell Terri to forget that" still sends.
"""

from __future__ import annotations

import re

from .text import SEP, clean, phrase_pattern, simple

BREAK = r"[.,;:!?\-–—…]"


def detect(transcript: str, strong: list[str], weak: list[str]) -> str | None:
    """Return the matched cancel phrase, or None."""
    t = clean(transcript)
    whole = simple(t)
    for phrase in strong + weak:
        if whole == simple(phrase):
            return phrase
    for phrase in sorted(strong, key=len, reverse=True):
        if re.search(r"(?:^|(?<![\w']))" + phrase_pattern(phrase) + SEP + r"*$", t, re.IGNORECASE):
            return phrase
    for phrase in sorted(weak, key=len, reverse=True):
        if re.search(BREAK + r"\s*" + phrase_pattern(phrase) + SEP + r"*$", t, re.IGNORECASE):
            return phrase
    return None
