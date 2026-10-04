"""Live end-to-end check of the AI Team against the real model provider.

    python scripts/team_live_check.py            (Windows: python scripts\team_live_check.py)

It resolves the credential exactly as the browser does (OS keyring entry saved
by Tools -> Configure AI Agent, else the provider's environment variable),
sends one tiny request to prove the key and network work, then runs a small
real mission - Coordinator -> Researcher -> Writer -> Reviewer -> final - on a
few pasted paragraphs and prints a report. The key is never printed.

Exit codes:  0 = the live mission ran and produced a reviewed result
             1 = it ran but failed (see the report)
             2 = BLOCKED before running: no credential, invalid credential, or
                 the provider is unreachable from this machine.

Options:  --model NAME   override the model       --web   also use web search
          --provider ID  groq (default) | openai | openrouter | gemini
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SOURCES = (
    ("Widget A - product page", "https://shop.example/widget-a",
     "Widget A costs $10, ships in 2 days and is rated to last 2 years. It has a 30 day return window."),
    ("Widget B - product page", "https://shop.example/widget-b",
     "Widget B costs $14, ships in 5 days and is rated to last 5 years. It has a 90 day return window "
     "and a free replacement if it fails in the first year."),
)
GOAL = ("Compare Widget A and Widget B using the attached pages and write a short recommendation for "
        "someone who keeps things for many years. Cite the sources.")


def _utf8_console() -> None:
    """A Windows console defaults to cp1252 and would crash on the model's
    unicode (curly quotes, arrows) when printing the result."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main() -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--web", action="store_true")
    args = parser.parse_args()

    from app.agent.claude_client import ClaudeError  # noqa: F401  (import check)
    from app.team import sandbox as sandbox_mod
    from app.team import websearch
    from app.team.engine import Capabilities, TeamEngine
    from app.team.limits import TeamLimits
    from app.team.llm import make_client_factory, redact, resolve_provider
    from app.team.model import Mission, Source, SourceKind

    class Settings:  # only the overrides given on the command line
        def get(self, key, default=""):
            return {"team_model": args.model, "team_provider": args.provider}.get(key, default) or default

    status = resolve_provider(Settings())
    print(f"Provider : {status.label} ({status.provider})")
    print(f"Model    : {status.model or '(none chosen)'}")
    if not status.available:
        print("\nBLOCKED - no usable credential.\n  " + status.detail)
        print("  Fix: Tools -> Configure AI Agent -> choose Groq -> paste your key -> Save,\n"
              "       or set GROQ_API_KEY and run this again.")
        return 2
    print(f"Key      : {status.detail}")

    from app.agent.openai_compatible import GeminiClient, GroqClient, OpenAIClient, OpenRouterClient
    client_class = {"groq": GroqClient, "openai": OpenAIClient, "openrouter": OpenRouterClient,
                    "gemini": GeminiClient}[status.provider]
    ok, message = client_class.test_connection(status.secret, status.model)
    message = redact(message, status.secret)
    print(f"Preflight: {'ok' if ok else 'FAILED'} - {message}")
    if not ok:
        reason = ("the provider could not be reached from this machine" if "Could not reach" in message
                  else "the provider rejected the request")
        print(f"\nBLOCKED - {reason}. Nothing was run.")
        return 2

    limits = TeamLimits(max_concurrency=1, max_tasks=4, max_model_calls=16, max_revision_rounds=1,
                        task_timeout_s=180.0, max_retries=3)
    sources = [Source(f"S{i}", SourceKind.PASTE, title, url, text) for i, (title, url, text) in enumerate(SOURCES, 1)]
    web = None
    if args.web:
        search_status = websearch.resolve_search(None)
        web = websearch.WebSearch(search_status) if search_status.available else None
        print(f"Web      : {search_status.label + ' ready' if web else search_status.detail}")
    mission = Mission(goal=GOAL, sources=sources, web_search=web is not None,
                      model_label=f"{status.label} {status.model}")
    engine = TeamEngine(mission, make_client_factory(status, Settings(), limits), limits,
                        Capabilities(sandbox_mod.SandboxStatus(False, "not needed for this check"), web_search=web),
                        secret=status.secret, provider_label=status.label)
    print("\nRunning a live mission (this makes ~5-8 model calls)...")
    started = time.monotonic()
    engine.run()
    elapsed = time.monotonic() - started

    print(f"\nStatus   : {mission.status}  ({elapsed:.0f}s, {mission.model_calls} model calls, "
          f"{mission.input_tokens}+{mission.output_tokens} tokens)")
    print("Tasks    :")
    for task in mission.tasks:
        print(f"  {task.id} {task.agent:<10} {task.status:<9} {task.title}"
              + (f"  [{task.error}]" if task.error else ""))
    chain = " -> ".join(dict.fromkeys(t.agent for t in mission.tasks if t.status == "done"))
    print(f"Agents   : coordinator -> {chain} -> coordinator (final)")
    if mission.error:
        print(f"Error    : {mission.error}")
    final = mission.artifact(mission.final_artifact_id)
    if final is not None:
        print("\n--- Final result (first 900 characters) ---\n" + final.content[:900])
    done = {t.agent for t in mission.tasks if t.status == "done"}
    passed = final is not None and {"researcher", "writer", "reviewer"} <= done
    print("\nRESULT   : " + ("PASS - the live Coordinator -> Researcher -> Writer -> Reviewer flow ran."
                            if passed else "FAIL - see the report above."))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
