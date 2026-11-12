"""Allowlisted recipient resolution with confidence tiers.

  strong  - the spoken name is a configured alias/sounds_like  -> send
  weak    - close to an allowlisted name (spelling or sound)   -> Pushcut confirm
  unknown - not on the allowlist                               -> refuse, nothing sent
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text import SEP, phrase_pattern, similarity, simple, soundex

# Words after ask/tell that mean "not a person's name" -> not a messaging command.
NOT_A_NAME = {"around", "me", "us", "him", "her", "them", "it", "you", "everyone", "everybody", "someone", "somebody",
              "what", "what's", "whats", "when", "where", "who", "why", "how", "which", "whether", "if",
              "about", "for", "a", "an", "the", "is", "are", "do", "does", "did", "can", "could", "will",
              "would", "should", "my", "your", "this", "that", "to"}
WEAK_SIMILARITY = 0.72   # "sherry"~"terry" is 0.73; "harry"~"terry" is 0.6 (unknown)
CLOSE_SIMILARITY = 0.8   # without a shared ending, a name must be this close in spelling


@dataclass
class RecipientMatch:
    key: str | None           # config key, None when unknown
    tier: str                 # strong | weak | unknown
    heard: str                # the words taken as the name
    end: int                  # index in the transcript where the body starts
    display: str = ""


def _aliases(recipients: dict) -> list[tuple[str, str]]:
    out = []
    for key, r in recipients.items():
        names = set(r.get("sounds_like", [])) | {r.get("display", key).lower()}
        for n in names:
            out.append((n.lower(), key))
    return sorted(out, key=lambda kv: len(kv[0]), reverse=True)


def resolve(text: str, pos: int, recipients: dict, *, bare_only: bool = False) -> RecipientMatch | None:
    """Resolve the recipient whose name starts at text[pos:].

    Returns None when the next word is a pronoun (e.g. "tell me a joke") or empty.
    bare_only: only recipients with bare = true may match (no ask/tell verb spoken).
    """
    for alias, key in _aliases(recipients):
        if bare_only and not recipients[key].get("bare"):
            continue
        m = re.compile(phrase_pattern(alias) + SEP + "*", re.IGNORECASE).match(text, pos)
        if m and (not bare_only or re.search(r"[,.:;!?\-–—…]", text[m.start() + len(alias):m.end()])):
            return RecipientMatch(key, "strong", alias, m.end(), recipients[key].get("display", key))
        # no verb in front: the name must be followed by a pause ("Instinct, ..."), so that
        # "Instinct tells me this is a bad idea" isn't sent to Instinct
    if bare_only:
        return None

    # Not an exact alias: take the next one or two words as the heard name.
    m = re.compile(r"((?:the\s+)?[\w']+)" + SEP + "*", re.IGNORECASE).match(text, pos)
    if not m:
        return None
    heard = simple(m.group(1))
    if not heard or heard in NOT_A_NAME or heard.split()[0] in NOT_A_NAME - {"the"}:
        return None
    if heard.endswith(("'s", "s'")):       # "Terri's mom" is somebody else
        return RecipientMatch(None, "unknown", heard, m.end(), heard)
    best_key, best_score = None, 0.0
    for alias, key in _aliases(recipients):
        score = similarity(heard, alias)
        hw, aw = heard.split()[-1], alias.split()[-1]
        hs, as_ = soundex(hw), soundex(aw)
        if len(alias.split()) > 1 or len(heard.split()) > 1:
            # "the guys": "the" matching is worth nothing; the last word has to sound the same
            near = len(alias.split()) == len(heard.split()) and bool(hs) and hs == as_
        else:
            # near-miss = spelled alike AND sounds alike, and either ends the same ("Jerry", "Sherry")
            # or is very close ("Instincts"). "Troy" and "Tara" share only T-R with "Terri": not near.
            sounds = bool(hs) and (hs == as_ or hs[1:] == as_[1:])
            near = sounds and (hw[-2:] == aw[-2:] or score >= CLOSE_SIMILARITY)
        if not near:
            continue
        if score > best_score:
            best_key, best_score = key, score
    if best_key and best_score >= WEAK_SIMILARITY:
        return RecipientMatch(best_key, "weak", heard, m.end(), recipients[best_key].get("display", best_key))
    return RecipientMatch(None, "unknown", heard, m.end(), heard)
