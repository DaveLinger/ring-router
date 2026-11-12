"""Notes: short ones go through mcp-macos; long ones use the AppleScript writer, which has no size cap."""

import pytest

from ringrouter.handlers import Context, run
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store

LONG = "Make a note: " + " ".join(f"point {i} about the boat trailer & its <bearings>." for i in range(120))


@pytest.fixture
def live(cfg, fake_fm):
    cfg.raw["server"]["dry_run"] = False
    mcp_calls, written = [], []
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  mcp_call=lambda name, args: mcp_calls.append((name, args)) or "ok",
                  notes_writer=lambda title, html, folder: written.append((title, html, folder)))
    return ctx, mcp_calls, written


def test_short_note_goes_through_mcp(live):
    ctx, mcp_calls, written = live
    res = run(route("Make a note: the boat trailer needs new bearings", ctx.cfg), ctx)
    assert res.state == "done" and written == []
    assert mcp_calls == [("notes_items", {"action": "create", "title": "Test title",
                                          "body": "the boat trailer needs new bearings", "folder": "Notes"})]


def test_long_note_is_saved_whole(live):
    ctx, mcp_calls, written = live
    res = run(route(LONG, ctx.cfg), ctx)
    (title, html, folder), = written
    assert res.state == "done" and mcp_calls == [] and folder == "Notes"
    assert "point 119 about the boat trailer &amp; its &lt;bearings&gt;." in html      # all of it, escaped
    assert "cut" not in ctx.notifier.sent[-1].text


def test_long_note_falls_back_to_a_cut_one_and_says_so(live):
    ctx, mcp_calls, _ = live
    ctx.notes_writer = lambda *a: (_ for _ in ()).throw(RuntimeError("Notes automation not allowed"))
    res = run(route(LONG, ctx.cfg), ctx)
    (_, args), = mcp_calls
    assert res.state == "done" and len(args["body"]) == 2000
    assert "cut at 2,000 of" in ctx.notifier.sent[-1].text and "cut at 2,000 of" in res.summary
