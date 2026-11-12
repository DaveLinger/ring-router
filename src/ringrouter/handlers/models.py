"""Cloud model routes: ask-a-model, research, and the general agent (with its reroute hook)."""

from __future__ import annotations

import logging

from .. import md
from ..llm import Reply, load_profile
from ..notify import Button
from ..prompts import prompt
from ..router import Route, route
from .context import Context, Result

log = logging.getLogger(__name__)


def system_prompt(*names: str) -> str:
    parts = [prompt("spoken_preamble"), load_profile()] + [prompt(n) for n in names]
    return "\n\n".join(p for p in parts if p)


def _short(text: str, limit: int) -> str:
    t = " ".join(md.plain(text).split())
    return t if len(t) <= limit else t[: limit - 1].rsplit(" ", 1)[0] + "…"


def h_llm(r: Route, ctx: Context) -> Result:
    """'Ask <model> ...': answer as a notification; full answer + sources on the answer page."""
    key = r.params["model"]
    m = ctx.cfg.raw["models"][key]
    name = m.get("display", key)
    oc = ctx.cfg.section("openrouter")
    history = ctx.llm_store.history(key, int(oc.get("followup_window_s", 600)))
    question = r.body
    if history:   # make the follow-up standalone so the web search has something real to find
        prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        try:
            rewritten = ctx.fm.standalone(prev, r.body).strip().strip('"')
            if rewritten and len(rewritten) < 4 * max(len(r.body), 40):
                question = rewritten
        except Exception as e:
            log.warning("follow-up rewrite failed, sending as spoken: %s", e)
    messages = ([{"role": "system", "content": system_prompt("llm_answer")}] + history
                + [{"role": "user", "content": question}])
    try:
        reply = ctx.llm.chat(m["model"], messages, web=bool(m.get("web_search", oc.get("web_search_ask", True))),
                             web_results=int(oc.get("web_results_ask", 3)))
    except Exception as e:
        ctx.notify(name, f"Not asked: {e}")
        return Result("failed", str(e))
    ctx.llm_store.remember(key, question, reply.text)
    aid = ctx.llm_store.save_answer("ask", m["model"], question, reply)
    limit = int(ctx.cfg.section("notify").get("max_chars", 300))
    buttons = []
    if len(reply.text) > limit or reply.citations:
        buttons = [Button("Full answer", f"{ctx.public_url}/answers/{aid}")]
    ctx.notify(name, _short(reply.text, limit), buttons)
    follow = f", follow-up of {len(history) // 2}" if history else ""
    cost = f", ${reply.cost:.4f}" if reply.cost is not None else ""
    return Result("done", f"{name} answered ({len(reply.text)} chars, {len(reply.citations)} sources{follow}{cost})")


def h_research(r: Route, ctx: Context) -> Result:
    """'Research ...': web-backed write-up saved as an Apple Note; Pushcut only says it's ready."""
    key = r.params.get("model") or ctx.cfg.section("research").get("default_model", "chatgpt")
    m = ctx.cfg.raw["models"][key]
    rc = ctx.cfg.section("research")
    folder = rc.get("notes_folder", "Research")
    messages = [{"role": "system", "content": system_prompt("research")}, {"role": "user", "content": r.body}]
    try:
        reply = ctx.llm.chat(m["model"], messages, web=True, web_results=int(rc.get("web_results", 8)))
    except Exception as e:
        ctx.notify("Research", f"Not done: {e}")
        return Result("failed", str(e))
    aid = ctx.llm_store.save_answer("research", m["model"], r.body, reply)
    try:
        title = ctx.fm.title(r.body)
    except Exception:
        title = _short(r.body, 60)
    body = reply.text
    if reply.citations and "sources" not in body.lower()[-2000:]:
        body += "\n\n## Sources\n" + "\n".join(f"- [{c['title']}]({c['url']})" for c in reply.citations)
    link = [Button("Read it", f"{ctx.public_url}/answers/{aid}")]
    if ctx.dry("research"):
        ctx.notify("Research ready (dry run)", f"“{title}” — note not saved", link)
        return Result("dry_run", f"DRY RUN: would save note {title!r} in {folder!r} ({len(body)} chars)")
    ctx.notes_writer(title, md.to_html(body), folder)
    ctx.notify("Research ready", f"“{title}” in Notes › {folder}", link)
    return Result("done", f"research note {title!r} in {folder!r} ({len(body)} chars)")


def h_agent(r: Route, ctx: Context, kind: str) -> Result:
    """General agent: questions and requests (macOS tools without mail/sending, web search)."""
    window = int(ctx.cfg.section("openrouter").get("followup_window_s", 600))
    history = ctx.llm_store.history(AGENT_THREAD, window)
    def rerouter(command: str) -> tuple[str, str]:
        """Run a trimmed command from the top of the pipeline (called by the agent's run_command)."""
        sub_r = route(command, ctx.cfg)
        if sub_r.route == "agent":
            return "agent", "not a fixed command"
        if sub_r.route == "imessage" and ctx.cfg.section("agents").get("confirm_rerouted_messages", True):
            sub_r.params["tier"] = "weak"            # noise around it: ask before texting anyone
            sub_r.params["heard"] = f"{sub_r.params.get('heard', '')} (picked out of background noise)"
        if sub_r.route == "home" and sub_r.params.get("action") in ctx.cfg.section("agents").get(
                "confirm_rerouted_home", ["unlock", "open"]):
            sub_r.params["confirm"] = True           # said directly it just runs; dug out of noise, ask first
        log.info("delivery %s: agent rerouted %r -> %s", ctx.delivery_id[:8], command, sub_r.describe())
        ctx.trace("reroute", command=command, route=sub_r.route, reason=sub_r.reason, params=sub_r.params,
                  extracted=sub_r.body)
        from . import run  # noqa: PLC0415  (the registry in __init__ imports this module)
        res = run(sub_r, ctx)
        return sub_r.route, f"{res.state}: {res.summary}"

    try:
        text, spec = ctx.agent_runner(kind, r.body, ctx.cfg, history, rerouter)
    except Exception as e:
        log.exception("%s agent failed", kind)
        ctx.notify("Not done", f"{e}. Heard: \u201c{r.transcript}\u201d")
        return Result("failed", f"{kind} agent error: {e}")
    ctx.llm_store.remember(AGENT_THREAD, r.body, text)
    ctx.trace("agent", kind=kind, history_turns=len(history) // 2, tool_calls=list(spec.calls),
              sources=list(getattr(spec, "citations", [])), reply=text)
    if getattr(spec, "rerouted", None):   # the rerouted route already notified the user
        rr = spec.rerouted[0]
        return Result("done", f"agent rerouted {rr['command']!r} -> {rr['route']}: {rr['summary']}")
    calls = ", ".join(f"{c['tool']}.{c['action']}={c['result']}" for c in spec.calls) or "no tools"
    limit = int(ctx.cfg.section("notify").get("max_chars", 300))
    dry = any(c["result"] == "dry_run" for c in spec.calls)
    wrote = any(c["action"] not in ("read", "search") and c["result"] in ("ok", "dry_run") for c in spec.calls)
    label = "Done" if wrote else "Answer"
    buttons = []
    if len(md.plain(text)) > limit or spec.citations:
        model_id = ctx.cfg.section("agents").get("model", "anthropic/claude-sonnet-5.5")
        aid = ctx.llm_store.save_answer(kind, model_id, r.body, Reply(text, spec.citations, model_id))
        buttons = [Button("Full answer", f"{ctx.public_url}/answers/{aid}")]
    ctx.notify(label + (" (dry run)" if dry else ""), _short(text, limit), buttons)
    follow = f", follow-up of {len(history) // 2}" if history else ""
    return Result("dry_run" if dry else "done", f"{kind} agent: {_short(text, 120)} [{calls}{follow}]")


AGENT_THREAD = "agent"   # follow-up thread key for the general agent


def _default_agent_runner(kind: str, request: str, cfg, history: list[dict] | None = None, rerouter=None):
    from ..agents import run_agent  # noqa: PLC0415  (pydantic-ai imported only when an agent runs)
    return run_agent(kind, request, cfg, history=history, rerouter=rerouter)
