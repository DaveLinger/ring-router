"""Apple's on-device Foundation Model via /usr/bin/fm (macOS 27)."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .prompts import prompt

log = logging.getLogger(__name__)
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"



class FM:
    def __init__(self, path: str = "/usr/bin/fm", timeout_s: int = 15):
        self.path, self.timeout_s = path, timeout_s

    def respond(self, prompt: str, instructions: str = "", schema: Path | None = None) -> str:
        # --greedy: same input -> same output, so rewrites are predictable and testable
        cmd = [self.path, "respond", "--no-stream", "--greedy"]
        if instructions:
            cmd += ["--instructions", instructions]
        if schema:
            cmd += ["--schema", str(schema)]
        cmd.append(prompt)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s, check=True).stdout
        return re.sub(r"\x1b\[[0-9;]*m", "", out).strip()

    # --- tasks -------------------------------------------------------------
    def rewrite_ask(self, recipient: str, question: str, group: bool = False) -> str:
        who = f"{recipient} (group chat)" if group else recipient
        return self.respond(f"Recipient: {who}. Indirect question: {question}", prompt("ask_rewrite"))

    def standalone(self, previous_question: str, followup: str) -> str:
        return self.respond(f"Previous question: {previous_question}\nFollow-up: {followup}",
                            prompt("followup_rewrite"))

    def title(self, body: str) -> str:
        return self.respond(body, prompt("note_title"))

    def split_items(self, spoken: str) -> list[str]:
        data = json.loads(self.respond(spoken, prompt("shopping_split"), SCHEMAS / "shopping_items.json"))
        return [i.strip() for i in data.get("items", []) if i.strip()]

    def extract(self, text: str, instructions: str, schema: dict, now: datetime | None = None) -> dict:
        """Structured extraction; the date/time is always included. `now` is when the words were
        spoken, if that differs from the present (a recording delivered late)."""
        now = (now or datetime.now()).astimezone()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(schema, f)
        try:
            prompt = f"Now: {now:%A %Y-%m-%d %H:%M} ({now.tzname()}).\nRequest: {text}"
            return json.loads(self.respond(prompt, instructions, Path(f.name)))
        finally:
            Path(f.name).unlink(missing_ok=True)


def checked_rewrite(original: str, rewritten: str, recipient_names: list[str]) -> tuple[str, bool]:
    """Apply the fallback rules. Returns (text_to_send, used_rewrite)."""
    r = (rewritten or "").strip().strip('"').strip()
    for n in sorted(recipient_names, key=len, reverse=True):   # drop a leading "Instinct:" / "Terri,"
        if n:
            r = re.sub(rf"^{re.escape(n)}\s*[:,\-—]\s*", "", r, flags=re.IGNORECASE)
    if not r:
        return original, False
    if len(r) > max(40, 2 * len(original)):
        return original, False
    low = r.lower()
    if any(n and re.search(rf"\b{re.escape(n.lower())}\b", low) for n in recipient_names):
        return original, False
    return r[:1].upper() + r[1:], True
