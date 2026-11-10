"""Small Markdown -> HTML converter for model answers (answer page and Apple Notes bodies).

Handles what models actually emit: headings, paragraphs, bullet/numbered lists, bold, italic,
inline code, links, bare URLs, and simple pipe tables. Everything else is escaped text.
"""

from __future__ import annotations

import html
import re

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL = re.compile(r"(?<![\"'>=])(https?://[^\s<)]+)")


def inline(text: str) -> str:
    t = html.escape(text, quote=False)
    links: list[str] = []

    def keep(m):
        links.append(f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>')
        return f"\x00{len(links) - 1}\x00"

    t = _LINK.sub(keep, t)
    t = _URL.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], t)


def to_html(md: str) -> str:
    out: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i, para = 0, []

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            flush(); i += 1; continue
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            flush(); n = min(len(h.group(1)) + 1, 4)
            out.append(f"<h{n}>{inline(h.group(2))}</h{n}>"); i += 1; continue
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            flush()
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", line))
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]):
                items.append("<li>" + inline(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", lines[i])) + "</li>")
                i += 1
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>"); continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            flush()
            cells = lambda s: [c.strip() for c in s.strip().strip("|").split("|")]  # noqa: E731
            head = "".join(f"<th>{inline(c)}</th>" for c in cells(line))
            rows, i = [], i + 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append(f"<table><tr>{head}</tr>{''.join(rows)}</table>"); continue
        para.append(line.strip()); i += 1
    flush()
    return "\n".join(out)


def page(title: str, body_html: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: light dark; --bg:#fff; --fg:#1c1c1e; --muted:#6e6e73; --line:#d2d2d7; --link:#0066cc; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1c1e; --fg:#f5f5f7; --muted:#a1a1a6; --line:#3a3a3c; --link:#4da3ff; }} }}
body {{ background:var(--bg); color:var(--fg); font:17px/1.5 -apple-system, system-ui, sans-serif;
       max-width:720px; margin:0 auto; padding:20px 16px 48px; }}
a {{ color:var(--link); }} .meta {{ color:var(--muted); font-size:14px; }}
.q {{ border-left:3px solid var(--line); padding-left:12px; color:var(--muted); }}
table {{ border-collapse:collapse; width:100%; font-size:15px; }} td,th {{ border:1px solid var(--line); padding:6px 8px; text-align:left; }}
code {{ font:15px ui-monospace, monospace; }}
</style></head><body>{body_html}</body></html>"""
