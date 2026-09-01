# Py artwork

Py is a boy in a royal-blue hoodie with a white "P." badge, dark jeans and blue
sneakers, in a semi-realistic animated-film style.

Drop the final Py files straight into **this folder**. That is the whole
integration — no code changes, no registration, no rebuild.

## Two crops

Py appears in two places that want different framings, so each state has two
files:

| Suffix | Where | Framing | Aspect |
|---|---|---|---|
| `<state>-full.*` | new-tab page | **whole character**, head to feet | taller than wide — around 1:2 |
| `<state>-panel.*` | AI sidebar header | **head and shoulders** | roughly square to slightly tall |

The new-tab slot sizes Py by height with `width: auto`, so the drawing's own
aspect decides how wide the character ends up: a full-body file may be as tall
and narrow as it likes. The panel slot is a square box about 44px on a side
(34px when the panel is narrow), which is why the panel crop has to be a bust —
a whole figure at 44px is a smudge.

## The seven states

| State | Shown when |
|---|---|
| `idle` | nothing is happening |
| `reading` | a page is being read |
| `thinking` | waiting on the model |
| `working` | a browser action is running |
| `approval` | waiting for the user to allow or deny |
| `complete` | a task finished with an answer (~2.6s, then idle) |
| `stuck` | a task was stopped or failed — **never** shown as success |

## Recommended delivery

Fourteen files, the full set:

```
idle-full.png       idle-panel.png
reading-full.png    reading-panel.png
thinking-full.png   thinking-panel.png
working-full.png    working-panel.png
approval-full.png   approval-panel.png
complete-full.png   complete-panel.png
stuck-full.png      stuck-panel.png
```

* **`.png`** is the safe default: it renders identically everywhere and is
  inlined into the new-tab page without a font or filter surprise. Export at
  **2× or 3×** the display size and drop it in as `<name>@2x.png` alongside the
  1× file — around 220×440 for a full body, 128×128 for a bust.
* **Transparent background, no baked-in light ground.** The new-tab page is
  near-white in one theme and near-black in the other, and the same file is
  used for both.
* **`.svg`** is better if the artwork is vector — it stays sharp at any size and
  keeps the file small, which matters because the new-tab drawing is inlined as
  a data URI on every new tab. Give the SVG an explicit `viewBox` **trimmed to
  what is actually drawn**: empty margin in the viewBox makes Py render small
  inside a large invisible box.
* **`.gif` / `.webp` / `.apng`** if a state is animated — see Motion below.
* No pure-black outlines — they disappear against the dark theme. The orange
  fur and the blue hoodie both carry enough contrast on either ground; the
  cream muzzle is what needs watching against a light page.

Extensions are tried in this order: `.gif`, `.webp`, `.apng`, `.png`, `.svg` —
so an animated file wins over a still one of the same name.

## Rules

* **Only `idle` is required.** Any missing state falls back to `idle`, and a
  single final `idle.png` outranks the entire placeholder set — so one file is
  enough to make Py the new character everywhere.
* **Backwards compatible.** A bare `<state>.*` with no suffix still works and is
  used for both crops: `<state>-<variant>` is tried first, then `<state>`, then
  `idle-<variant>`, then `idle`. Shipping only `idle.png` is valid; so is
  shipping all fourteen; so is anything in between.
* **`<state>@2x.png`** is preferred on a high-DPI screen.

## What is installed

The twenty-eight files at the top of this folder are Py, derived from the seven
supplied full-body renders. `as-supplied/` holds those seven exactly as they
arrived, byte for byte.

Everything in between is cropping and scaling — no pixel of the character was
redrawn or recoloured:

* the **cream backdrop** each render arrived on is flooded out to transparency
  from the border, so an enclosed highlight inside the figure survives
* the **contact shadow** painted onto that backdrop is cleared as well; it is
  not the backdrop colour, so the flat knock-out leaves it as an opaque grey
  smudge — barely visible on a light page, a bright blob on the dark theme
* the canvas is **trimmed tight** to the character, because the new-tab page
  sizes Py by the file's own height and padding is height spent on nothing
* the **panel bust** is cut from the same render rather than being a separate
  image, so the face in the sidebar is literally the face on the new-tab page

To re-derive the whole set:

```
python scripts/import_py_artwork.py as-supplied --out . --force \
    --panel-spread 2.0 --panel-min-height 0.38
```

`--panel-spread` is the one number worth knowing. It sets how much wider than
the head the bust is, and it is a real trade-off, decided by looking at 44px
rather than by taste: at 1.45 the face is large and clear but every prop is
cropped away, so `approval`, `reading` and `working` are indistinguishable from
`idle` in the sidebar. At 2.6 every prop is present and the face is mush. 2.0
keeps enough of the raised hand, the book and the laptop to tell the states
apart while the face still reads.

## Commissioning new artwork

`ART-DIRECTION.md`, next to this file, is the brief: the character bible to
repeat verbatim in every prompt, how to keep the same fox across fourteen
images, the seven states with their poses and props, the delivery spec, and why
the panel crop has to be composed for 44px rather than merely scaled to it.

## Importing supplied artwork

If the artwork arrives as full-body drawings - or as one sheet with several
figures on it - `scripts/import_py_artwork.py` produces the fourteen files by
**cropping it**. It draws nothing: every pixel it writes comes out of the file
it was given.

```
python scripts/import_py_artwork.py ~/py-art/            # a file per state
python scripts/import_py_artwork.py sheet.png            # a sheet, split left to right
python scripts/import_py_artwork.py py.png --all-states  # one drawing, every state
```

It knocks a flat background out to transparency (flood-filled from the border,
so an enclosed white highlight in an eye survives), clears a ground shadow
painted onto that background (a shadow is not the backdrop colour, so the flat
knock-out leaves it as an opaque grey smudge - subtle on a light page, a bright
blob on the dark theme), trims the margin tight to
the character, finds the neck by looking for where the drawing suddenly gets
wider and cuts a square head-and-shoulders bust above it, and writes `@2x`
alongside each file. `--dry-run` reports without writing; the crop boxes it
chooses are printed, and `--panel-spread`, `--panel-headroom` and
`--panel-shoulder` adjust them if a bust comes out framed wrong.

It never scales artwork up. A file smaller than the target is left at its own
size and the UI scales it down instead.

## Placeholders

`placeholder/` holds the stand-in Py that ships with the source, kept in a
separate folder so it can never be mistaken for the real artwork:
`has_final_artwork()` reports `False` while only placeholders exist. It is
generated by `scripts/make_placeholder_py.py`. Delete the folder or leave it —
anything in this folder wins regardless.

## Motion

Still artwork gets a restrained animation layer for free, split by what the
transform does to the drawing rather than by what it looks like:

* **Rigid** — a slow vertical breath, a fraction of a degree of sway, and a
  uniform scale of well under a percent. None of these can change Py's
  proportions or distort his face, so they run on any still artwork. Each state
  has its own cadence: `working` breathes faster than `idle` and carries a small
  shift of weight, so a glance says "he is doing something"; `stuck` barely
  moves at all.
* **Squash** — the blink, a non-uniform scale. It reads as expression on a flat
  placeholder and as a rendering fault on an illustration, so it stops the
  moment real artwork is installed.

`complete` also gets a one-shot on arrival: a small rise and swell that crests
in about 200ms and is spent inside a second, then settles into an ordinary happy
breath. A celebration that loops forever stops being a celebration.

**What the transform layer cannot do**: blinking, a head turn, or fingers moving
on a laptop. Those need pixels that do not exist in a single still, and faking
them by warping the drawing is exactly what makes finished artwork look broken.
They are an **animated asset** — drop a `.gif`, `.webp` or `.apng` in beside the
stills and it wins over the still of the same name automatically and plays
through `QMovie` instead of this layer. No code change.

Cost, measured: 0.16 ms per frame at the panel's 44px and 0.56 ms at the new
tab's 210px, at 20fps — around 0.3% and 1.1% of one core.

All motion stops when `PYBROWSER_REDUCED_MOTION=1`, `QT_REDUCED_MOTION=1` or
`NO_ANIMATIONS=1` is set — including animated assets.

## Palette

From the character sheet, and shared with the browser chrome
(`app/ui/theme.py`, `app/browser/newtab.py`):

| Colour | Role |
|---|---|
| `#3D5AFE` | hoodie, and the browser's light accent |
| `#556BFF` | hood lining, highlights |
| `#B8C6FF` | sneaker trim |
| `#1E2430` | joggers, linework |
| `#2D3342` | jogger highlight |
| `#FFB347` | the approval sign |
| `#FF6B6B` | deny |
| `#28C76F` | done |

The dark theme uses `#8C9CFF` rather than `#556BFF`: the sheet's lighter blue
is only about 4:1 against a near-black window, which is thin for body text.
