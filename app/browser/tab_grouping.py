"""AI-assisted tab grouping: suggest which open tabs belong together.

Deliberately cheap by default: a suggestion is built from title, domain and
URL alone - never a tab's page content - and only escalates to asking a
configured model provider when that metadata is what is being classified
(the provider still only ever sees the same metadata, never full pages).
Nothing here ever touches an actual tab; ``TabManager.move_tab_to_group``
does that, and only once the user accepts a suggestion (see
``app/ui/tab_grouping_dialog.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class TabMeta:
    """The only thing a suggestion is built from - never page content."""

    index: int
    title: str
    url: str

    @property
    def domain(self) -> str:
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return ""   # an internal page, about:/data:, or anything else
                        # that is not really "a website" has no real domain
        host = parts.netloc
        return host[4:] if host.startswith("www.") else host


@dataclass(frozen=True)
class GroupSuggestion:
    name: str
    tab_indices: tuple[int, ...]


def suggest_groups_by_domain(tabs: list[TabMeta], *, min_size: int = 2) -> list[GroupSuggestion]:
    """The offline fallback: tabs sharing a domain, when there are at least
    ``min_size`` of them. Cheap, deterministic, no network call - what
    "Suggest Groups" falls back to when no AI classifier is configured, or
    the classifier's own output cannot be trusted (see
    ``suggest_groups_with_ai``).
    """
    by_domain: dict[str, list[int]] = {}
    for tab in tabs:
        if not tab.domain:
            continue
        by_domain.setdefault(tab.domain, []).append(tab.index)
    return [GroupSuggestion(name=domain, tab_indices=tuple(indices))
           for domain, indices in by_domain.items() if len(indices) >= min_size]


#: What the model is asked, and the exact shape its answer must take. Kept
#: narrow on purpose - a name and a list of indices, nothing that could be
#: mistaken for a tool call or an instruction to act on the browser.
_PROMPT = """You group a browser's open tabs by topic, from metadata only.

Reply with ONLY a JSON array, no other text. Each element:
{{"name": "<2-4 word topic name>", "tab_indices": [<int>, ...]}}

Rules:
- Every value in tab_indices must be one of the indices given below.
- Only group tabs that clearly share a topic - do not force every tab into
  a group. A tab with nothing else like it should appear in no group.
- A group needs at least 2 tabs.

Tabs:
{tabs_json}
"""


def suggest_groups_with_ai(transport, tabs: list[TabMeta]) -> list[GroupSuggestion] | None:
    """Ask the configured model provider to classify tabs by topic.

    ``transport`` is anything shaped like ``ClaudeTransport.send()`` (see
    app/agent/claude_client.py) - the same object every provider client in
    this codebase already implements, so this reuses the existing provider
    abstraction rather than adding a second way to reach a model. Returns
    None on any failure at all - no transport, a request error, output that
    isn't valid JSON, or JSON that doesn't name real tab indices - so the
    caller always has a safe fallback (``suggest_groups_by_domain``) rather
    than ever applying something malformed.
    """
    if transport is None or not tabs:
        return None
    payload = [{"index": t.index, "title": t.title[:80], "domain": t.domain} for t in tabs]
    prompt = _PROMPT.format(tabs_json=json.dumps(payload))
    try:
        response = transport.send(
            system="", messages=[{"role": "user", "content": prompt}], tools=[])
    except Exception:  # noqa: BLE001 - any transport failure just means "no AI suggestion"
        return None
    return _parse_suggestions(getattr(response, "text", "") or "",
                              valid_indices={t.index for t in tabs})


def _parse_suggestions(text: str, *, valid_indices: set[int]) -> list[GroupSuggestion] | None:
    """Strict, fail-safe parsing: anything not shaped exactly right drops
    that one entry rather than raising, and a response with nothing usable
    in it returns None so the caller falls back cleanly."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    suggestions = []
    seen_names = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        indices = entry.get("tab_indices")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(indices, list):
            continue
        clean = sorted({i for i in indices if isinstance(i, int) and i in valid_indices})
        if len(clean) < 2:
            continue
        clean_name = name.strip()[:40]
        if clean_name in seen_names:
            continue
        seen_names.add(clean_name)
        suggestions.append(GroupSuggestion(name=clean_name, tab_indices=tuple(clean)))
    return suggestions if suggestions else None


def suggest_groups(tabs: list[TabMeta], *, transport=None) -> list[GroupSuggestion]:
    """The one entry point "Suggest Groups" calls: try the AI classifier if
    one is configured, fall back to the domain heuristic otherwise or on
    any failure. Never raises, never returns something that would touch a
    tab index that does not exist.
    """
    ai_result = suggest_groups_with_ai(transport, tabs)
    if ai_result is not None:
        return ai_result
    return suggest_groups_by_domain(tabs)
