# Py AI mascot artwork

Drop the character's artwork in this folder, named after the state it shows:

| File | Shown when |
|---|---|
| `idle.svg` | nothing is happening |
| `reading.svg` | a page is being read |
| `thinking.svg` | waiting on the model |
| `working.svg` | a browser action is running |
| `complete.svg` | a task just finished (~2.6s, then back to idle) |
| `approval.svg` | waiting for the user to allow or deny |

`.png` works too, and `<state>@2x.png` is preferred on a high-DPI screen.

Only `idle` is required — any missing state falls back to it, and with no files
at all the browser draws a built-in placeholder. Nothing in the code needs to
change when real artwork arrives.

Square artwork, please: it is drawn into a square box (40px in the agent panel,
64px on the new-tab page) and anything else will be letterboxed.
