"""OpenRouter client, per-model follow-up memory, and the answer store."""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_DIR

API_URL = "https://openrouter.ai/api/v1/chat/completions"
PROFILE_FILE = PROJECT_DIR / "profile.md"


@dataclass
class Reply:
    text: str
    citations: list[dict] = field(default_factory=list)   # [{"title", "url"}]
    model: str = ""
    cost: float | None = None                            # USD, when OpenRouter reports it
    tokens: int | None = None


class LLMError(RuntimeError):
    pass


class OpenRouter:
    def __init__(self, api_key: str | None, timeout_s: int = 120):
        self.api_key, self.timeout_s = api_key, timeout_s

    def chat(self, model: str, messages: list[dict], *, web: bool = False, web_results: int = 5) -> Reply:
        if not self.api_key:
            raise LLMError("OpenRouter API key not set (RING_ROUTER_OPENROUTER_API_KEY in .env)")
        if not model or "CHANGE-ME" in model:
            raise LLMError(f"model ID not configured ({model!r})")
        body: dict = {"model": model, "messages": messages, "usage": {"include": True}}
        if web:
            body["plugins"] = [{"id": "web", "max_results": web_results}]
        req = urllib.request.Request(API_URL, method="POST", data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "X-Title": "ring-router"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise LLMError(f"OpenRouter HTTP {e.code}: {detail}") from e
        if "error" in data:
            raise LLMError(f"OpenRouter error: {data['error']}")
        msg = data["choices"][0]["message"]
        cites, seen = [], set()
        for a in msg.get("annotations") or []:
            u = (a.get("url_citation") or {})
            if u.get("url") and u["url"] not in seen:
                seen.add(u["url"])
                cites.append({"title": u.get("title") or u["url"], "url": u["url"]})
        usage = data.get("usage") or {}
        return Reply(text=(msg.get("content") or "").strip(), citations=cites, model=data.get("model", model),
                     cost=usage.get("cost"), tokens=usage.get("total_tokens"))


def load_profile(path: Path = PROFILE_FILE) -> str:
    """Facts about the user (profile.md, git-ignored), added to every model prompt."""
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return ""
    return f"About the user (use when relevant, e.g. location for 'near me'):\n{text}" if text else ""


# ---------------------------------------------------------------- storage ----

LLM_SCHEMA = """
CREATE TABLE IF NOT EXISTS convo (
  model TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
  id TEXT PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL,
  question TEXT NOT NULL, answer TEXT NOT NULL, citations TEXT NOT NULL, cost REAL
);
"""


class LLMStore:
    """Follow-up memory per model and saved answers; shares the router's SQLite connection."""

    def __init__(self, store):
        self.s = store
        with store.lock:
            store.conn.executescript(LLM_SCHEMA)

    def history(self, model_key: str, window_s: int, max_messages: int = 8) -> list[dict]:
        """Recent turns with this model, if the last one is within the follow-up window."""
        cutoff = time.time() - window_s
        with self.s.lock:
            rows = self.s.conn.execute("SELECT role, content, ts FROM convo WHERE model=? ORDER BY ts DESC, rowid DESC LIMIT ?",
                                       (model_key, max_messages)).fetchall()
        if not rows or rows[0][2] < cutoff:
            return []
        out = []
        for role, content, ts in rows:          # newest first; stop at a gap longer than the window
            if out and (prev_ts - ts) > window_s:
                break
            out.append({"role": role, "content": content})
            prev_ts = ts
        return list(reversed(out))

    def remember(self, model_key: str, question: str, answer: str) -> None:
        now = time.time()
        with self.s.lock:
            self.s.conn.executemany("INSERT INTO convo VALUES(?,?,?,?)",
                                    [(model_key, "user", question, now), (model_key, "assistant", answer, now)])

    def save_answer(self, kind: str, model: str, question: str, reply: Reply) -> str:
        aid = secrets.token_urlsafe(16)
        with self.s.lock:
            self.s.conn.execute("INSERT INTO answers VALUES(?,?,?,?,?,?,?,?)",
                                (aid, time.time(), kind, model, question, reply.text,
                                 json.dumps(reply.citations), reply.cost))
        return aid

    def get_answer(self, aid: str) -> dict | None:
        with self.s.lock:
            row = self.s.conn.execute("SELECT created, kind, model, question, answer, citations, cost FROM answers"
                                      " WHERE id=?", (aid,)).fetchone()
        if not row:
            return None
        keys = ("created", "kind", "model", "question", "answer", "citations", "cost")
        d = dict(zip(keys, row))
        d["citations"] = json.loads(d["citations"])
        return d
