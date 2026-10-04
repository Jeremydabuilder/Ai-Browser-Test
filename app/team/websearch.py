"""Web search for the Researcher, through a supported search API.

Two providers are supported, both plain REST + a key (there is no keyless
search API that is both supported and sanctioned for programmatic use, and
scraping a search results page would be neither):

* **Tavily**  - https://tavily.com  (``TAVILY_API_KEY``). Built for LLM agents;
  returns a relevance-ranked passage per result.
* **Brave Search API** - https://brave.com/search/api  (``BRAVE_SEARCH_API_KEY``).

Keys follow the same rule as the model key: OS keyring first (account
``tavily-api-key`` / ``brave-search-api-key``), then the environment variable.
They are never stored in the settings table, logged, or put in a repr.

What is sent: only a short search *query* (secrets redacted by the app's
firewall first), never page contents, files or the mission text. What comes
back are the title, URL and snippet of each hit - the Team keeps the URL and
labels the source as a web search result, distinct from anything you attached.
Pages are not fetched; a snippet is search-engine text, not the full page.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any

import httpx2 as httpx

from app.security import firewall

MAX_RESULTS = 6
MAX_SNIPPET = 900
MAX_QUERY = 240


class SearchErrorKind:
    NO_CREDENTIAL = "no_credential"
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    PROVIDER = "provider"


class SearchError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchProvider:
    id: str
    label: str
    env_var: str
    account: str
    signup_url: str
    credential_note: str


PROVIDERS: dict[str, SearchProvider] = {
    "tavily": SearchProvider(
        "tavily", "Tavily", "TAVILY_API_KEY", "tavily-api-key", "https://app.tavily.com",
        "Create an account at tavily.com and copy an API key (they offer a limited free tier; "
        "check their current pricing)."),
    "brave": SearchProvider(
        "brave", "Brave Search", "BRAVE_SEARCH_API_KEY", "brave-search-api-key",
        "https://api-dashboard.search.brave.com",
        "Subscribe to a 'Search' plan in the Brave Search API dashboard and copy the subscription "
        "token (check their current plans and free allowance)."),
}


@dataclass
class SearchStatus:
    provider: str
    label: str
    available: bool
    detail: str
    origin: str = "none"
    secret: str = field(default="", repr=False)


def _key_store(provider: str):
    from app.agent.keys import ApiKeyStore

    return ApiKeyStore(account=PROVIDERS[provider].account)


def selected_provider(settings=None, env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    chosen = (source.get("PYBROWSER_SEARCH_PROVIDER") or "").strip().lower()
    if not chosen and settings is not None:
        try:
            chosen = (settings.get("team_search_provider", "") or "").strip().lower()
        except Exception:  # noqa: BLE001
            chosen = ""
    return chosen if chosen in PROVIDERS else ""


def resolve_search(settings=None, env: dict | None = None) -> SearchStatus:
    """Which search provider is selected and whether a key for it exists.
    Never raises, never returns the key in any displayable field."""
    source = env if env is not None else os.environ
    chosen = selected_provider(settings, env)
    if not chosen:
        return SearchStatus("", "", False,
                            "Web search is off. Pick Tavily or Brave Search in Limits and model… "
                            "and add its API key to enable it.")
    info = PROVIDERS[chosen]
    key = ""
    origin = "none"
    try:
        stored = _key_store(chosen).get_keyring_key()
    except BaseException:  # noqa: BLE001 - a broken keyring is not fatal
        stored = None
    if stored:
        key, origin = stored, "keyring"
    elif (source.get(info.env_var) or "").strip():
        key, origin = source[info.env_var].strip(), "environment"
    if not key:
        return SearchStatus(chosen, info.label, False,
                            f"No {info.label} API key found. Add it in Limits and model… (stored in "
                            f"your OS keyring) or set {info.env_var}. {info.credential_note}")
    where = "OS keyring" if origin == "keyring" else info.env_var
    return SearchStatus(chosen, info.label, True, f"{info.label} key from the {where}", origin, key)


def save_search_key(provider: str, key: str) -> None:
    _key_store(provider).set_key(key.strip())


def clear_search_key(provider: str) -> None:
    _key_store(provider).clear_key()


def safe_query(query: str) -> str:
    """A short query with likely secrets removed. This is the ONLY text that
    leaves the machine for a search."""
    text = " ".join((query or "").split())
    if firewall.is_enabled():
        text, _findings = firewall.redact(text, only_high_risk=True)
    return text[:MAX_QUERY]


_TAGS = re.compile(r"<[^>]+>")


def _clean(text: Any, limit: int = MAX_SNIPPET) -> str:
    return " ".join(unescape(_TAGS.sub("", str(text or ""))).split())[:limit]


def _http_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if url.lower().startswith(("http://", "https://")) and len(url) < 2000 else ""


class WebSearch:
    """A search client bound to one provider and one key."""

    def __init__(self, status: SearchStatus, *, transport: "httpx.BaseTransport | None" = None,
                 timeout: float = 15.0, sleep=time.sleep) -> None:
        if not status.available or not status.secret:
            raise SearchError(SearchErrorKind.NO_CREDENTIAL, status.detail)
        self.status = status
        self._transport = transport
        self._timeout = timeout
        self._sleep = sleep

    @property
    def label(self) -> str:
        return self.status.label

    def search(self, query: str, max_results: int = MAX_RESULTS) -> list[WebResult]:
        query = safe_query(query)
        if not query:
            return []
        count = max(1, min(int(max_results), MAX_RESULTS))
        for attempt in (0, 1):
            try:
                response = self._request(query, count)
            except httpx.TimeoutException as exc:
                raise SearchError(SearchErrorKind.NETWORK, f"{self.label} took too long to respond.") from exc
            except httpx.HTTPError as exc:
                raise SearchError(SearchErrorKind.NETWORK,
                                  f"Could not reach {self.label}. Check the network connection.") from exc
            if response.status_code == 429 and attempt == 0:
                delay = self._retry_after(response)
                if delay <= 5:
                    self._sleep(delay)
                    continue
            return self._parse(response)
        raise SearchError(SearchErrorKind.RATE_LIMIT, f"{self.label} is rate-limiting searches.")

    @staticmethod
    def _retry_after(response) -> float:
        try:
            return max(0.0, float(response.headers.get("retry-after", "1")))
        except ValueError:
            return 1.0

    def _request(self, query: str, count: int):
        key = self.status.secret
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            if self.status.provider == "tavily":
                return client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"query": query, "max_results": count, "search_depth": "basic",
                          "include_answer": False, "include_raw_content": False})
            return client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                params={"q": query, "count": count, "safesearch": "moderate", "text_decorations": "false"})

    def _parse(self, response) -> list[WebResult]:
        status = response.status_code
        label = self.label
        if status in (401, 403):
            raise SearchError(SearchErrorKind.AUTH,
                              f"{label} rejected the API key. Check it in Limits and model…")
        if status in (402, 432, 433):
            raise SearchError(SearchErrorKind.QUOTA, f"Your {label} plan's usage limit has been reached.")
        if status == 429:
            raise SearchError(SearchErrorKind.RATE_LIMIT, f"{label} is rate-limiting searches.")
        if status >= 400:
            raise SearchError(SearchErrorKind.PROVIDER, f"{label} rejected the search ({status}).")
        try:
            data = response.json()
        except ValueError as exc:
            raise SearchError(SearchErrorKind.PROVIDER, f"{label} sent back something unreadable.") from exc
        if self.status.provider == "tavily":
            raw = [(r.get("title"), r.get("url"), r.get("content")) for r in (data.get("results") or [])]
        else:
            raw = []
            for r in ((data.get("web") or {}).get("results") or []):
                extra = " ".join(r.get("extra_snippets") or [])
                raw.append((r.get("title"), r.get("url"), f"{r.get('description') or ''} {extra}"))
        results: list[WebResult] = []
        seen: set[str] = set()
        for title, url, snippet in raw:
            clean_url = _http_url(url)
            if not clean_url or clean_url in seen:
                continue
            seen.add(clean_url)
            results.append(WebResult(_clean(title, 160) or clean_url, clean_url, _clean(snippet)))
        return results[:MAX_RESULTS]
