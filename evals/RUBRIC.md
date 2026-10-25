# Manual scoring rubric

Score each run yourself, after reading the final answer **and opening the sources it cites**. Five dimensions, each
**0, 1 or 2** (total 0–10). Score the output, not the effort. Do not score a run you did not read in full.

| Dimension | 0 | 1 | 2 |
|---|---|---|---|
| **requirements** - every requirement the task states is addressed individually | ignored or silently dropped | some addressed, or lumped together | each one reported as met / not met / not found |
| **accuracy** - figures and facts match the inputs | an invented or wrong figure that matters | minor slip, or a figure lacking context | every checked figure matches its source |
| **evidence** - claims trace to sources, and snippets vs opened pages are distinguishable | no usable citations | citations present but some unverifiable or wrong | important claims cite sources you could open, and the answer says how each was read |
| **honesty** - gaps, conflicts and unreadable pages are visible; confidence is not inflated | hides a conflict/unreadable source or states a guess as fact | mentions some limits vaguely | conflicts shown with both sources; unknowns stated as unknown |
| **usefulness** - a person could act on it without reading internals | confusing, empty or only logs | usable with effort | clear recommendation (or an honest "cannot decide") with next steps |

Also record, per run: **user interventions** (every time you had to fix, retry, re-add something or work around the
system), **minutes** to a usable answer, **model calls and tokens** if shown, **cost** if you know the provider's prices, the
**provider/model/version** and the **run date**.

Fixture tasks also get *automatic keyword checks*. They catch a missing caveat or an invented figure cheaply; they do not
measure quality, and passing all of them does not make an answer correct.

## Comparing with another tool
Run the **same task text, the same inputs, the same day**, with the other tool's default settings and no extra help. Record
it with `python evals/run_eval.py record TASK --system "Name and version" --competitor ...`, then score it with this rubric
**blind to which system produced it if you can** (ask someone else to shuffle the answers). One run per task is an anecdote;
use several tasks and, where it matters, repeat runs. Do not publish a "beats X" claim from the smoke subset.
