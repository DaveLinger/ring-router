"""HTTP listener: POST /hook/<path_token>, GET|POST /confirm/<id>/<yes|no|1-9>, GET /health.

Status codes follow the Pebble app's retry behaviour:
  200  handled / dry run / duplicate / discarded / cancelled / refused / accepted (not retried)
  400  malformed; 401 bad signature or expired timestamp; 404 wrong path (not retried)
  422  failed, partly done or status unknown (not auto-retried). "Retry" in Recent runs resends the
       same delivery: it runs again only if the first try failed with nothing done; otherwise it is a
       duplicate
  503  temporary failure before anything was sent (the app retries every minute, 10x)
"""

from __future__ import annotations

import hmac
import urllib.parse
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import activity, handlers, logfile, md, protocol
from .config import Config
from .fm import FM
from .handlers import Context, Result
from .llm import LLMStore, OpenRouter
from .notify import make_notifier
from .router import route
from .text import clean
from .store import Store

log = logging.getLogger(__name__)
MAX_BODY = 20 * 1024 * 1024


def same(a: str, b: str) -> bool:
    """Constant-time comparison of two strings. (hmac.compare_digest raises on non-ASCII str, and a
    request path can hold any byte.)"""
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))


def recorded_time(value, now: float | None = None) -> float | None:
    """The webhook's recordedAt (Unix ms) as Unix seconds; None when missing or implausible.

    The field comes from the reference project and hasn't been seen in real ring traffic yet, so
    anything odd (not a number, the future, over a month old) is ignored rather than trusted.
    """
    try:
        t = float(value)
    except (TypeError, ValueError):
        return None
    t = t / 1000 if t > 1e11 else t          # ms, or seconds if the app turns out to send those
    now = time.time() if now is None else now
    if not (now - 30 * 86400 <= t <= now + 300):
        return None
    return min(t, now)


def _ago(seconds: float) -> str:
    m = round(seconds / 60)
    if m < 90:
        return f"{m} min"
    return f"{round(m / 60)} h" if m < 48 * 60 else f"{round(m / 1440)} days"


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
        self.prune()
        for did, was, transcript, updated in self.store.recover_interrupted():
            log.error("delivery %s was %s at shutdown; marked unknown (not run again)", did[:8], was)
            if time.time() - updated < 86400:       # tell the owner, unless it is ancient history
                self.notifier.send(handlers.Notice(
                    "Interrupted", f"A restart cut this off, so it may not have finished: “{transcript or '?'}”. "
                                   "Check, and say it again if needed."))

    def prune(self) -> None:
        """Drop everything older than [server] retention_days, and keep the log file bounded.
        Runs at start-up and then daily."""
        days = int(self.cfg.server.get("retention_days", 90))
        self.store.prune(days)
        self.llm_store.prune(days)
        logfile.trim_own_log()          # launchd never rotates ~/Library/Logs/ring-router.log
        self.pruned_at = time.time()

    def ctx(self, delivery_id: str, recorded_at: float | None = None) -> Context:
        return Context(cfg=self.cfg, notifier=self.notifier, store=self.store, fm=self.fm,
                       delivery_id=delivery_id, public_url=self.public_url, recorded_at=recorded_at,
                       mcp_call=self.mcp_call,
                       llm=self.llm, llm_store=self.llm_store, notes_writer=self.notes_writer,
                       agent_runner=self.agent_runner)

    def process(self, delivery_id: str, transcript: str, recorded_at: float | None = None,
                allow_late: bool = False, tested: bool = False) -> tuple[int, str]:
        """Route + handle one authenticated, de-duplicated transcript.

        recorded_at: when it was spoken (Unix s), if known. allow_late: the owner tapped "Run" on
        an old recording, so don't hold it again. tested: the owner tapped "Proceed" in test mode.
        """
        if time.time() - self.pruned_at > 86400:
            self.prune()
        ctx = self.ctx(delivery_id, recorded_at)
        ctx.trace("transcript", text=transcript)
        ctx.trace("cleaned", text=clean(transcript))
        r = route(transcript, self.cfg)
        ctx.trace("route", route=r.route, reason=r.reason, params=r.params, extracted=r.body, verb=r.verb,
                  slow=r.route in handlers.SLOW_ROUTES, dry_run=self.cfg.is_dry_run(r.route))
        log.info("delivery %s: %s", delivery_id[:8], r.describe())
        self.store.set_state(delivery_id, "received", transcript=r.transcript, route=r.route)
        if self.cfg.server.get("test_mode") and not tested and r.route not in handlers.NEVER_HELD:
            return self._hold_test(ctx, r, transcript, recorded_at)
        age = time.time() - recorded_at if recorded_at else 0
        max_age = int(self.cfg.server.get("max_recording_age_s", 300))
        if r.route in handlers.LATE_CONFIRM_ROUTES and not allow_late and 0 < max_age < age:
            return self._hold_late(ctx, r, transcript, recorded_at, age)
        if r.route in handlers.SLOW_ROUTES and self.pool:
            self.store.set_state(delivery_id, "accepted", summary=r.reason)
            ctx.trace("response", http_status=200, body=f"accepted: {r.route}", note="slow route; finishing in background")
            self.pool.submit(self._background, r, ctx)
            return 200, f"accepted: {r.route}"
        res = handlers.run(r, ctx)
        self.store.set_state(delivery_id, res.state, summary=res.summary)
        ctx.trace("result", state=res.state, summary=res.summary)
        ctx.trace("response", http_status=res.http_status, body=f"{res.state}: [{r.route}] {res.summary}")
        log.info("delivery %s: %s: %s", delivery_id[:8], res.state, res.summary)
        return res.http_status, f"{res.state}: [{r.route}] {res.summary}"

    def _hold_late(self, ctx: Context, r, transcript: str, recorded_at: float, age: float) -> tuple[int, str]:
        """An old recording that would act right away: ask before running it."""
        pid = self.store.add_pending(ctx.delivery_id, "late", {
            "delivery_id": ctx.delivery_id, "transcript": transcript, "recorded_at": recorded_at},
            self.cfg.section("notify").get("confirm_ttl_s", 300))
        summary = f"recorded {_ago(age)} ago, held for confirmation {pid}"
        ctx.trace("late_recording", age_s=round(age), route=r.route, confirmation=pid)
        ctx.notify("Old recording", f"Recorded {_ago(age)} ago: \u201c{r.transcript}\u201d. Run it now?",
                   [handlers.Button("Run", f"{self.public_url}/confirm/{pid}/yes", background=True),
                    handlers.Button("Cancel", f"{self.public_url}/confirm/{pid}/no", background=True)])
        self.store.set_state(ctx.delivery_id, "needs_confirm", summary=summary)
        ctx.trace("response", http_status=200, body=f"needs_confirm: [{r.route}] {summary}")
        log.info("delivery %s: %s", ctx.delivery_id[:8], summary)
        return 200, f"needs_confirm: [{r.route}] {summary}"

    def _hold_test(self, ctx: Context, r, transcript: str, recorded_at: float | None) -> tuple[int, str]:
        """Test mode: work out what this would do, say so, and do it only after a Proceed tap.

        Fast routes run as a dry run (rewrite, recipient, items, times), with their notices kept back.
        Model, agent and Claude Code routes aren't run at all: asking them already sends the words out."""
        preview: list = []
        if r.route in handlers.SLOW_ROUTES:
            extra = ", ".join(f"{k}={v}" for k, v in r.params.items() if v not in (None, "", False))
            res = Result("dry_run", f"would run {r.route}{f' ({extra})' if extra else ''}: {r.body or r.transcript!r}")
        else:
            ctx.force_dry, ctx.held = True, preview
            try:
                res = handlers.run(r, ctx)
            finally:
                ctx.force_dry, ctx.held = False, None
        if res.state not in ("dry_run", "needs_confirm"):     # refused, discarded, failed: nothing to proceed with
            for n in preview:
                self.notifier.send(handlers.Notice(n.title, n.text))
            self.store.set_state(ctx.delivery_id, res.state, summary=res.summary)
            ctx.trace("result", state=res.state, summary=res.summary, test_mode=True)
            ctx.trace("response", http_status=res.http_status, body=f"{res.state}: [{r.route}] {res.summary}")
            return res.http_status, f"{res.state}: [{r.route}] {res.summary}"
        # The Proceed run counts dates from when it was spoken, even if the ring didn't say when that was.
        pid = self.store.add_pending(ctx.delivery_id, "test", {
            "delivery_id": ctx.delivery_id, "transcript": transcript, "recorded_at": recorded_at or time.time()},
            self.cfg.section("notify").get("confirm_ttl_s", 300))
        would = res.summary.removeprefix("DRY RUN: ")
        lines = [f"Heard: \u201c{r.transcript}\u201d", f"Route: {r.route}"]
        lines += [n.text for n in preview] or [would[:1].upper() + would[1:]]
        ctx.trace("test_hold", route=r.route, would=res.summary, confirmation=pid)
        ctx.notify("Test mode", "\n".join(lines),
                   [handlers.Button("Proceed", f"{self.public_url}/confirm/{pid}/yes", background=True),
                    handlers.Button("Cancel", f"{self.public_url}/confirm/{pid}/no", background=True)])
        summary = f"test mode: {would} (held for Proceed {pid})"
        self.store.set_state(ctx.delivery_id, "needs_confirm", summary=summary)
        ctx.trace("response", http_status=200, body=f"needs_confirm: [{r.route}] {summary}")
        log.info("delivery %s: %s", ctx.delivery_id[:8], summary)
        return 200, f"needs_confirm: [{r.route}] {summary}"

    def _background(self, r, ctx: Context) -> None:
        res = handlers.run(r, ctx)
        self.store.set_state(ctx.delivery_id, res.state, summary=res.summary)
        ctx.trace("result", state=res.state, summary=res.summary, background=True)
        log.info("delivery %s: (background) %s: %s", ctx.delivery_id[:8], res.state, res.summary)

    def _confirmed_background(self, kind: str, payload: dict, ctx: Context, choice: int | None = None) -> None:
        try:
            res = handlers.run_confirmed(kind, payload, ctx, choice)
        except Exception as e:
            log.exception("confirmed %s failed", kind)
            self.notifier.send(handlers.Notice("Error", f"{kind}: {e}"))
            res = Result("failed", f"{kind} error: {e}", 422)
        self.store.set_state(ctx.delivery_id, res.state, summary=res.summary)
        ctx.trace("result", state=res.state, summary=res.summary, confirmed=True, background=True)
        log.info("delivery %s: (confirmed, background) %s: %s", ctx.delivery_id[:8], res.state, res.summary)

    ANSWER_STATUS = {"ok": "Going ahead. The result will arrive as a notification.",
                     "cancelled": "Cancelled. Nothing was changed.",
                     "expired": "That proposal expired or was already handled. Nothing was changed.",
                     "failed": "Couldn't start it. Check the activity page."}

    def answer_action(self, aid: str) -> str:
        """The "Make this change" block for an answer whose Claude Code proposal is still waiting."""
        import html as _h  # noqa: PLC0415
        import json as _j  # noqa: PLC0415
        from .handlers.claude_code import ANSWER_PROPOSAL_KEY  # noqa: PLC0415
        raw = self.store.get_kv(ANSWER_PROPOSAL_KEY.format(aid=aid))
        if not raw:
            return ""
        d = _j.loads(raw)
        if not self.store.pending_waiting(d["pid"]):
            return ""
        q = urllib.parse.quote(aid, safe="")
        btn = ("font:inherit;font-weight:600;padding:.7em 1.2em;border-radius:.6em;border:0;"
               "margin:.3em .4em .3em 0;cursor:pointer")
        return (f'<div style="border:1px solid #8884;border-radius:.8em;padding:1em;margin:1.2em 0">'
                f'<p style="margin-top:0"><strong>Proposed change:</strong> {_h.escape(d["proposal"])}</p>'
                f'<form method="post" action="/confirm/{d["pid"]}/yes?from={q}" style="display:inline">'
                f'<button style="{btn}background:#e5533d;color:#fff">Make this change</button></form>'
                f'<form method="post" action="/confirm/{d["pid"]}/no?from={q}" style="display:inline">'
                f'<button style="{btn}background:#8883;color:inherit">Cancel</button></form></div>')

    def answer_page(self, aid: str, status: str = "") -> str | None:
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
                f'<li><a href="{_h.escape(c["url"])}">{_h.escape(c["title"])}</a></li>' for c in a["citations"]
                if str(c.get("url", "")).lower().startswith(("http://", "https://"))) + "</ol>"
        note = (f'<p style="border-left:3px solid #e5533d;padding-left:.8em"><strong>'
                f'{_h.escape(self.ANSWER_STATUS[status])}</strong></p>') if status in self.ANSWER_STATUS else ""
        body = (f'<p class="meta">{_h.escape(a["model"])} \u00b7 {when}{cost}</p>{note}'
                f'<p class="q">\u201c{_h.escape(a["question"])}\u201d</p>{md.to_html(a["answer"])}{src}'
                f'{self.answer_action(aid)}')
        return md.page(a["question"][:80], body)

    def confirm(self, pid: str, answer: str) -> tuple[int, str]:
        """A Send/Run or Cancel tap. The held action runs under the delivery that asked for it, so
        that delivery's state and trace show how it ended."""
        did = self.store.pending_delivery(pid)

        def close(state: str, summary: str) -> None:         # only a delivery still waiting on this tap
            if did and self.store.get_state(did) == "needs_confirm":
                self.store.set_state(did, state, summary=summary)

        if answer == "no":
            ok = self.store.cancel_pending(pid)
            if ok:
                self.notifier.send(handlers.Notice("Cancelled", "Nothing done."))
                close("cancelled", "confirmation declined")
            return 200, "cancelled" if ok else "not pending"
        claimed = self.store.take_pending(pid)
        if not claimed:
            close("cancelled", "confirmation expired")
            return 410, "expired or already handled"
        kind, payload = claimed
        choice = int(answer) if answer.isdigit() else None     # "Which one?" buttons send 1, 2, 3...
        if kind == "late":      # an old recording the owner chose to run: back through the pipeline
            return self.process(payload["delivery_id"], payload["transcript"], payload.get("recorded_at"),
                                allow_late=True)
        if kind == "test":      # Proceed in test mode: run it for real (the tap is fresh, so no late hold)
            return self.process(payload["delivery_id"], payload["transcript"], payload.get("recorded_at"),
                                allow_late=True, tested=True)
        ctx = self.ctx(did or f"confirm-{pid}")
        ctx.trace("confirmed", kind=kind)
        if kind in handlers.SLOW_CONFIRMS and self.pool:      # minutes of work: answer the tap now
            self.store.set_state(ctx.delivery_id, "accepted", summary=f"confirmed {kind}, running")
            self.pool.submit(self._confirmed_background, kind, payload, ctx, choice)
            return 200, f"accepted: {kind}"
        try:
            res: Result = handlers.run_confirmed(kind, payload, ctx, choice)
        except Exception as e:
            log.exception("confirmed %s failed", kind)
            self.notifier.send(handlers.Notice("Error", f"{kind}: {e}"))
            res = Result("failed", f"{kind} error: {e}", 422)
        self.store.set_state(ctx.delivery_id, res.state, summary=res.summary)
        ctx.trace("result", state=res.state, summary=res.summary, confirmed=True)
        log.info("delivery %s: (confirmed) %s: %s", ctx.delivery_id[:8], res.state, res.summary)
        return res.http_status, f"{res.state}: {res.summary}"


def make_handler(app: App):
    cfg = app.cfg
    path_token = cfg.secret("path_token") or ""
    activity_token = cfg.secret("activity_token") or ""
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
            if p.startswith("/confirm/"):       # a link preview or prefetch must never send or unlock
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if activity_token and p.startswith("/activity/"):
                parts = p.split("/")          # ['', 'activity', token, (delivery_id)]
                if len(parts) >= 3 and same(parts[2], activity_token):
                    base = f"/activity/{activity_token}"
                    if len(parts) == 3 or parts[3] == "":
                        page = activity.list_page(base, app.store.activity(100))
                    else:
                        did = urllib.parse.unquote(parts[3])
                        page = activity.detail_page(base, app.store.get_delivery(did), app.store.get_trace(did))
                    return self._html(page)
                return self.reply(404, "not found")
            if p.startswith("/answers/"):
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                page = app.answer_page(p.split("/", 2)[2], (q.get("r") or [""])[0])
                if page is None:
                    return self.reply(404, "not found")
                return self._html(page, forms=True)
            return self.reply(404, "not found")

        def _html(self, page: str, forms: bool = False):
            data = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            # The pages are text and inline CSS only. Nothing else may load or run, even if model
            # output slipped markup through, and the secret in the URL isn't sent on to linked sites.
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                             + ("form-action 'self'" if forms else "form-action 'none'"))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _read_chunked(self) -> bytes | None:
            """Read a Transfer-Encoding: chunked body; None if malformed or over MAX_BODY."""
            out = bytearray()
            try:
                while True:
                    size = int(self.rfile.readline(64).split(b";", 1)[0].strip(), 16)
                    if size == 0:
                        while self.rfile.readline(1024).strip():   # trailers
                            pass
                        return bytes(out)
                    if len(out) + size > MAX_BODY:
                        return None
                    out += self.rfile.read(size)
                    self.rfile.readline(8)                         # CRLF after each chunk
            except ValueError:
                return None

        def do_POST(self):
            p = self.path.split("?", 1)[0]
            if p.startswith("/confirm/"):
                return self._confirm(p)
            if not path_token or not same(p.rstrip("/"), "/hook/" + path_token):
                log.warning("rejected: POST to unknown path")
                return self.reply(404, "not found")
            if not secret:
                log.error("no signing_secret configured; refusing")
                return self.reply(503, "not configured")
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                body = self._read_chunked()
                length = len(body) if body is not None else 0
                if body is None:
                    log.warning("rejected: bad chunked body")
                    return self.reply(400, "bad length")
            else:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if length <= 0 or length > MAX_BODY:
                    log.warning("rejected: bad length (Content-Length=%r, Transfer-Encoding=%r)",
                                self.headers.get("Content-Length"), self.headers.get("Transfer-Encoding"))
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
                log.warning("delivery %s: ignored trigger %r", did[:8], v.trigger)
                return self.reply(200, "ignored trigger")
            with hook_lock:
                state = app.store.get_state(did)
                if state is not None and state != "failed":
                    log.info("delivery %s: duplicate (state=%s), not reprocessed", did[:8], state)
                    return self.reply(200, f"duplicate: {state}")
                app.store.set_state(did, "received")
            if state == "failed":       # "failed" means nothing happened, so the same delivery may run again
                log.info("delivery %s: retry after a failure", did[:8])
                app.store.add_trace(did, "retry", {"previous": "failed"})
            try:
                fields = protocol.parse_multipart(self.headers.get("Content-Type", ""), body)
            except Exception:
                log.warning("delivery %s: discarded, malformed multipart", did[:8])
                app.store.set_state(did, "discarded", summary="malformed multipart")
                return self.reply(400, "malformed multipart")
            app.store.add_trace(did, "received", {
                "trigger": v.trigger, "webhook_version": self.headers.get("X-Index-Webhook-Version"),
                "signed_at": v.timestamp, "signature": "valid", "client": fields.get("client"),
                "recorded_at": fields.get("recordedAt"), "body_bytes": length,
                "fields": sorted(fields), "audio_part": b'name="audio"' in body,
                "via": self.headers.get("X-Forwarded-For") or self.client_address[0]})
            transcript = (fields.get("transcription") or "").strip()
            if not transcript:
                log.warning("delivery %s: discarded, no transcription (fields: %s); set this "
                            "gesture's webhook to send Transcription", did[:8], ", ".join(sorted(fields)))
                app.store.set_state(did, "discarded", summary="no transcription part")
                return self.reply(200, "discarded: no transcription (set payload mode to include it)")
            code, text = app.process(did, transcript, recorded_time(fields.get("recordedAt")))
            return self.reply(code, text)

        def _confirm(self, p: str):
            parts = p.strip("/").split("/")
            if len(parts) != 3 or not (parts[2] in ("yes", "no") or parts[2] in "123456789" and len(parts[2]) == 1):
                return self.reply(404, "not found")
            code, text = app.confirm(parts[1], parts[2])
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            back = (q.get("from") or [""])[0]
            if back and app.llm_store.get_answer(back):     # the answer page's button: go back to it
                status = ("cancelled" if parts[2] == "no" else "expired" if code == 410
                          else "ok" if code < 400 else "failed")
                self.send_response(303)
                self.send_header("Location", f"/answers/{urllib.parse.quote(back, safe='')}?r={status}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self.reply(code, text)

    return Handler


def serve(cfg: Config) -> None:
    app = App(cfg)
    host, port = cfg.server.get("host", "127.0.0.1"), int(cfg.server.get("port", 8642))
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    mode = "TEST MODE: everything waits for Proceed" if cfg.server.get("test_mode") else "LIVE"
    log.info("ring-router listening on http://%s:%d (%s, notify=%s)", host, port, mode,
             type(app.notifier).__name__)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
