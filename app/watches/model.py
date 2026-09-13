"""Page Watches: the data model. See app/watches/__init__.py."""

from __future__ import annotations

from dataclasses import dataclass


class WatchTarget:
    """What part of the page is being watched."""

    FULL_PAGE = "full_page"
    SELECTION = "selection"

    ALL = (FULL_PAGE, SELECTION)


class WatchCondition:
    """When does a check count as "meaningful", per the phase spec.

    ANY_CHANGE is the generic case; the rest are conditions a user can pick
    for a specific kind of watch - see app/watches/detection.py for how
    each is actually evaluated against an extracted value.
    """

    ANY_CHANGE = "any_change"
    VALUE_BELOW = "value_below"
    VALUE_ABOVE = "value_above"
    TEXT_CONTAINS = "text_contains"
    TEXT_NOT_CONTAINS = "text_not_contains"
    BECOMES_AVAILABLE = "becomes_available"

    ALL = (ANY_CHANGE, VALUE_BELOW, VALUE_ABOVE, TEXT_CONTAINS,
           TEXT_NOT_CONTAINS, BECOMES_AVAILABLE)

    #: Conditions that need a numeric baseline/threshold rather than text.
    NUMERIC = (VALUE_BELOW, VALUE_ABOVE)
    #: Conditions that need a substring to look for.
    TEXTUAL = (TEXT_CONTAINS, TEXT_NOT_CONTAINS)


class WatchState:
    ACTIVE = "active"
    PAUSED = "paused"
    #: Repeated check failures - see app/watches/runner.py. Distinct from
    #: PAUSED: the user did not choose this, and it is not "the page
    #: changed" either - it means "Py could not tell", which must never be
    #: reported as a detected change.
    NEEDS_ATTENTION = "needs_attention"

    ALL = (ACTIVE, PAUSED, NEEDS_ATTENTION)


@dataclass(frozen=True)
class Watch:
    id: int
    title: str
    url: str
    target_type: str
    #: The selected text at creation time, when target_type is SELECTION -
    #: used only to describe the watch to the user and to help re-find the
    #: section on the next check; never treated as instructions.
    selection_hint: str
    condition: str
    #: The threshold (numeric conditions) or substring (textual conditions),
    #: as plain text - "" for ANY_CHANGE/BECOMES_AVAILABLE, which need none.
    condition_value: str
    check_interval_seconds: int
    #: A compact fingerprint of the first observed content, not the content
    #: itself - see app/watches/detection.py normalize_text/hash_text. Never
    #: a full-page copy kept forever, per the phase spec.
    baseline_hash: str | None
    baseline_value: str | None
    last_observed_hash: str | None
    last_observed_value: str | None
    last_checked_at: str | None
    next_check_at: str | None
    state: str
    failure_count: int
    #: Set only once the user chooses "Turn change into Mission", or the
    #: watch was created attached to one. Never auto-populated.
    mission_id: int | None
    created_at: str
    updated_at: str
