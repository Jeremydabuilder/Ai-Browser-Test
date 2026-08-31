"""Classifying how consequential a browser action is.

This module ONLY classifies. It never blocks, prompts, or gates anything - by
design, per the Phase 2 preparation brief. It exists so that a future caller
can ask "would this action need the user's blessing?" *before* performing it,
and so that every ActionResult carries that judgement for auditing afterwards.

Two deliberate design choices:

* **Advisory, not enforcing.** Wiring a confirmation prompt to these levels is
  a Phase 2 decision, and putting the policy here now would bake in an answer
  before the UI that has to present it exists.
* **Heuristic, and honest about it.** Matching English words against an
  accessible name will miss a localised "Kaufen" and will over-flag a link
  called "Delete draft". It is a safety net that biases toward asking, not a
  security boundary. The real boundary is that a caller cannot execute
  arbitrary JavaScript at all.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass, field
from typing import Any


class Sensitivity:
    """How much scrutiny an action deserves before it runs."""

    NORMAL = "normal"        # reading, scrolling, ordinary navigation
    ELEVATED = "elevated"    # writes something, leaves a trace, spends nothing
    SENSITIVE = "sensitive"  # money, identity, destruction, or legal consent


@dataclass(frozen=True)
class SensitivityAssessment:
    level: str = Sensitivity.NORMAL
    reasons: list[str] = field(default_factory=list)

    @property
    def requires_confirmation(self) -> bool:
        """Advisory: a future agent should ask the user before doing this."""
        return self.level == Sensitivity.SENSITIVE

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["requires_confirmation"] = self.requires_confirmation
        return data


def _phrases(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


# Grouped so a reason string can name *why*, not just "matched a word".
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (_phrases("buy", "purchase", "order now", "place order", "checkout", "check out",
              "pay", "payment", "subscribe", "donate", "add funds", "top up",
              "confirm and pay", "complete purchase", "bid"),
     Sensitivity.SENSITIVE, "may spend money or place an order"),
    (_phrases("delete", "remove", "erase", "destroy", "deactivate", "close account",
              "cancel subscription", "unsubscribe", "wipe", "permanently"),
     Sensitivity.SENSITIVE, "may delete or destroy something"),
    (_phrases("send", "post", "publish", "tweet", "reply", "comment", "share",
              "submit review", "message"),
     Sensitivity.SENSITIVE, "may publish or send a message on the user's behalf"),
    (_phrases("password", "passphrase", "2fa", "two-factor", "security question",
              "recovery", "api key", "access token", "revoke", "permissions",
              "privacy settings", "security settings"),
     Sensitivity.SENSITIVE, "may change credentials or security settings"),
    (_phrases("i agree", "accept terms", "terms of service", "privacy policy",
              "consent", "sign contract", "accept agreement", "e-sign"),
     Sensitivity.SENSITIVE, "may accept a legal agreement"),
    (_phrases("transfer", "withdraw", "send money", "wire", "refund"),
     Sensitivity.SENSITIVE, "may move money"),
    (_phrases("sign in", "log in", "login", "sign up", "register", "create account"),
     Sensitivity.ELEVATED, "authenticates or creates an account"),
    (_phrases("save", "update", "apply", "upload", "submit", "confirm", "continue"),
     Sensitivity.ELEVATED, "writes data back to the site"),
]

# Field semantics that mean "this text is secret or financial", independent of
# any wording on the page.
_SECRET_INPUT_TYPES = {"password"}
_SECRET_AUTOCOMPLETE = {
    "current-password", "new-password", "one-time-code",
    "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-name",
}
_FINANCIAL_FIELD = _phrases(
    "card number", "cardnumber", "creditcard", "credit card", "cvv", "cvc",
    "security code", "iban", "account number", "routing", "sort code",
    "ssn", "social security", "tax id", "passport",
)

# Extensions a browser should treat as "the user really must okay this".
_RISKY_DOWNLOADS = {
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1", ".vbs", ".jar",
    ".sh", ".bash", ".apk", ".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".msix",
}


def _rank(level: str) -> int:
    return {Sensitivity.NORMAL: 0, Sensitivity.ELEVATED: 1, Sensitivity.SENSITIVE: 2}[level]


def _combine(items: list[tuple[str, str]]) -> SensitivityAssessment:
    if not items:
        return SensitivityAssessment()
    level = max((lvl for lvl, _ in items), key=_rank)
    # Report only the reasons that justify the level we settled on.
    reasons = [reason for lvl, reason in items if lvl == level]
    return SensitivityAssessment(level=level, reasons=list(dict.fromkeys(reasons)))


def _match_text(*values: str) -> list[tuple[str, str]]:
    haystack = " ".join(v for v in values if v)
    if not haystack.strip():
        return []
    return [(level, reason) for pattern, level, reason in _PATTERNS if pattern.search(haystack)]


def classify_click(element: dict[str, Any] | None) -> SensitivityAssessment:
    """How consequential is clicking this element?"""
    if not element:
        return SensitivityAssessment()
    found = _match_text(element.get("name", ""), element.get("field_name", ""))

    href = str(element.get("href", ""))
    if element.get("download") or _has_risky_extension(href):
        found.append((Sensitivity.SENSITIVE, "starts a file download"))
    # A submit button inside a form is a write by definition, even if its label
    # is something bland like "Go".
    role = element.get("role", "")
    if role == "button" and element.get("form") is not None:
        found.append((Sensitivity.ELEVATED, "submits a form"))
    return _combine(found)


def classify_type(element: dict[str, Any] | None, text: str = "") -> SensitivityAssessment:
    """How consequential is typing this text into this field?"""
    if not element:
        return SensitivityAssessment()
    found: list[tuple[str, str]] = []
    if element.get("input_type", "") in _SECRET_INPUT_TYPES or element.get("secret"):
        found.append((Sensitivity.SENSITIVE, "enters a password"))
    if str(element.get("autocomplete", "")).lower() in _SECRET_AUTOCOMPLETE:
        found.append((Sensitivity.SENSITIVE, "enters credentials or payment details"))
    if _FINANCIAL_FIELD.search(" ".join(
        str(element.get(key, "")) for key in ("name", "field_name", "placeholder")
    )):
        found.append((Sensitivity.SENSITIVE, "enters financial or identity information"))
    if looks_like_payment_card(text):
        found.append((Sensitivity.SENSITIVE, "the text looks like a payment card number"))
    if not found:
        found.append((Sensitivity.ELEVATED, "enters data into the page"))
    return _combine(found)


def classify_submit(form: dict[str, Any] | None, fields: list[dict[str, Any]] | None = None) -> SensitivityAssessment:
    """Submitting a form is always at least a write, and often more."""
    found: list[tuple[str, str]] = [(Sensitivity.ELEVATED, "submits a form")]
    if form:
        found.extend(_match_text(form.get("name", ""), form.get("action", "")))
    for field_info in fields or []:
        assessment = classify_type(field_info)
        if assessment.level == Sensitivity.SENSITIVE:
            found.extend((assessment.level, reason) for reason in assessment.reasons)
    return _combine(found)


def classify_navigate(url: str) -> SensitivityAssessment:
    """Ordinary navigation is normal; a direct link to a risky file is not."""
    found: list[tuple[str, str]] = []
    if _has_risky_extension(url):
        found.append((Sensitivity.SENSITIVE, "starts a file download"))
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme and scheme not in ("http", "https", "about", "file", "data"):
        found.append((Sensitivity.ELEVATED, f"hands the '{scheme}' address to another application"))
    return _combine(found)


def classify_read() -> SensitivityAssessment:
    """Reading, scrolling and inspecting are always normal."""
    return SensitivityAssessment()


def _has_risky_extension(url: str) -> bool:
    """Does this URL point at a file whose extension we treat as risky?

    Only the final path segment counts. Matching against the whole URL would
    flag every https://example.com as an MS-DOS executable, because ".com" is
    both a hostname suffix and an executable extension - which is exactly the
    false positive this function existed to produce before.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    filename = path.rsplit("/", 1)[-1].lower()
    if "." not in filename:
        return False
    return any(filename.endswith(ext) for ext in _RISKY_DOWNLOADS)


def looks_like_payment_card(text: str) -> bool:
    """Luhn check on a 13-19 digit string, ignoring spaces and dashes."""
    digits = re.sub(r"[\s-]", "", text or "")
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
