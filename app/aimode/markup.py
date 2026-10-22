"""A small, safe Markdown -> HTML converter for model answers.

The model's text is untrusted. Everything is HTML-escaped FIRST; only then are a few patterns turned
into tags we generate ourselves (headings, lists, tables, code, bold/italic, http(s) links, ``[n]``
citation badges). There is no way for model output to introduce a tag, an attribute or a ``javascript:``
URL - which is exactly what the tests pin down.
"""

from __future__ import annotations

import html
import re

_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,500})\)")
_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+?)`")
_CITE = re.compile(r"\[(\d{1,2})\]")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d{1,2}[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(escaped: str, citations: set[int] | None) -> str:
    """Inline formatting on text that is ALREADY escaped."""
    text = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)

    def link(m: re.Match) -> str:
        url = m.group(2)                                  # already escaped; only http(s) matched
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", text)

    def cite(m: re.Match) -> str:
        n = int(m.group(1))
        if citations is not None and n not in citations:
            return m.group(0)
        return f'<sup class="cite"><a href="#src-{n}" data-n="{n}">{n}</a></sup>'

    return _CITE.sub(cite, text)


def to_html(text: str, citations: set[int] | None = None) -> str:
    """HTML for ``text``. ``citations``: the source numbers that exist (others stay literal)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(html.escape(l), citations) for l in paragraph) + "</p>")
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = min(4, len(heading.group(1)) + 1)          # h1 in an answer is h2 on the page
            out.append(f"<h{level}>{_inline(html.escape(heading.group(2).strip()), citations)}</h{level}>")
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head = "".join(f"<th>{_inline(html.escape(c), citations)}</th>" for c in header)
            body = "".join("<tr>" + "".join(f"<td>{_inline(html.escape(c), citations)}</td>" for c in row) + "</tr>"
                           for row in rows)
            out.append(f"<div class=\"table\"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
            continue
        for pattern, tag in ((_BULLET, "ul"), (_NUMBERED, "ol")):
            if pattern.match(line):
                flush()
                items: list[str] = []
                while i < len(lines) and pattern.match(lines[i]):
                    items.append(pattern.match(lines[i]).group(1))
                    i += 1
                out.append(f"<{tag}>" + "".join(f"<li>{_inline(html.escape(it), citations)}</li>" for it in items)
                           + f"</{tag}>")
                break
        else:
            paragraph.append(line)
            i += 1
            continue
    flush()
    return "".join(out)
