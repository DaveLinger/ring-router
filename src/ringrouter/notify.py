"""Notifications back to the user: console (dev) or Pushcut."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Button:
    label: str
    url: str


@dataclass
class Notice:
    title: str
    text: str
    buttons: list[Button] = field(default_factory=list)


class ConsoleNotifier:
    """Prints notifications; keeps them in memory for tests and the `route` command."""

    def __init__(self, echo: bool = True):
        self.echo = echo
        self.sent: list[Notice] = []

    def send(self, notice: Notice) -> None:
        self.sent.append(notice)
        if self.echo:
            lines = [f"  \U0001F514 {notice.title}: {notice.text}"]
            lines += [f"     [{b.label}] {b.url}" for b in notice.buttons]
            print("\n".join(lines), flush=True)
        log.info("notify: %s: %s", notice.title, notice.text)


class PushcutNotifier:
    """POST https://api.pushcut.io/v1/notifications/<name> (API-Key header).

    Button actions call our /confirm URLs in the background. Field names follow
    Pushcut's Web API docs; verify against your account in phase 1.
    """

    def __init__(self, api_key: str, notification: str):
        self.api_key, self.notification = api_key, notification

    def send(self, notice: Notice) -> None:
        payload: dict = {"title": notice.title, "text": notice.text[:1000]}
        if notice.buttons:
            payload["actions"] = [{"name": b.label, "url": b.url, "urlBackgroundOptions": {"httpMethod": "POST"}}
                                  for b in notice.buttons]
        req = urllib.request.Request(
            "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(self.notification),
            data=json.dumps(payload).encode(), method="POST",
            headers={"API-Key": self.api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
        except Exception as e:  # never let a notification failure break routing
            log.error("pushcut failed: %s (%s: %s)", e, notice.title, notice.text)


def make_notifier(cfg):
    n = cfg.section("notify")
    if n.get("provider") == "pushcut" and cfg.secret("pushcut_api_key"):
        return PushcutNotifier(cfg.secret("pushcut_api_key"), n.get("pushcut_notification", "Ring Router"))
    return ConsoleNotifier()
