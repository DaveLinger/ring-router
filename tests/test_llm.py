"""Phase 3: ask-a-model, follow-ups, answer page, research notes (OpenRouter faked)."""

import time
import urllib.request

import pytest

from ringrouter import md
from ringrouter.handlers import Context, h_llm, h_research
from ringrouter.llm import LLMStore, Reply
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store


class FakeLLM:
    def __init__(self, text="Canberra is the capital of Australia.", citations=None):
        self.text, self.citations, self.calls = text, citations or [], []

    def chat(self, model, messages, *, web=False, web_results=5):
        self.calls.append({"model": model, "messages": messages, "web": web, "web_results": web_results})
        return Reply(self.text, self.citations, model, cost=0.0012, tokens=200)


@pytest.fixture
def ctx(cfg, fake_fm):
    store = Store(":memory:")
    notes = []
    c = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=store, fm=fake_fm,
                public_url="http://test", llm=FakeLLM(), llm_store=LLMStore(store),
                notes_writer=lambda title, html, folder: notes.append((title, html, folder)),
                mcp_call=lambda *a: pytest.fail("no MCP"))
    c.notes = notes
    return c


def test_ask_model_sends_spoken_preamble_and_web(ctx, cfg):
    r = route("Ask ChatGPT what the capital of Australia is", cfg)
    res = h_llm(r, ctx)
    call = ctx.llm.calls[-1]
    assert res.state == "done" and call["model"] == "openai/gpt-6.1-terra" and call["web"] is True
    system, user = call["messages"][0], call["messages"][-1]
    assert "speech-to-text transcript" in system["content"] and "phone notification" in system["content"]
    assert user == {"role": "user", "content": "what the capital of Australia is"}
    assert ctx.notifier.sent[-1].title == "ChatGPT" and "Canberra" in ctx.notifier.sent[-1].text
    assert not ctx.notifier.sent[-1].buttons            # short, no sources -> no button


def test_followup_within_window_carries_history(ctx, cfg):
    h_llm(route("Ask DeepSeek what's the best Chinese restaurant near me", cfg), ctx)
    h_llm(route("Ask DeepSeek more about that", cfg), ctx)
    msgs = ctx.llm.calls[-1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "what's the best Chinese restaurant near me"
    assert msgs[-1]["content"] == "more about that (re: what's the best Chinese restaurant near me)"
    # a different model doesn't see DeepSeek's thread
    h_llm(route("Ask Qwen more about that", cfg), ctx)
    assert [m["role"] for m in ctx.llm.calls[-1]["messages"]] == ["system", "user"]


def test_followup_expires(ctx):
    ctx.llm_store.remember("deepseek", "old q", "old a")
    with ctx.store.lock:
        ctx.store.conn.execute("UPDATE convo SET ts = ?", (time.time() - 3600,))
    assert ctx.llm_store.history("deepseek", 600) == []


def test_long_answer_or_sources_get_full_answer_button(ctx, cfg):
    ctx.llm = FakeLLM("word " * 200, [{"title": "Src", "url": "https://example.com"}])
    h_llm(route("Ask Claude how to descale an espresso machine", cfg), ctx)
    n = ctx.notifier.sent[-1]
    assert n.text.endswith("…") and len(n.text) <= 300
    assert n.buttons and n.buttons[0].label == "Full answer" and "/answers/" in n.buttons[0].url


def test_model_error_is_reported(ctx, cfg):
    class Boom:
        def chat(self, *a, **k):
            raise RuntimeError("OpenRouter API key not set")
    ctx.llm = Boom()
    res = h_llm(route("Ask Qwen to translate good morning into Mandarin", cfg), ctx)
    assert res.state == "failed" and "Not asked" in ctx.notifier.sent[-1].text


def test_research_dry_run_saves_answer_not_note(ctx, cfg):
    ctx.llm = FakeLLM("Bottom line: buy X.\n\n## Picks\n- X\n- Y", [{"title": "Review", "url": "https://r.example"}])
    res = h_research(route("Research the best robot vacuum under 500 dollars", cfg), ctx)
    assert res.state == "dry_run" and not ctx.notes
    call = ctx.llm.calls[-1]
    assert call["web"] is True and call["web_results"] == 8 and "research note" in call["messages"][0]["content"]
    assert ctx.notifier.sent[-1].buttons[0].label == "Read it"


def test_research_live_writes_note_with_sources(ctx, cfg):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    ctx.llm = FakeLLM("Bottom line: buy X.\n\n## Picks\n- **X** is best", [{"title": "Review", "url": "https://r.example"}])
    res = h_research(route("Research with DeepSeek the best robot vacuum", cfg), ctx)
    assert res.state == "done" and ctx.llm.calls[-1]["model"] == "deepseek/deepseek-v4.1-flash"
    title, html, folder = ctx.notes[-1]
    assert title == "Test title" and folder == "Research"
    assert "<h3>Picks</h3>" in html and "<b>X</b>" in html and 'href="https://r.example"' in html


def test_notification_text_is_plain(ctx, cfg):
    ctx.llm = FakeLLM("The **Burj Khalifa** is tallest. ([guinnessworldrecords.com](https://g.example))")
    h_llm(route("Ask DeepSeek what's the tallest building", cfg), ctx)
    assert ctx.notifier.sent[-1].text == "The Burj Khalifa is tallest."


def test_plain_drops_bare_domain_citations():
    from ringrouter.md import plain
    assert plain("Tops at 4.2/5. [stacker.com](https://s.example) Runners-up . **A**") == "Tops at 4.2/5. Runners-up. A"
    assert plain("See [the review](https://r.example).") == "See the review."
    assert plain("Blue, from all directions ([a.com](https://a), [b.org](https://b)). Next.") == "Blue, from all directions. Next."


def test_markdown_to_html():
    h = md.to_html("# Title\nPara with **bold** and [link](https://a.b).\n\n1. one\n2. two\n\n"
                   "| A | B |\n|---|---|\n| 1 | 2 |\n\n<script>x</script>")
    assert "<h2>Title</h2>" in h and "<b>bold</b>" in h and '<a href="https://a.b">link</a>' in h
    assert "<ol><li>one</li><li>two</li></ol>" in h and "<td>1</td>" in h and "<script>" not in h


def test_answer_page_served(cfg, fake_fm):
    import threading
    from http.server import ThreadingHTTPServer

    from ringrouter.server import App, make_handler
    app = App(cfg, store=Store(":memory:"), notifier=ConsoleNotifier(echo=False), fm=fake_fm,
              background=False, llm=FakeLLM("**Canberra.**", [{"title": "Wiki", "url": "https://w.example"}]))
    aid = app.llm_store.save_answer("ask", "openai/x", "capital of Australia?", app.llm.chat("m", []))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        body = urllib.request.urlopen(f"{base}/answers/{aid}").read().decode()
        assert "<b>Canberra.</b>" in body and "https://w.example" in body and "$0.0012" in body
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/answers/nope")
    finally:
        httpd.shutdown()
