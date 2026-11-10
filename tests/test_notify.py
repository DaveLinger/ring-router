import json

from ringrouter.notify import Button, Notice, PushcutNotifier


def capture(monkeypatch):
    sent = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=10):
        sent.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data))
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return sent


def test_pushcut_webhook_url_needs_no_key(monkeypatch):
    sent = capture(monkeypatch)
    PushcutNotifier("Ring Router", webhook_url="https://api.pushcut.io/SECRET/notifications/Ring%20Router").send(
        Notice("Sent to Terri", "I'm on my way", [Button("Undo", "https://x/confirm/1/no")]))
    assert sent["url"].endswith("/SECRET/notifications/Ring%20Router")
    assert not any(k.lower() == "api-key" for k in sent["headers"])
    assert sent["body"]["title"] == "Sent to Terri" and sent["body"]["actions"][0]["url"] == "https://x/confirm/1/no"


def test_pushcut_api_key(monkeypatch):
    sent = capture(monkeypatch)
    PushcutNotifier("Ring Router", api_key="k").send(Notice("T", "x"))
    assert sent["url"] == "https://api.pushcut.io/v1/notifications/Ring%20Router"
    assert {k.lower(): v for k, v in sent["headers"].items()}["api-key"] == "k"
