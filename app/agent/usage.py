"""Counting what a task cost, so the number is not a surprise on the invoice.

The Claude API reports four token meters on every response, and they are the
only ground truth about whether prompt caching is actually working:

===========================  ===========================================
``input_tokens``             processed at full price this turn
``cache_read_input_tokens``  served from the cache at about a tenth of it
``cache_creation_input_tokens``  written to the cache (1.25x for a 5-minute
                             entry, 2x for a one-hour one)
``output_tokens``            generated
===========================  ===========================================

The first is *only the uncached remainder* - a common misreading is to treat it
as the prompt size and conclude a long agent loop used 4,000 tokens of input.
The prompt size is the sum of the first three, which is why ``prompt_tokens``
below adds them up rather than reporting one.

This module holds no Qt and no SDK types: it is a running total plus the
arithmetic to phrase it, so it can be tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Published prices, US dollars per million tokens, for the models this
#: browser offers and Anthropic documents a price for. Used only to turn token
#: counts into a rough figure in the panel; a model absent from this table
#: simply shows tokens and no estimate, which is better than a made-up number.
#: Cache reads bill at 0.1x input and one-hour cache writes at 2x.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
}

_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 2.0     # the one-hour TTL this browser writes with


@dataclass
class Usage:
    """Running totals for one task, or for a whole session."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, response) -> None:
        """Fold in one model response. Tolerant of anything with the fields."""
        self.requests += 1
        self.input_tokens += getattr(response, "input_tokens", 0) or 0
        self.output_tokens += getattr(response, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(response, "cache_read_tokens", 0) or 0
        self.cache_write_tokens += getattr(response, "cache_write_tokens", 0) or 0

    def reset(self) -> None:
        self.requests = 0
        self.input_tokens = self.output_tokens = 0
        self.cache_read_tokens = self.cache_write_tokens = 0

    @property
    def prompt_tokens(self) -> int:
        """Everything sent to the model, cached or not."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Share of the prompt served from cache, 0.0 to 1.0.

        A healthy multi-turn loop settles high - Anthropic's measurements put a
        well-shaped agent loop at 81% to 90%. Persistently near zero means
        something is changing inside the cached prefix on every request.
        """
        total = self.prompt_tokens
        return (self.cache_read_tokens / total) if total else 0.0

    def estimated_cost(self, model: str) -> float | None:
        """Rough dollars, or None when the model's price is not known here.

        Deliberately returns None rather than guessing: an invented price is
        worse than no price, because it looks authoritative.
        """
        price = PRICES.get(model)
        if price is None:
            return None
        in_price, out_price = price
        million = 1_000_000
        return (
            self.input_tokens * in_price
            + self.cache_read_tokens * in_price * _CACHE_READ_MULTIPLIER
            + self.cache_write_tokens * in_price * _CACHE_WRITE_MULTIPLIER
            + self.output_tokens * out_price
        ) / million

    def uncached_cost(self, model: str) -> float | None:
        """What the same tokens would have cost with caching switched off.

        Every cached token would have been billed at the full input rate, and
        nothing would have been written. This is the comparison that says
        whether caching is earning its keep, and it is honest in the direction
        that matters: it never flatters caching, because a cache write really
        does cost more than not caching at all.
        """
        price = PRICES.get(model)
        if price is None:
            return None
        in_price, out_price = price
        return (self.prompt_tokens * in_price
                + self.output_tokens * out_price) / 1_000_000

    def summary(self, model: str = "") -> str:
        """One line for the panel. Empty when nothing has been sent yet."""
        if not self.requests:
            return ""
        parts = [f"{self.prompt_tokens:,} in / {self.output_tokens:,} out"]
        if self.cache_read_tokens:
            parts.append(f"{self.cache_hit_rate * 100:.0f}% from cache")
        spend = self.estimated_cost(model)
        if spend is not None:
            parts.append(f"about ${spend:.3f}")
            saved = self.uncached_cost(model)
            if saved is not None and saved > spend:
                parts.append(f"caching saved about ${saved - spend:.3f}")
        return " · ".join(parts)
