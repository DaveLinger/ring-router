"""The exact message text that goes out, using the REAL on-device model (/usr/bin/fm).

Checks every phrases.toml case that has `sends`. Skipped where fm isn't available.
Run only these:  pytest -m live
Skip these:      pytest -m "not live"
"""

import os
import tomllib
from pathlib import Path

import pytest

from ringrouter.fm import FM
from ringrouter.handlers import Context, compose_imessage
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store

FM_PATH = "/usr/bin/fm"
CASES = [c for c in tomllib.loads((Path(__file__).parent / "phrases.toml").read_text())["case"] if "sends" in c]

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.access(FM_PATH, os.X_OK), reason="needs /usr/bin/fm (macOS 27)")]


@pytest.mark.parametrize("case", CASES, ids=[c["say"] for c in CASES])
def test_message_sent(case, cfg):
    r = route(case["say"], cfg)
    assert r.route == "imessage", r.describe()
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=FM(FM_PATH),
                  mcp_call=lambda *a: pytest.fail("no MCP calls"))
    text, _ = compose_imessage(r, ctx)
    expected = case["sends"] if isinstance(case["sends"], list) else [case["sends"]]
    assert text in expected, f"sent {text!r}, expected one of {expected}"
