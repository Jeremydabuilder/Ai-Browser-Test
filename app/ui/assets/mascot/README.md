# Py artwork

Drop the final Py files straight into **this folder**. That is the whole
integration — no code changes, no registration, no rebuild.

| File | Shown when |
|---|---|
| `idle.*` | nothing is happening |
| `reading.*` | a page is being read |
| `thinking.*` | waiting on the model |
| `working.*` | a browser action is running |
| `approval.*` | waiting for the user to allow or deny |
| `complete.*` | a task finished with an answer (~2.6s, then idle) |
| `stuck.*` | a task was stopped or failed — never shown as success |

## Rules

* **Only `idle` is required.** Any missing state falls back to `idle`, and a
  single final `idle.png` outranks the entire placeholder set — so one file is
  enough to make Py the new character everywhere.
* **Extensions**, tried in this order: `.gif`, `.webp`, `.apng`, `.png`, `.svg`.
  An animated file wins over a still one of the same name, and plays through
  `QMovie` in place of the built-in motion.
* **`<state>@2x.png`** is preferred on a high-DPI screen.
* **Square artwork.** It is drawn into a square box — 40px in the agent panel
  (30px when the panel is narrow) and 64px on the new-tab page.

## Placeholders

`placeholder/` holds the stand-in Py that ships with the source, kept in a
separate folder so it can never be mistaken for the real artwork:
`has_final_artwork()` reports `False` while only placeholders exist. Delete the
folder or leave it — anything in this folder wins regardless.

## Motion

Still artwork gets a restrained animation layer for free: a slow breath, an
occasional randomised blink, a small lean while thinking, a soft pulse while
waiting for approval. All of it stops when `PYBROWSER_REDUCED_MOTION=1`,
`QT_REDUCED_MOTION=1` or `NO_ANIMATIONS=1` is set, and an animated asset
replaces it entirely.
