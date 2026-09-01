# Py — art direction brief

For commissioning the final Py artwork, from an illustrator or an image model.
Written from the integration side, so the technical constraints here are
measured rather than guessed: they are what the browser actually does with
these files.

Fourteen images: seven states × two crops.

---

## 1. The character bible

Repeat this block **verbatim** in every prompt, or hand it to the illustrator
once and hold every render against it. Consistency across fourteen images comes
from the description never drifting, not from describing it well once.

> Py, a semi-realistic anthropomorphic red fox character in the style of a
> modern animated feature film. Warm orange-red fur with visible individual
> hair strands and soft layered fur shading; cream-white muzzle, chest and tail
> tip; darker rust tones on the ear backs and along the spine. Realistic fox
> facial structure — a proper tapering muzzle, a small dark glossy nose, defined
> brow ridges and cheek fur. Large upright fluffy ears with visible pale inner
> fur. Amber-brown eyes with real corneal reflections, a wet highlight and
> depth; expressive but not human. A large, thick, fluffy tail with a cream tip.
> He wears a royal-blue (#3D5AFE) hoodie rendered as actual soft cotton fabric,
> with believable folds, seams, a drawstring, and gentle ambient occlusion where
> it meets the fur; a small white rounded-square badge with a blue "P." on the
> left chest. Dark charcoal joggers. Slightly stylised, friendly proportions —
> roughly five heads tall, an adolescent build, not chibi, not a giant head.
> Clever and curious rather than childish. Soft three-point studio lighting with
> a warm key from the upper left and a cool rim light separating him from the
> background. Subsurface scattering in the ears. Physically based materials.
> Transparent background.

**Locked, and identical in all fourteen:** fur colour and pattern, eye colour,
muzzle and nose shape, ear shape and inner-fur colour, hoodie colour and cut,
badge design and placement, joggers, body proportions, lighting direction and
temperature, render style.

**Free to change per state:** pose, facial expression, ear angle, tail position,
the prop, and the gesture.

---

## 2. Getting the same fox fourteen times

This is the hard part, and it is a workflow problem rather than a prompting
problem. Generating fourteen images from fourteen prompts gives fourteen
different foxes.

1. **Make one master first.** The `idle` full-body. Iterate on that alone until
   it is exactly right. Nothing else starts until it is approved.
2. **Derive the other thirteen from the master**, not from the prompt — via
   character reference (`--cref` in Midjourney, IP-Adapter / reference-only in
   Stable Diffusion, "use this character" in the newer conversational models),
   or by handing an illustrator the approved master as the model sheet.
3. **Panels are crops of their own full-body render where the resolution
   allows.** A separately generated bust is a different fox — subtly wrong ear
   set, slightly different muzzle length — and the difference is obvious once
   the panel and the new-tab page are on screen together. If the full-body
   render is 2048px tall, its head is roughly 400px and crops fine to 512×512.
   Generate a dedicated bust only when the head is too small to crop.
4. **Fix the seed / style reference** across the set where the tool allows it.
5. **Review all fourteen side by side**, never one at a time. Identity drift is
   invisible in isolation and glaring in a row.

---

## 3. The seven states

Each is: the character bible, then the delta below, then the framing from §4.
The companion line is what the UI says underneath — it is the emotional brief
for the expression, not text to draw into the image.

| # | State | Pose and expression | Prop | Companion line |
|---|---|---|---|---|
| 1 | `idle` | Relaxed standing, weight on one leg, a small friendly smile, ears up and neutral, tail resting naturally behind him | none | "Ready when you are." |
| 2 | `reading` | Holding an open book in both paws, head tilted down toward it, eyes lowered and focused, **ears tipped slightly forward** — the giveaway that he is paying attention | open hardback book | "I'm looking through the page…" |
| 3 | `thinking` | Looking up and to one side, one paw near his chin, brow slightly furrowed, one ear cocked | small soft-glowing thought motes drifting up | "Let me figure this out…" |
| 4 | `working` | Seated or leaning over a small open laptop, both paws on it, concentrated but confident, screen light warming his face from below | laptop with the "P." mark glowing on the lid | "On it." |
| 5 | `approval` | Alert and a little surprised, **not frightened** — eyes wide, ears up and forward, one paw raised holding up a marker | a warm-orange (#FFB347) rounded exclamation badge | "I need your okay for this." |
| 6 | `complete` | Genuinely delighted, eyes squeezed shut in a real smile, one paw punched up, tail swept high and mid-motion | a light scatter of confetti | "Done!" |
| 7 | `stuck` | Head tilted, one paw scratching the back of his head, ears slightly back and asymmetric, mouth a small uncertain line — confused but still endearing | a small "?" or a scribble mote above his head | "Looks like I got stuck." |

Two hard rules the UI depends on:

* **`complete` means the task genuinely succeeded.** It is never shown for an
  error, a cancellation, or a stop. It must not be usable as a generic "finished".
* **`stuck` must not read as success.** It is what the user sees when a task
  failed or was stopped, and it has to be unmistakable at a glance next to
  `complete`.

---

## 4. Framing and delivery

| | Full-body | Panel (bust) |
|---|---|---|
| Shows | whole character, ear tips to feet, tail included | head and shoulders |
| Aspect | portrait, about **1:2** | **square** |
| Deliver at | ≥ 1024 × 2048 | ≥ 512 × 512 |
| Used at | up to 210 px tall on the new-tab page | **44 px** in the agent panel (34 px when narrow) |

* **PNG with a real alpha channel.** No baked background of any colour — the
  same file is used on a near-white page and a near-black one.
* **Trim the canvas tight to the character.** The page sizes Py by the file's
  own height, so transparent padding is spent instead of character. The last
  set arrived 59% filled and rendered visibly small.
* **No pure black outlines or black rim light** — they vanish against the dark
  theme. Separate him from the background with a *cool* rim light instead.
* **No drop shadow onto a ground plane.** There is no ground; he sits on a page.
* Filenames, exactly: `idle-full.png`, `idle-panel.png`, `reading-full.png`,
  `reading-panel.png`, `thinking-full.png`, `thinking-panel.png`,
  `working-full.png`, `working-panel.png`, `approval-full.png`,
  `approval-panel.png`, `complete-full.png`, `complete-panel.png`,
  `stuck-full.png`, `stuck-panel.png`.

### Negative prompt

> flat vector art, thick black outlines, geometric shapes, sticker, logo, icon,
> emoji, corporate mascot, chibi, oversized head, simplistic cartoon anatomy,
> photorealistic wild animal, taxidermy, uncanny human face, text, watermark,
> signature, background, ground shadow, drop shadow, border, frame

---

## 5. The 44-pixel problem

Worth knowing before commissioning, because it is a genuine tension in this
brief rather than a detail.

The panel art is displayed at **44 px**. Individual fur strands, subsurface
scattering and fabric weave do not survive that — they resolve to grey mush, and
a heavily detailed bust can read *worse* at 44px than a simpler one. What still
reads at 44px is: the **silhouette** (ear shape above all), **two or three large
value blocks**, the **eyes**, and the **prop's colour**.

So the panel renders should be composed for it, not merely cropped smaller:

* **Frame tight.** The head should fill most of the square, with just enough
  shoulder and hoodie collar to place it. A bust with lots of chest is a smudge.
* **Keep the ear silhouette clean** and clear of props — the ears are what says
  "fox" when nothing else survives.
* **Push eye contrast** harder than looks right at full size. Dark iris, bright
  catchlight, clear separation from the surrounding fur.
* **Make props big, saturated and clear of the head.** The orange `!` badge, the
  book, the laptop and the confetti are the only things telling six of the seven
  states apart at 44px.
* **Check every candidate at 44px before accepting it.** On white *and* on
  near-black.

This does not mean simplifying the character. The full-body art should be as
detailed as the brief asks. It means the panel crop is a **different
composition** of the same fox, framed for the size it is actually shown at.

---

## 6. After the artwork exists

Nothing in the browser needs changing — the mascot system already resolves these
filenames, and the last set went in without a line of code moving.

```
python scripts/import_py_artwork.py ~/py-art/ --out app/ui/assets/mascot --force
```

That trims transparent margins, knocks out a flat background if one was baked
in, derives panel crops from full-body renders where they are missing, and
writes `@2x`. `--trim-only` just crops. `--dry-run` reports without writing.

Then, to see it at the sizes that matter:

```
python scripts/mascot_check.py                     # 40 checks against the real window
python scripts/ui_shots.py /tmp/shots              # new tab, panel, approval
python scripts/ui_shots.py /tmp/shots --dark       # the same, dark theme
```

If any state is missing, the resolver falls back
`<state>-<variant>` → `<state>` → `idle-<variant>` → `idle`, so a partial set
works: Py simply wears the same face for the states that are absent. One
excellent `idle` is a perfectly good first delivery.
