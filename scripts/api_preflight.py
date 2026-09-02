"""Send one real request per model, with the browser's exact parameters.

    python scripts/api_preflight.py                 # every model in the picker
    python scripts/api_preflight.py claude-opus-5   # just one

Answers the question the test suite cannot: *will the API accept what this
browser sends?* Every agent test drives a scripted transport, which proves the
loop is correct and says nothing about whether the request is valid - and that
is exactly how a `thinking` parameter went out to a model that rejects it and
broke every AI feature for anyone who picked the cheapest model in the list.

This builds the request through the real ClaudeClient - same system prompt, same
19 tool schemas, same caching, thinking, effort and context-management
parameters - and sends it. It costs a few hundred tokens per model.

Exit status is 0 only if every model tried succeeded.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.agent.claude_client import ClaudeClient, ClaudeError, api_message_of  # noqa: E402
from app.agent.config import MODELS, AgentConfig, describe_model  # noqa: E402
from app.agent.credentials import resolve  # noqa: E402
from app.agent.prompt import SYSTEM_PROMPT  # noqa: E402
from app.agent.tools import TOOL_SCHEMAS  # noqa: E402

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def check(model_id: str, credential) -> bool:
    label = describe_model(model_id).label
    print(f"\n{model_id}  {DIM}{label}{OFF}")

    # A real client, so the request is assembled by the code under test rather
    # than by a copy of it here.
    config = AgentConfig(model=model_id, max_tokens=1024)
    client = ClaudeClient(credential, config)

    sent = []
    real_create = client._create

    def spy(**kwargs):
        sent.append(kwargs)
        return real_create(**kwargs)

    client._create = spy
    try:
        response = client.send(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "Reply with the single word: ready."}],
            tools=TOOL_SCHEMAS,
        )
    except ClaudeError as exc:
        print(f"  {RED}FAILED{OFF}  {exc.message}")
        if exc.api_message:
            print(f"          {YELLOW}{exc.api_message}{OFF}")
        return False
    except Exception as exc:                       # noqa: BLE001 - report, never crash
        print(f"  {RED}FAILED{OFF}  {type(exc).__name__}: {exc}")
        detail = api_message_of(exc)
        if detail:
            print(f"          {YELLOW}{detail}{OFF}")
        return False

    attempts = len(sent)
    first = sent[0] if sent else {}
    print(f"  {GREEN}OK{OFF}      answered {response.text.strip()[:40]!r}"
          f"  in {response.output_tokens} output tokens")
    print(f"  {DIM}sent    thinking={first.get('thinking')} "
          f"effort={(first.get('output_config') or {}).get('effort')} "
          f"betas={first.get('betas')}{OFF}")
    if client._unsupported:
        # Not a failure: the client gives a parameter up and retries. Worth
        # seeing, because each one costs money or quality.
        print(f"  {YELLOW}dropped {sorted(client._unsupported)} after "
              f"{attempts} attempt(s){OFF}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="*",
                        help="model ids to try (default: every one in the picker)")
    args = parser.parse_args()

    credential = resolve()
    if not credential.available:
        print(f"{RED}No API key.{OFF} Set ANTHROPIC_API_KEY, or add one in the "
              "browser under Tools -> Configure AI Agent.")
        return 2
    print(f"credential: {credential.describe()}")

    wanted = args.models or [choice.model_id for choice in MODELS]
    results = {model_id: check(model_id, credential) for model_id in wanted}

    print()
    broken = [model_id for model_id, ok in results.items() if not ok]
    if broken:
        print(f"{RED}{len(broken)} of {len(results)} models rejected the request: "
              f"{', '.join(broken)}{OFF}")
        print("The yellow line under each failure is the API's own words about "
              "what it objected to.")
        return 1
    print(f"{GREEN}All {len(results)} models accepted the browser's request.{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
