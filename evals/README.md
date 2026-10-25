# Evaluation pack (optional, free to read, opt-in to run)

Two layers, deliberately separate:

1. **Deterministic checks** - `python tests/run.py` (fixture pages, scripted providers, no network, no cost). They prove
   that requirements survive planning, that conflicts and unreadable pages stay visible, that restart/resume does not redo
   finished work, and so on. They say **nothing** about how good a real model is.
2. **Live evaluation** (this folder) - 24 tasks with a manual rubric to measure a real provider on realistic work.
   **Nothing here runs or spends anything unless you opt in.**

## Quick start (about 30 minutes, free-tier friendly)
```
python evals/run_eval.py list --smoke          # six tasks: 3 fixture + 3 real-world
python evals/run_eval.py show fx-laptops-requirements
python evals/run_eval.py run --smoke           # prints the PLAN and runs nothing
```
To run the **fixture** tasks against a real model (they send only the small fictional pages in `fixtures/`):
```
set PYBROWSER_EVAL_LIVE=1                       # PowerShell: $env:PYBROWSER_EVAL_LIVE = "1"
python evals/run_eval.py run --smoke --live --yes --provider gemini --model <model id>
```
The key is resolved the way the browser does (OS keyring or the provider's environment variable); this tool never reads
it into a file, a log or a result. A local model (`--provider ollama --model llama3.1:8b`) costs nothing.
`--price-in/--price-out` (USD per million tokens) fill the cost field if you want it.

The `lv-*` tasks use real sites, so a person runs them **in the app** (AI Team or Missions), then records the result:
```
python evals/run_eval.py record lv-headphones --system "PyBrowser 0.1.0 (app)" --provider gemini --model <id> \
    --minutes 7 --calls 9 --interventions 1 --notes "page 2 was a sign-in wall"
python evals/run_eval.py score evals/results/<file>.json --set requirements=2 accuracy=1 evidence=2 honesty=2 usefulness=1
python evals/run_eval.py report
```
Competitors: same tasks, same inputs, same day, `record ... --competitor`. See `RUBRIC.md` for how to score fairly.

## Files
* `tasks.json` - the tasks: wording, inputs, requirements, observable success criteria, failure conditions, time limit.
* `fixtures/` - fictional product/pricing/article pages, including a sign-in wall, conflicting prices and a prompt-injection page.
* `results/` - your run sheets (JSON). Not part of the repository's evidence unless you commit them on purpose.
* `RUBRIC.md` - 5 x 0-2 manual scoring and the comparison protocol.

## What this pack can and cannot show
* It can show whether a given model/provider, in this app, keeps your requirements, cites what it read, flags conflicts and
  gaps, and how many times you had to step in.
* It cannot show that PyBrowser is better than another tool until comparable, scored runs of both exist. Live pages change:
  always record the date. A scripted run (`--scripted`) only checks the harness and is excluded from reports.
