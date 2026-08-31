"""Prove that prompt caching is actually working, against the real API.

Caching failures are silent. The requests still succeed and the answers are
still correct; only the invoice changes. The response `usage` meters are the
only ground truth, so this script sends the same representative request twice -
byte-identical, with the browser's real system prompt and real tool schemas -
and prints all four meters for each.

    python scripts/cache_probe.py

The second request must report a non-zero `cache_read_input_tokens`. If it does
not, something is changing inside the cached prefix between requests and every
turn of every task is being billed at full price. The script exits non-zero in
that case so it can be wired into a check.

This spends real money - two short requests, no tools are executed and no
browser is started - so it is a deliberate command, never run as part of the
test suite. Run it after any change to how the prompt is assembled.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.agent.claude_client import ClaudeClient  # noqa: E402
from app.agent.config import AgentConfig  # noqa: E402
from app.agent.credentials import resolve  # noqa: E402
from app.agent.prompt import SYSTEM_PROMPT  # noqa: E402
from app.agent.tools import TOOL_SCHEMAS  # noqa: E402

QUESTION = "Reply with the single word: ready."


def main() -> int:
    credential = resolve()
    if not credential.available:
        print("No credential configured. Sign in with `ant auth login`, set "
              "ANTHROPIC_API_KEY, or add a key in Tools -> Configure AI Agent.")
        return 2

    config = AgentConfig.from_environment()
    print(f"credential : {credential.describe()}")
    print(f"model      : {config.model}")
    print(f"effort     : {config.effort_level or 'model default'}")
    print(f"caching    : prefix={config.cache.prefix} ttl={config.cache.prefix_ttl} "
          f"conversation={config.cache.conversation}")
    print(f"prefix size: system {len(SYSTEM_PROMPT):,} chars, "
          f"{len(TOOL_SCHEMAS)} tool schemas\n")

    client = ClaudeClient(credential, config)
    messages = [{"role": "user", "content": QUESTION}]

    reads: list[int] = []
    for attempt in (1, 2):
        response = client.send(
            system=SYSTEM_PROMPT, messages=messages, tools=TOOL_SCHEMAS)
        reads.append(response.cache_read_tokens)
        print(f"request {attempt}: "
              f"input {response.input_tokens:,} · "
              f"cache write {response.cache_write_tokens:,} · "
              f"cache read {response.cache_read_tokens:,} · "
              f"output {response.output_tokens:,}")

    print()
    if reads[1] > 0:
        print(f"PASS: the second request read {reads[1]:,} tokens from the cache, "
              "so the static prefix is being reused.")
        return 0
    if not config.cache.enabled:
        print("Caching is switched off (PYBROWSER_AGENT_CACHE), so no read was "
              "expected. Unset it and run again to measure.")
        return 1
    print("FAIL: the second request read nothing from the cache.\n"
          "Either the prefix is shorter than this model's minimum cacheable "
          "size, or something in the tools or system prompt differs between "
          "requests. Diff the two request payloads; the first difference "
          "inside the shared part is the cause.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
