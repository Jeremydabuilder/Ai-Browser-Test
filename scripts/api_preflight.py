"""Drive a real conversation per model, with the browser's exact parameters.

    python scripts/api_preflight.py                 # every model in the picker
    python scripts/api_preflight.py claude-opus-5   # just one

Answers the question the test suite cannot: *will the API accept what this
browser sends?* Every agent test drives a scripted transport, which proves the
loop is correct and says nothing about whether the request is valid - and that
is exactly how a `thinking` parameter went out to a model that rejects it and
broke every AI feature for anyone who picked the cheapest model in the list.

This builds the requests through the real ClaudeClient - same system prompt,
same 19 tool schemas, same caching, thinking, effort and context-management
parameters - and walks a whole conversation: the opening request, a tool_use
turn echoed back with a synthetic tool_result answering it, and a follow-up
after the assistant's text turn. Those are the three message shapes the agent
loop produces, and each has its own way of being rejected. It costs a few
thousand tokens per model.

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

#: How many tool round-trips to answer before giving up on reaching a text turn.
MAX_TOOL_ROUNDS = 4

#: Stands in for a real browser tool result. Deliberately mundane: the point is
#: the message shape, not the content.
TOOL_RESULT = ("Done. Tab 2 is at https://example.com and its title is "
               "\"Example Domain\". (preflight - no real browser attached)")


def check(model_id: str, credential) -> bool:
    """Drive a real, multi-turn conversation the way the agent loop does.

    A single request proves only that the API accepts the *first* payload. The
    failures that actually break the browser live later in a conversation: a
    tool_use turn echoed back, a tool_result answering it, and - since the
    retention fix - an assistant text turn kept in the history. So this walks
    all three shapes and stops at the first one the API refuses.
    """
    label = describe_model(model_id).label
    print(f"\n{model_id}  {DIM}{label}{OFF}")

    # A real client, so the request is assembled by the code under test rather
    # than by a copy of it here. from_environment(None) picks up
    # ANTHROPIC_WORKSPACE_ID exactly as the browser's settings-backed config
    # would - the same path an identity-linked key needs to succeed here too.
    config = AgentConfig.from_environment(None)
    config.model = model_id
    config.max_tokens = 1024
    client = ClaudeClient(credential, config)

    sent = []
    real_create = client._create

    def spy(**kwargs):
        sent.append(kwargs)
        return real_create(**kwargs)

    client._create = spy

    messages: list[dict] = [{
        "role": "user",
        "content": "Open https://example.com in a new tab, then tell me its title.",
    }]
    tool_rounds = 0
    stage = "first request"

    def ask() -> "object | None":
        try:
            return client.send(system=SYSTEM_PROMPT, messages=messages, tools=TOOL_SCHEMAS)
        except ClaudeError as exc:
            print(f"  {RED}FAILED{OFF}  at the {stage}: {exc.message}")
            if exc.api_message:
                print(f"          {YELLOW}{exc.api_message}{OFF}")
            return None
        except Exception as exc:                   # noqa: BLE001 - report, never crash
            print(f"  {RED}FAILED{OFF}  at the {stage}: {type(exc).__name__}: {exc}")
            detail = api_message_of(exc)
            if detail:
                print(f"          {YELLOW}{detail}{OFF}")
            return None

    response = ask()
    if response is None:
        return False
    first = sent[0]
    print(f"  {GREEN}ok{OFF}      first request accepted"
          f"  {DIM}thinking={first.get('thinking')} "
          f"effort={(first.get('output_config') or {}).get('effort')} "
          f"betas={first.get('betas')}{OFF}")

    # Round-trip every tool call the model makes, with a synthetic result. This
    # is the shape that a malformed history breaks: the assistant's tool_use
    # blocks go back verbatim, and each one must be answered.
    while response.wants_tools and tool_rounds < MAX_TOOL_ROUNDS:
        tool_rounds += 1
        names = ", ".join(call.name for call in response.tool_calls)
        messages.append({"role": "assistant", "content": response.raw_content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": TOOL_RESULT}
            for call in response.tool_calls
        ]})
        stage = f"tool round {tool_rounds} ({names})"
        response = ask()
        if response is None:
            return False
        print(f"  {GREEN}ok{OFF}      tool round {tool_rounds} accepted"
              f"  {DIM}answered {names}{OFF}")

    if not tool_rounds:
        print(f"  {YELLOW}note{OFF}    the model answered without calling a tool; "
              "the tool-result shape went untested")

    # The assistant's final text turn, kept in the history - the thing that was
    # being dropped before, which left the model unable to see its own replies.
    messages.append({"role": "assistant", "content": response.raw_content})
    messages.append({"role": "user", "content": "Thanks. Reply with the single word: ready."})
    stage = "follow-up turn"
    final = ask()
    if final is None:
        return False
    print(f"  {GREEN}OK{OFF}      follow-up accepted, model said "
          f"{final.text.strip()[:40]!r}")

    if client._unsupported:
        # Not a failure: the client gives a parameter up and retries. Worth
        # seeing, because each one costs money or quality.
        print(f"  {YELLOW}dropped {sorted(client._unsupported)} over "
              f"{len(sent)} request(s){OFF}")
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
    workspace_id = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    if workspace_id:
        print(f"workspace: {workspace_id} (from ANTHROPIC_WORKSPACE_ID)")

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
