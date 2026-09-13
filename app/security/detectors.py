"""Conservative, deterministic detectors for likely secrets and personal
identifiers in free text.

Deliberately regex/heuristic only - never a call to another model to
decide whether something looks sensitive (that would mean sending the
very text we are trying to protect to a third party merely to ask about
it). False negatives are the accepted cost of that choice; a detector
here would rather miss an unusual token format than flag every long
identifier on the page as a "secret".
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class Category:
    PASSWORD = "PASSWORD"
    API_KEY = "API_KEY"
    ACCESS_TOKEN = "ACCESS_TOKEN"
    OAUTH_TOKEN = "OAUTH_TOKEN"
    PRIVATE_KEY = "PRIVATE_KEY"
    CREDIT_CARD = "CREDIT_CARD"
    CVV = "CVV"
    SSN = "SSN"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    SESSION_COOKIE = "SESSION_COOKIE"


class RiskLevel:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Which risk level each category carries by default - credentials/
#: payment/authentication data is always HIGH (per the phase's own
#: classification); personal identifiers are MEDIUM; everything else a
#: page might contain is LOW and never reaches a detector at all.
_RISK_BY_CATEGORY = {
    Category.PASSWORD: RiskLevel.HIGH,
    Category.API_KEY: RiskLevel.HIGH,
    Category.ACCESS_TOKEN: RiskLevel.HIGH,
    Category.OAUTH_TOKEN: RiskLevel.HIGH,
    Category.PRIVATE_KEY: RiskLevel.HIGH,
    Category.CREDIT_CARD: RiskLevel.HIGH,
    Category.CVV: RiskLevel.HIGH,
    Category.SSN: RiskLevel.HIGH,
    Category.SESSION_COOKIE: RiskLevel.HIGH,
    Category.EMAIL: RiskLevel.MEDIUM,
    Category.PHONE: RiskLevel.MEDIUM,
}

#: The replacement text for a redacted match - stable and specific enough
#: that a model can still reason about "there was an API key here"
#: without ever seeing it.
PLACEHOLDER_BY_CATEGORY = {
    Category.PASSWORD: "[REDACTED_PASSWORD]",
    Category.API_KEY: "[REDACTED_API_KEY]",
    Category.ACCESS_TOKEN: "[REDACTED_ACCESS_TOKEN]",
    Category.OAUTH_TOKEN: "[REDACTED_OAUTH_TOKEN]",
    Category.PRIVATE_KEY: "[REDACTED_PRIVATE_KEY]",
    Category.CREDIT_CARD: "[REDACTED_CREDIT_CARD]",
    Category.CVV: "[REDACTED_CVV]",
    Category.SSN: "[REDACTED_SSN]",
    Category.SESSION_COOKIE: "[REDACTED_SESSION_COOKIE]",
    Category.EMAIL: "[REDACTED_EMAIL]",
    Category.PHONE: "[REDACTED_PHONE]",
}


@dataclass(frozen=True)
class Match:
    category: str
    risk: str
    start: int
    end: int


# -- patterns ---------------------------------------------------------------
# Ordered so a more specific pattern (a labelled "password: ...") is tried
# before a more generic one that might otherwise also match its value.

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----[\s\S]+?"
    r"-----END (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")

# Common vendor-prefixed API key shapes (OpenAI, Anthropic, GitHub, Slack,
# AWS, Stripe, Google) plus a labelled generic fallback ("api_key: ...").
_VENDOR_API_KEY = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|xox[abp]-[A-Za-z0-9-]{16,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})\b")
_LABELLED_API_KEY = re.compile(
    r"(?i)\b(?:api[_ -]?key|secret[_ -]?key|client[_ -]?secret)\b\s*[:=]\s*"
    r"['\"]?([A-Za-z0-9_\-./+]{12,})['\"]?")

_LABELLED_ACCESS_TOKEN = re.compile(
    r"(?i)\b(?:access[_ -]?token|auth[_ -]?token|bearer)\b\s*[:= ]\s*"
    r"['\"]?([A-Za-z0-9_\-.=]{12,})['\"]?")

_SESSION_COOKIE = re.compile(
    r"(?i)\b(?:session[_ -]?id|sessionid|session[_ -]?token|"
    r"auth[_ -]?cookie)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-.%]{8,})['\"]?")

_LABELLED_PASSWORD = re.compile(
    r"(?i)\b(?:password|passwd|pwd)\b\s*[:=]\s*['\"]?([^\s'\"]{4,})['\"]?")

_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_PHONE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")

# A run of 13-19 digits, optionally grouped by spaces/dashes - only kept if
# it also passes a Luhn check (see _looks_like_card_number below), so an
# ordinary long number (an order id, a phone number already matched above)
# does not get flagged.
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_CVV_LABELLED = re.compile(r"(?i)\b(?:cvv|cvc|security code)\b\s*[:= ]\s*(\d{3,4})\b")


def _looks_like_card_number(digits: str) -> bool:
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


def find(text: str) -> list[Match]:
    """Every likely secret/identifier in ``text``, as non-overlapping
    matches. Conservative by construction - see the module docstring."""
    if not text:
        return []
    matches: list[Match] = []
    claimed: list[tuple[int, int]] = []

    def _claim(start: int, end: int) -> bool:
        for existing_start, existing_end in claimed:
            if start < existing_end and end > existing_start:
                return False
        claimed.append((start, end))
        return True

    def _add(category: str, start: int, end: int) -> None:
        if _claim(start, end):
            matches.append(Match(category=category, risk=_RISK_BY_CATEGORY[category],
                                 start=start, end=end))

    for m in _PRIVATE_KEY.finditer(text):
        _add(Category.PRIVATE_KEY, m.start(), m.end())
    for m in _JWT.finditer(text):
        _add(Category.OAUTH_TOKEN, m.start(), m.end())
    for m in _VENDOR_API_KEY.finditer(text):
        _add(Category.API_KEY, m.start(), m.end())
    for m in _LABELLED_API_KEY.finditer(text):
        _add(Category.API_KEY, m.start(1), m.end(1))
    for m in _SESSION_COOKIE.finditer(text):
        _add(Category.SESSION_COOKIE, m.start(1), m.end(1))
    for m in _LABELLED_ACCESS_TOKEN.finditer(text):
        _add(Category.ACCESS_TOKEN, m.start(1), m.end(1))
    for m in _LABELLED_PASSWORD.finditer(text):
        _add(Category.PASSWORD, m.start(1), m.end(1))
    for m in _CVV_LABELLED.finditer(text):
        _add(Category.CVV, m.start(1), m.end(1))
    for m in _SSN.finditer(text):
        _add(Category.SSN, m.start(), m.end())
    for m in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if _looks_like_card_number(digits):
            _add(Category.CREDIT_CARD, m.start(), m.end())
    for m in _PHONE.finditer(text):
        _add(Category.PHONE, m.start(), m.end())
    for m in _EMAIL.finditer(text):
        _add(Category.EMAIL, m.start(), m.end())

    matches.sort(key=lambda m: m.start)
    return matches
