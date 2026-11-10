"""HTTP listener: POST /hook/<path_token>, GET|POST /confirm/<id>/<yes|no>, GET /health.

Status codes follow the Pebble app's retry behaviour:
  200  handled / dry run / duplicate / discarded / cancelled / refused / accepted (not retried)
  400  malformed; 401 bad signature or expired timestamp; 404 wrong path (not retried)
  422  send failed or status unknown (not auto-retried; "Retry" in Recent runs resends)
  503  temporary failure before anything was sent (the app retries every minute, 10x)
"""

from __future__ import annotations

import hmac
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import handlers, md, protocol
from .config import Config
from .fm import FM
from .handlers import Context, Result
from .llm import LLMStore, OpenRouter
from .notify import make_notifier
from .router import route
from .store import Store

log = logging.getLogger(__name__)
MAX_BODY = 20 * 1024 * 1024


class App:
    def __init__(self, cfg: Config, *, store: Store | None = None, notifier=None, fm: FM | None = None,
                 mcp_call=None, background: bool = True, llm=None, notes_writer=None,
                 agent_runner=None):
        self.cfg = cfg
        self.store = store or Store(cfg.state_dir / "state.db")
        self.notifier = notifier or make_notifier(cfg)
        f = cfg.section("fm")
        self.fm = fm or FM(f.get("path", "/usr/bin/fm"), f.get("timeout_s", 15))
        self.mcp_call = mcp_call
        self.llm = llm or OpenRouter(cfg.secret("openrouter_api_key"))
        self.llm_store = LLMStore(self.store)
        self.notes_writer = notes_writer
        self.agent_runner = agent_runner
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="job") if background else None
        self.public_url = cfg.server.get("public_url", f"http://{cfg.server.get('host')}:{cfg.server.get('port')}")
        for d in self.store.recover_interrupted():
            log.error("delivery %s was mid-send at shutdown; marked unknown (not resent)", d[:8])

    def ctx(self, delivery_id: str) -> Context:
        return Context(cfg=self.cfg, notifier=self.notifier, store=self.store, fm=self.fm,
                       delivery_id=delivery_id, public_url=self.public_url, mcp_call=self.mcp_call,
                       llm=self.llm, llm_store=self.llm_store, notes_writer=self.notes_writer,
                       agent_runner=self.agent_runner)

    def process(self, delivery_id: str, transcript: str) -> tuple[int, str]:
        """Route + handle one authenticated, de-duplicated transcript."""
        r = route(transcript, self.cfg)
        log.info("delivery %s: %s", delivery_id[:8], r.describe())
        self.store.set_state(delivery_id, "received", transcript=r.transcript, route=r.route)
        ctx = self.ctx(delivery_id)
        if r.route in handlers.SLOW_ROUTES and self.pool:
            self.store.set_state(delivery_id, "accepted", summary=r.reason)
            self.pool.submit(self._background, r, ctx)
            return 200, f"accepted: {r.route}"
        res = handlers.run(r, ctx)
        self.store.set_state(delivery_id, res.state, summary=res.summary)
        log.info("delivery %s: %s: %s", delivery_id[:8], res.state, res.summary)
        return res.http_status, f"{res.state}: [{r.route}] {res.summary}"

    def _background(self, r, ctx: Context) -> None:
        res = handlers.run(r, ctx)
        self.store.set_state(ctx.delivery_id, res.state, summary=res.summary)
        log.info("delivery %s: (background) %s: %s", ctx.delivery_id[:8], res.state, res.summary)

    def answer_page(self, aid: str) -> str | None:
        a = self.llm_store.get_answer(aid)
        if not a:
            return None
        import html as _h  # noqa: PLC0415
        import time as _t  # noqa: PLC0415
        when = _t.strftime("%b %-d, %Y %-I:%M %p", _t.localtime(a["created"]))
        cost = f" \u00b7 ${a['cost']:.4f}" if a.get("cost") is not None else ""
        src = ""
        if a["citations"] and "sources" not in a["answer"].lower()[-2000:]:
            src = "<h3>Sources</h3><ol>" + "".join(
                f'<li><a href="{_h.escape(c["url"])}">{_h.escape(c["title"])}</a></li>' for c in a["citations"]) + "</ol>"
        body = (f'<p class="meta">{_h.escape(a["model"])} \u00b7 {when}{cost}</p>'
                f'<p class="q">\u201c{_h.escape(a["question"])}\u201d</p>{md.to_html(a["answer"])}{src}')
        return md.page(a["question"][:80], body)

    def confirm(self, pid: str, answer: str) -> tuple[int, str]:
        if answer == "no":
            ok = self.store.cancel_pending(pid)
            if ok:
                self.notifier.send(handlers.Notice("Cancelled", "Nothing sent."))
            return 200, "cancelled" if ok else "not pending"
        claimed = self.store.take_pending(pid)
        if not claimed:
            return 410, "expired or already handled"
        kind, payload = claimed
        res: Result = handlers.run_confirmed(kind, payload, self.ctx(f"confirm-{pid}"))
        return res.http_status, f"{res.state}: {res.summary}"


def make_handler(app: App):
    cfg = app.cfg
    path_token = cfg.secret("path_token") or ""
    secret = cfg.secret("signing_secret") or ""
    window = int(cfg.server.get("timestamp_window_s", 300))
    hook_lock = threading.Lock()  # serialise dedupe check + claim

    class Handler(BaseHTTPRequestHandler):
        server_version = "ring-router"
        sys_version = ""
        timeout = 30

        def log_message(self, fmt, *args):  # quiet default access log
            log.debug("http: " + fmt, *args)

        def reply(self, code: int, text: str) -> None:
            data = (text + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p == "/health":
                return self.reply(200, "ok")
            if p.startswith("/confirm/"):
                return self._confirm(p)
            if p.startswith("/answers/"):
                page = app.answer_page(p.split("/", 2)[2])
                if page is None:
                    return self.reply(404, "not found")
                data = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Robots-Tag", "noindex")
                self.end_headers()
                self.wfile.write(data)
                return None
            return self.reply(404, "not found")

        def do_POST(self):
            p = self.path.split("?", 1)[0]
            if p.startswith("/confirm/"):
                return self._confirm(p)
            if not path_token or not hmac.compare_digest(p.rstrip("/"), "/hook/" + path_token):
                return self.reply(404, "not found")
            if not secret:
                log.error("no signing_secret configured; refusing")
                return self.reply(503, "not configured")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400, "bad length")
            if length <= 0 or length > MAX_BODY:
                return self.reply(400, "bad length")
            body = self.rfile.read(length)
            try:
                v = protocol.verify(self.headers, body, secret, window)
            except protocol.RejectError as e:
                log.warning("rejected: %s", e.reason)
                return self.reply(e.status, e.reason)
            did = v.delivery_id
            if v.is_test or v.trigger == "test-event":
                log.info("delivery %s: test event OK (nothing run)", did[:8])
                return self.reply(200, "test ok")
            if v.trigger not in protocol.RECORDING_TRIGGERS:
                return self.reply(200, "ignored trigger")
            with hook_lock:
                state = app.store.get_state(did)
                if state is not None:
                    log.info("delivery %s: duplicate (state=%s), not reprocessed", did[:8], state)
                    return self.reply(200, f"duplicate: {state}")
                app.store.set_state(did, "received")
            try:
                fields = protocol.parse_multipart(self.headers.get("Content-Type", ""), body)
            except Exception:
                app.store.set_state(did, "discarded", summary="malformed multipart")
                return self.reply(400, "malformed multipart")
            transcript = (fields.get("transcription") or "").strip()
            if not transcript:
                app.store.set_state(did, "discarded", summary="no transcription part")
                return self.reply(200, "discarded: no transcription (set payload mode to include it)")
            code, text = app.process(did, transcript)
            return self.reply(code, text)

        def _confirm(self, p: str):
            parts = p.strip("/").split("/")
            if len(parts) != 3 or parts[2] not in ("yes", "no"):
                return self.reply(404, "not found")
            code, text = app.confirm(parts[1], parts[2])
            return self.reply(code, text)

    return Handler


def serve(cfg: Config) -> None:
    app = App(cfg)
    host, port = cfg.server.get("host", "127.0.0.1"), int(cfg.server.get("port", 8642))
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    mode = "DRY RUN" if cfg.server.get("dry_run", True) else "LIVE"
    log.info("ring-router listening on http://%s:%d (%s, notify=%s)", host, port, mode,
             type(app.notifier).__name__)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
