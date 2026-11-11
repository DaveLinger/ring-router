import pytest

from ringrouter.config import EXAMPLE_CONFIG, load_config


class FakeFM:
    """Deterministic stand-in for /usr/bin/fm."""

    def __init__(self):
        self.calls = []

    def rewrite_ask(self, recipient, question, group=False):
        self.calls.append(("rewrite", recipient, question))
        return "When will you be home?"

    def standalone(self, previous_question, followup):
        self.calls.append(("standalone", previous_question, followup))
        return f"{followup} (re: {previous_question})"

    def title(self, body):
        self.calls.append(("title", body))
        return "Test title"

    def split_items(self, spoken):
        self.calls.append(("split", spoken))
        return [s.strip() for s in spoken.replace(" and ", ",").split(",") if s.strip()]

    def extract(self, text, instructions, schema):
        self.calls.append(("extract", text))
        return {"title": "Call the vet", "due": "2026-11-10 17:00:00", "start": "2026-11-10 15:00:00"}


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("RING_ROUTER_PATH_TOKEN", "testpath")
    monkeypatch.setenv("RING_ROUTER_SIGNING_SECRET", "testsecret")
    for n in ("OPENROUTER_API_KEY", "PUSHCUT_API_KEY", "HUBITAT_TOKEN"):
        monkeypatch.setenv(f"RING_ROUTER_{n}", "")
    c = load_config(EXAMPLE_CONFIG)
    c.secrets = {"path_token": "testpath", "signing_secret": "testsecret", "activity_token": "acttoken"}
    return c


@pytest.fixture
def fake_fm():
    return FakeFM()
