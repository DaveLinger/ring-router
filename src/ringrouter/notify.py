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
    """Pushcut notification, by either:
      * webhook_url: the notification's own URL from the Pushcut app (secret built in), or
      * api_key + notification name: POST https://api.pushcut.io/v1/notifications/<name>.
    The JSON body (title, text, actions) overrides the notification's defaults either way.
    Button actions call our /confirm URLs in the background.
    """

    def __init__(self, notification: str, api_key: str | None = None, webhook_url: str | None = None):
        self.api_key, self.notification, self.webhook_url = api_key, notification, webhook_url

    def send(self, notice: Notice) -> None:
        payload: dict = {"title": notice.title, "text": notice.text[:1000]}
        if notice.buttons:
            payload["actions"] = [{"name": b.label, "url": b.url, "urlBackgroundOptions": {"httpMethod": "POST"}}
                                  for b in notice.buttons]
        headers = {"Content-Type": "application/json"}
        if self.webhook_url:
            url = self.webhook_url
        else:
            url = "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(self.notification)
            headers["API-Key"] = self.api_key
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
        except Exception as e:  # never let a notification failure break routing
            log.error("pushcut failed: %s (%s: %s)", e, notice.title, notice.text)


def make_notifier(cfg):
    n = cfg.section("notify")
    if n.get("provider") == "pushcut" and (cfg.secret("pushcut_webhook_url") or cfg.secret("pushcut_api_key")):
        return PushcutNotifier(n.get("pushcut_notification", "Ring Router"),
                               api_key=cfg.secret("pushcut_api_key"), webhook_url=cfg.secret("pushcut_webhook_url"))
    return ConsoleNotifier()
