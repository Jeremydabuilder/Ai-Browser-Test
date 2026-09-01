"""Generate the stand-in Py artwork - the fox.

    python scripts/make_placeholder_py.py            # write the asset files
    python scripts/make_placeholder_py.py --sheet X  # also render a reference sheet

Writes ``app/ui/assets/mascot/placeholder/<state>-{full,panel}.svg`` - seven
states, two crops - built from the Py character sheet: an anthropomorphic fox
in a royal-blue hoodie with a white "P." badge, dark joggers, blue sneakers,
warm orange fur, cream muzzle, chest and tail tip, large ears, fluffy tail.

This is a placeholder. The character sheet is a polished semi-realistic render
with fur and lighting, and this is flat vector, so it cannot be that; what it
can be is the same *character* - same species, silhouette, outfit, palette, the
same seven poses and the same seven lines - so the browser looks like itself,
and so the final artwork is a drop-in rather than a redesign.

Generated from one shared figure rather than drawn separately, because the
whole point is that every state is recognisably the same fox: same head shape,
ears, fur colours, hoodie, badge, tail and proportions. Only the pose, the
props and the face change.

Delete the output folder and the browser falls back to a plain drawn mark; drop
real artwork into ``assets/mascot/`` and it wins over all of this.
"""

from __future__ import annotations

import argparse
import os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "app", "ui", "assets", "mascot", "placeholder")

# Py's palette, taken from the character sheet's own swatches.
BLUE = "#3D5AFE"          # hoodie
BLUE_LIGHT = "#556BFF"    # hood lining, highlights
BLUE_PALE = "#B8C6FF"     # sneaker trim, paper
BLUE_DEEP = "#2A41D6"     # sleeves and hoodie shadow; derived, not on the sheet
INK = "#1E2430"           # joggers, linework
INK_SOFT = "#2D3342"      # jogger highlight
AMBER = "#FFB347"         # the approval sign
RED = "#FF6B6B"           # deny
GREEN = "#28C76F"         # done

# Fur is not on the swatch row; these read as the sheet's warm orange fox with
# a cream muzzle, chest and tail tip.
FUR = "#E8722C"
FUR_DARK = "#C4551B"
FUR_LIGHT = "#F79A4E"
CREAM = "#FBEEDC"
CREAM_SH = "#E4D2B8"
NOSE = "#2E2018"
EYE = "#8B4A1E"
WHITE = "#FFFFFF"

#: Shared gradients. Flat fills are exactly the "flat clip-art fox" the sheet
#: rules out; a little depth on the fur, the hoodie and the joggers is what
#: separates a drawn character from a sticker.
DEFS = f"""<defs>
    <linearGradient id="hoodie" x1="0" y1="0" x2=".35" y2="1">
      <stop offset="0" stop-color="{BLUE_LIGHT}"/>
      <stop offset=".55" stop-color="{BLUE}"/>
      <stop offset="1" stop-color="{BLUE_DEEP}"/>
    </linearGradient>
    <linearGradient id="fur" x1=".2" y1="0" x2=".85" y2="1">
      <stop offset="0" stop-color="{FUR_LIGHT}"/>
      <stop offset=".5" stop-color="{FUR}"/>
      <stop offset="1" stop-color="{FUR_DARK}"/>
    </linearGradient>
    <linearGradient id="cream" x1=".3" y1="0" x2=".8" y2="1">
      <stop offset="0" stop-color="{WHITE}"/>
      <stop offset="1" stop-color="{CREAM_SH}"/>
    </linearGradient>
    <linearGradient id="legs" x1="0" y1="0" x2="1" y2=".2">
      <stop offset="0" stop-color="{INK_SOFT}"/>
      <stop offset="1" stop-color="{INK}"/>
    </linearGradient>
  </defs>"""


# -- the head ----------------------------------------------------------------
def ears() -> str:
    """The large ears. Half of what makes a fox read as a fox at 34 pixels, so
    they are big, pointed and set wide - and they are drawn behind the skull so
    the join never shows."""
    return (
        f'<path d="M38.5 26C34.8 18.6 29.6 10.2 25.8 6c-2-2.2-3.8-1.2-3.7 1.6.3 7.2 1.7 16.4 3.9 23.6Z" fill="url(#fur)"/>'
        f'<path d="M37 24.6c-3-6-7.2-12.8-10.2-16.2-1.2-1.4-2.2-.8-2.2 1 .2 5.6 1.2 12.6 2.9 18.4Z" fill="{CREAM}" opacity=".9"/>'
        f'<path d="M27.4 8.4c-.8-1-1.6-.6-1.6.9.1 2.6.4 5.6.9 8.6 1-3.6 1.4-6.8.7-9.5Z" fill="{FUR_DARK}"/>'
        f'<path d="M61.5 26C65.2 18.6 70.4 10.2 74.2 6c2-2.2 3.8-1.2 3.7 1.6-.3 7.2-1.7 16.4-3.9 23.6Z" fill="url(#fur)"/>'
        f'<path d="M63 24.6c3-6 7.2-12.8 10.2-16.2 1.2-1.4 2.2-.8 2.2 1-.2 5.6-1.2 12.6-2.9 18.4Z" fill="{CREAM}" opacity=".9"/>'
        f'<path d="M72.6 8.4c.8-1 1.6-.6 1.6.9-.1 2.6-.4 5.6-.9 8.6-1-3.6-1.4-6.8-.7-9.5Z" fill="{FUR_DARK}"/>')


def head(eyes: str, mouth: str, brow: str = "", tilt: float = 0.0) -> str:
    """Py's head. Identical everywhere; only the face inside it changes.

    A fox skull rather than a round mascot ball: wide at the brow, cheek ruffs
    flaring at the sides, tapering to a cream muzzle with a dark nose. The
    silhouette has to survive at 34 pixels, which is why the ruffs are cut as
    hard points and not as soft curves.
    """
    rot = f' transform="rotate({tilt} 50 36)"' if tilt else ""
    return f"""<g id="head"{rot}>
    {ears()}
    <path d="M31.6 33.4c-3.8 1.2-7 3.4-9.6 6.6 3.4 1 6.6 1 9.6 0Z" fill="url(#fur)"/>
    <path d="M33 42.4c-3.2 1.6-5.8 4-7.6 7.2 3.4.4 6.4-.2 8.8-2Z" fill="url(#fur)"/>
    <path d="M68.4 33.4c3.8 1.2 7 3.4 9.6 6.6-3.4 1-6.6 1-9.6 0Z" fill="url(#fur)"/>
    <path d="M67 42.4c3.2 1.6 5.8 4 7.6 7.2-3.4.4-6.4-.2-8.8-2Z" fill="url(#fur)"/>
    <path d="M31 33.6c0-10.6 8.5-19.2 19-19.2s19 8.6 19 19.2c0 4.6-.8 8.8-2.4 12.2-1.7 3.8-4.2 6.7-7.2 8.6-2.9 1.9-6.2 2.9-9.4 2.9s-6.5-1-9.4-2.9c-3-1.9-5.5-4.8-7.2-8.6C31.8 42.4 31 38.2 31 33.6Z" fill="url(#fur)"/>
    <path d="M50 24.6c5.8 0 10.6 2.6 13.4 6.8-2.4-8-7.4-12.6-13.4-12.6s-11 4.6-13.4 12.6c2.8-4.2 7.6-6.8 13.4-6.8Z" fill="{FUR_LIGHT}" opacity=".55"/>
    <path d="M50 60.2c-4.6 0-8.8-1.8-11.6-4.8-1.8-2-2.4-4.6-1.4-6.8 1.8-4 6.8-6.4 13-6.4s11.2 2.4 13 6.4c1 2.2.4 4.8-1.4 6.8-2.8 3-7 4.8-11.6 4.8Z" fill="url(#cream)"/>
    <path d="M50 44.8c1.4 3.2 1.4 6.2 0 9-1.5-2.8-1.5-5.8 0-9Z" fill="{CREAM_SH}" opacity=".5"/>
    <path d="M50 46.4c2.8 0 4.6 1.2 4.6 2.9 0 2.1-2.3 3.9-4.6 3.9s-4.6-1.8-4.6-3.9c0-1.7 1.8-2.9 4.6-2.9Z" fill="{NOSE}"/>
    <path d="M47.6 47.4c1-.5 2.1-.6 3-.3-1 .5-2 .6-3 .3Z" fill="{WHITE}" opacity=".45"/>
    {brow}
    {eyes}
    {mouth}
  </g>"""


# -- faces -------------------------------------------------------------------
def _eye(x: float, y: float, look: tuple = (0.0, 0.0)) -> str:
    """A big, warm, expressive eye - the sheet's most recognisable feature."""
    dx, dy = look
    return (f'<ellipse cx="{x}" cy="{y}" rx="4.6" ry="5.2" fill="{WHITE}"/>'
            f'<ellipse cx="{x + dx}" cy="{y + dy}" rx="3.5" ry="4.1" fill="{EYE}"/>'
            f'<ellipse cx="{x + dx}" cy="{y + dy + .5}" rx="2" ry="2.4" fill="{NOSE}"/>'
            f'<circle cx="{x + dx - 1.3}" cy="{y + dy - 1.7}" r="1.5" fill="{WHITE}"/>'
            f'<circle cx="{x + dx + 1.5}" cy="{y + dy + 1.6}" r=".85" fill="{WHITE}" opacity=".65"/>')


def eyes(look=(0.0, 0.0)) -> str:
    return _eye(41.4, 34.6, look) + _eye(58.6, 34.6, look)


def eyes_happy() -> str:
    return (f'<path d="M37 36q4.4-5.6 8.8 0M54.2 36q4.4-5.6 8.8 0"'
            f' stroke="{NOSE}" stroke-width="3" fill="none" stroke-linecap="round"/>')


#: The muzzle is cream, so the mouth is drawn on it rather than in it: a line
#: down from the nose, then the smile.
def _mouth(curve: str) -> str:
    return (f'<path d="M50 52.8v2.2" stroke="{NOSE}" stroke-width="1.7" stroke-linecap="round"/>'
            f'<path d="{curve}" stroke="{NOSE}" stroke-width="1.9" fill="none" stroke-linecap="round"/>')


SMILE = _mouth("M44.6 54.4q5.4 4.4 10.8 0")
SMILE_SOFT = _mouth("M46 55q4 2.8 8 0")
GRIN = (f'<path d="M50 52.8v1.6" stroke="{NOSE}" stroke-width="1.7" stroke-linecap="round"/>'
        f'<path d="M43.6 54q6.4 6.6 12.8 0Z" fill="{NOSE}"/>'
        f'<path d="M46.6 57.8q3.4 2.6 6.8 0Z" fill="{RED}" opacity=".85"/>')
FLAT = _mouth("M46.4 55.4h7.2")
WAVY = _mouth("M45.8 55.4q1.8-1.8 3.6 0t3.6 0")
OH = (f'<path d="M50 52.8v1.4" stroke="{NOSE}" stroke-width="1.7" stroke-linecap="round"/>'
      f'<ellipse cx="50" cy="56.4" rx="2.6" ry="3.1" fill="{NOSE}"/>')

BROW_UP = (f'<path d="M36.8 27.6q4.2-2.4 8.4-.6M54.8 27q4.2-1.8 8.4.8" stroke="{FUR_DARK}"'
           ' stroke-width="2.2" fill="none" stroke-linecap="round"/>')
BROW_THINK = (f'<path d="M36.8 28.4q4.2-3 8.4 0M55.2 26.6q4-.8 8 1.5" stroke="{FUR_DARK}"'
              ' stroke-width="2.2" fill="none" stroke-linecap="round"/>')
BROW_SOFT = (f'<path d="M37 27.8q4.2-1.7 8.4 0M54.6 27.8q4.2-1.7 8.4 0" stroke="{FUR_DARK}"'
             ' stroke-width="2.1" fill="none" stroke-linecap="round"/>')

FACES = {
    "idle": (eyes(), SMILE, ""),
    "reading": (eyes(look=(0, 1.8)), SMILE_SOFT, BROW_SOFT),
    "thinking": (eyes(look=(2, -1.4)), FLAT, BROW_THINK),
    "working": (eyes(look=(0, 1.6)), SMILE_SOFT, BROW_SOFT),
    "approval": (eyes(), OH, BROW_UP),
    "complete": (eyes_happy(), GRIN, ""),
    "stuck": (eyes(look=(-1.5, .9)), WAVY, BROW_THINK),
}


# -- the body ----------------------------------------------------------------
def badge(cx: float, cy: float, size: float) -> str:
    """The chest badge: a white rounded square with a blue "P.", as drawn on
    the sheet and in the PyBrowser logo. It is the one mark that has to survive
    all the way down to 34 pixels, so it is a solid shape and not an outline.
    """
    half = size / 2
    return (f'<rect x="{cx - half}" y="{cy - half}" width="{size}" height="{size}"'
            f' rx="{round(size * 0.28, 2)}" fill="{WHITE}"/>'
            f'<text x="{cx}" y="{cy + size * 0.30}" text-anchor="middle"'
            f' font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif"'
            f' font-size="{round(size * 0.62, 2)}" font-weight="700" fill="{BLUE}">P.</text>')


#: The tail. Drawn first, so it sits behind everything, and swept out to one
#: side rather than hanging straight down - a tail behind the legs is invisible
#: at panel size and does half the work of saying "fox" at full size.
TAIL = (f'<path d="M46 92c-7 1.8-14.4 6.6-20.6 13.8C18.8 113.6 14.6 122.6 13.4 131'
        f' c-.5 3.6 1.6 5.9 4.8 5.6 3.8-.4 7.6-3.8 10.4-9 3.7-6.7 5.6-14.6 6-21.8'
        f' 3.5-5.8 7.5-10.3 11.4-13.8Z" fill="url(#fur)"/>'
        f'<path d="M42.6 96.4c-5 2.2-10.2 6.2-14.8 11.6 1.4-3.6 3.4-6.8 5.8-9.4'
        f' 2.8-1 5.8-1.7 9-2.2Z" fill="{FUR_LIGHT}" opacity=".5"/>'
        f'<path d="M13.4 131c-.5 3.6 1.6 5.9 4.8 5.6 3.8-.4 7.6-3.8 10.4-9'
        f' -4.8 2-9.9 3.1-15.2 3.4Z" fill="url(#cream)"/>')


def torso(left_arm: str, right_arm: str) -> str:
    """Hoodie, joggers and sneakers: the same in every pose.

    The legs run most of the lower half on purpose. The sheet asks for a
    standing anthropomorphic fox, not a chibi - so the head sits on a body with
    real length to it rather than on a pair of stubs.
    """
    return f"""
    {TAIL}
    <path d="M39.4 96h10.1v42.4h-9.7Z" fill="url(#legs)"/>
    <path d="M50.5 96h10.1l-.4 42.4h-9.7Z" fill="url(#legs)"/>
    <path d="M39.4 96h4.6v42.4h-4.2Z" fill="{INK_SOFT}" opacity=".6"/>
    <path d="M49.5 96h1v42.4h-1Z" fill="{INK}" opacity=".55"/>
    <path d="M38.9 136h10.6v7.4c0 1.7-1 2.6-2.8 2.6H33.6c-1.4 0-2.1-.7-2.1-2 0-3.2 2.4-5.9 7.4-8Z" fill="{BLUE}"/>
    <path d="M50.5 136h10.6c5 2.1 7.4 4.8 7.4 8 0 1.3-.7 2-2.1 2H53.3c-1.8 0-2.8-.9-2.8-2.6Z" fill="{BLUE}"/>
    <path d="M38.9 136h10.6v3.6H36c.5-1.3 1.8-2.5 4-3.6Zm11.6 0h10.6c2.2 1.1 3.5 2.3 4 3.6H50.5Z" fill="{BLUE_LIGHT}" opacity=".6"/>
    <path d="M31.5 142.6h18v3.4H33.6c-1.4 0-2.1-.7-2.1-2Zm19 0h18c0 2.7-.7 3.4-2.1 3.4H53.3c-1.8 0-2.8-.9-2.8-2.6Z" fill="{BLUE_PALE}"/>
    <path d="M38 78c-3.4-2.6-4.6-6.4-3.6-11.4 3.4 2 6.6 3.4 9.6 4.2Zm24 0c3.4-2.6 4.6-6.4 3.6-11.4-3.4 2-6.6 3.4-9.6 4.2Z" fill="{BLUE_DEEP}"/>
    <path d="M44.6 62.4c1.6 3.4 3.4 6 5.4 7.8 2-1.8 3.8-4.4 5.4-7.8 2.6 1.2 4.6 2.8 5.9 4.9-2.1 6.6-5.9 10.2-11.3 10.2s-9.2-3.6-11.3-10.2c1.3-2.1 3.3-3.7 5.9-4.9Z" fill="url(#cream)"/>
    <path d="M34 100V78c0-8.8 7.2-16 16-16s16 7.2 16 16v22c0 1.9-1.1 2.9-3.1 2.9-8.6 1.2-17.2 1.2-25.8 0-2 0-3.1-1-3.1-2.9Z" fill="url(#hoodie)"/>
    <path d="M37.1 102.9c-2 0-3.1-1-3.1-2.9V78c0-6.8 4.3-12.6 10.3-15l-2.6 39.9Z" fill="{BLUE_LIGHT}" opacity=".4"/>
    <path d="M56.4 63.2L59 102.9h3.9c2 0 3.1-1 3.1-2.9V78c0-7-4.4-12.8-9.6-14.8Z" fill="{BLUE_DEEP}" opacity=".35"/>
    <path d="M41.6 64.6c2.4 4.4 5.2 6.6 8.4 6.6s6-2.2 8.4-6.6c2.4 1 4.4 2.4 5.9 4.2-3.4 4.4-8.4 7-14.3 7s-10.9-2.6-14.3-7c1.5-1.8 3.5-3.2 5.9-4.2Z" fill="{BLUE_LIGHT}"/>
    {badge(50, 88, 13)}
    {left_arm}
    {right_arm}
"""


#: The full body's crop, trimmed to what is actually drawn - the tail on the
#: left, a raised paw on the right, ears at the top, sneakers at the bottom.
#: Left at the full 100-wide drawing space the new-tab page sized Py by the
#: empty margin and the character came out a thin strip in a wide box.
FULL_VIEW = "8 0 78 152"

#: The bust's crop. Found by looking at it at 44 pixels rather than by
#: reasoning about it: too wide and Py is a smudge in the corner of the panel,
#: too tight and the ears - which are most of what says "fox" at that size -
#: are cropped off the top.
BUST_VIEW = "15 0 70 84"


def svg(body: str, view: str = FULL_VIEW) -> str:
    """Wrap a body in an SVG whose intrinsic size matches its viewBox.

    The intrinsic size matters: the new-tab page sizes Py with `height` and
    `width: auto`, so the drawing's own aspect is what decides how wide the
    character ends up.
    """
    _, _, width, height = view.split()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" '
            f'width="{width}" height="{height}">\n  {DEFS}\n{body}\n</svg>\n')


def arm(path: str, paw: tuple[float, float] | None = None) -> str:
    """A sleeve and the paw on the end of it.

    Drawn a shade darker than the hoodie body on purpose: at the hoodie's own
    mid blue the arms disappeared into the torso and Py looked like a hoodie
    with paws floating beside it. The silhouette has to survive at 34 pixels.
    """
    out = (f'<path d="{path}" stroke="{BLUE_DEEP}" stroke-width="8.6" fill="none"'
           ' stroke-linecap="round"/>')
    if paw:
        x, y = paw
        out += (f'<circle cx="{x}" cy="{y}" r="5.2" fill="url(#fur)"/>'
                f'<path d="M{x - 2.6} {y + 1.4}q2.6 2.4 5.2 0" stroke="{FUR_DARK}"'
                ' stroke-width="1.1" fill="none" stroke-linecap="round" opacity=".7"/>')
    return out


ARM_REST_L = arm("M35.6 80c-3.8 4-5.2 8.4-4.4 13", (31.2, 93))
ARM_REST_R = arm("M64.4 80c3.8 4 5.2 8.4 4.4 13", (68.8, 93))

POSES = {
    # Standing, one paw raised in a small wave. The "meet Py" pose.
    "idle": (ARM_REST_L, arm("M64.4 80c5-1.8 8.2-6.2 8.8-11.4", (73.2, 68.6))),
    # Both paws holding an open book, eyes down on it.
    "reading": (arm("M35.6 80c-1.4 4.8-.6 9 2.6 12.2", (38.2, 92.2)),
                arm("M64.4 80c1.4 4.8.6 9-2.6 12.2", (61.8, 92.2))),
    # One paw to the chin.
    "thinking": (ARM_REST_L, arm("M64.4 80c3.8 2.2 4 7.6 1 11.2M65.4 91.2c-3.2-3.6-4.2-10.4-4-15.6",
                                 (61.4, 75.6))),
    # Both paws forward on a laptop.
    "working": (arm("M35.6 80c-.4 5.4 1.4 9.4 5 11.8", (40.6, 91.8)),
                arm("M64.4 80c.4 5.4-1.4 9.4-5 11.8", (59.4, 91.8))),
    # One paw up, holding a small sign.
    "approval": (ARM_REST_L, arm("M64.4 80c5.8-2.8 8.4-8.2 8.4-14", (72.8, 66)),),
    # One paw punched up, the other relaxed - the sheet's celebration.
    "complete": (ARM_REST_L, arm("M64.4 80c6-3 8.8-9.2 9-16", (73.4, 64))),
    # Paw to the back of the head - the universal "I have no idea".
    "stuck": (ARM_REST_L,
              arm("M64.4 80c4.8-1.2 7-5.2 7-10.6M71.4 69.4c-3.8-.6-6.6-2.9-8.4-6.4",
                  (63, 63))),
}

PROPS_FULL = {
    "reading": (f'<g><path d="M36 93h28v13H36Z" fill="{BLUE_PALE}" stroke="{BLUE_LIGHT}" stroke-width="1.6"/>'
                f'<path d="M50 93v13" stroke="{BLUE_LIGHT}" stroke-width="1.4"/>'
                f'<path d="M39 97h8M39 100.4h8M53 97h8M53 100.4h8" stroke="{BLUE}"'
                ' stroke-width="1.2" stroke-linecap="round" opacity=".7"/></g>'),
    "thinking": (f'<g fill="{BLUE_LIGHT}"><circle cx="84" cy="14" r="6.4" opacity=".9"/>'
                 f'<circle cx="76" cy="24" r="3.2" opacity=".72"/>'
                 f'<circle cx="71" cy="31" r="1.9" opacity=".55"/></g>'),
    "working": (f'<g><path d="M34 112h32l3 5H31Z" fill="{BLUE_DEEP}"/>'
                f'<rect x="36" y="96" width="28" height="16" rx="1.8" fill="{INK}"/>'
                f'<rect x="37.6" y="97.6" width="24.8" height="12.8" rx="1" fill="{BLUE_LIGHT}" opacity=".55"/>'
                f'{badge(50, 104, 7.4)}</g>'),
    "approval": (f'<g><rect x="63" y="45" width="20" height="19" rx="4" fill="{AMBER}"/>'
                 f'<path d="M73 50v7.2" stroke="{INK}" stroke-width="2.8" stroke-linecap="round"/>'
                 f'<circle cx="73" cy="60.6" r="1.6" fill="{INK}"/>'
                 f'<path d="M70.6 64h4.8L73 68Z" fill="{AMBER}"/></g>'),
    # Confetti in the sheet's accent colours, scattered but not symmetrical - a
    # mirrored burst reads as a pattern rather than as a moment.
    "complete": (f'<g>'
                 f'<rect x="24" y="20" width="4.4" height="4.4" rx="1.1" fill="{AMBER}" transform="rotate(20 26 22)"/>'
                 f'<rect x="62" y="7" width="3.8" height="3.8" rx="1" fill="{GREEN}" transform="rotate(35 64 9)"/>'
                 f'<rect x="33" y="6" width="3.6" height="3.6" rx="1" fill="{RED}" transform="rotate(-18 35 8)"/>'
                 f'<rect x="80" y="28" width="4" height="4" rx="1" fill="{BLUE}" transform="rotate(-28 82 30)"/>'
                 f'<circle cx="19" cy="40" r="2.3" fill="{BLUE_LIGHT}"/>'
                 f'<circle cx="83" cy="15" r="2.1" fill="{AMBER}"/>'
                 f'<circle cx="27" cy="33" r="1.7" fill="{GREEN}"/>'
                 f'<path d="M21 12l-2.6-3.6M46 3.6V0M72 21l3-3.4" stroke="{AMBER}"'
                 ' stroke-width="2.4" stroke-linecap="round"/></g>'),
    # A scribble over the head. It is the one state that must never be mistaken
    # for a good outcome, so it gets the most obviously unhappy prop.
    "stuck": (f'<path d="M83 12c-4.6-3.4-9-2.4-9.8 1.6-.7 3.4 3.4 5 5.8 3.2'
              f' 2.6-2 1.4-6.2-2.4-6.6-4-.4-6.6 3.2-5.4 6.8" stroke="{BLUE_LIGHT}"'
              ' stroke-width="2.4" fill="none" stroke-linecap="round"/>'),
}

# Props for the bust, kept inside the tighter crop below - anything drawn
# outside it is simply not on screen.
BUST_PROPS = {
    # Held up into the crop, so reading and working are still tellable apart at
    # 44 pixels - below the crop they were invisible and every state looked
    # like idle with different eyes.
    "reading": (f'<path d="M28 62h44v12H28Z" fill="{BLUE_PALE}" stroke="{BLUE_LIGHT}" stroke-width="2"/>'
                f'<path d="M50 62v12" stroke="{BLUE_LIGHT}" stroke-width="1.8"/>'
                f'<path d="M32 66h13M55 66h13" stroke="{BLUE}" stroke-width="1.6"'
                ' stroke-linecap="round" opacity=".7"/>'),
    "thinking": (f'<g fill="{BLUE_LIGHT}"><circle cx="80" cy="9" r="5.4" opacity=".9"/>'
                 f'<circle cx="73.5" cy="16.5" r="2.8" opacity=".72"/></g>'),
    "working": (f'<rect x="30" y="62" width="40" height="12" rx="2" fill="{INK}"/>'
                f'<rect x="32" y="64" width="36" height="8" rx="1.2" fill="{BLUE_LIGHT}" opacity=".55"/>'
                f'{badge(50, 68, 6.2)}'),
    "approval": (f'<g><rect x="66" y="6" width="17" height="16" rx="3.6" fill="{AMBER}"/>'
                 f'<path d="M74.5 10.2v5.8" stroke="{INK}" stroke-width="2.5" stroke-linecap="round"/>'
                 f'<circle cx="74.5" cy="18.8" r="1.5" fill="{INK}"/></g>'),
    "complete": (f'<g>'
                 f'<rect x="20" y="14" width="4" height="4" rx="1" fill="{AMBER}" transform="rotate(20 22 16)"/>'
                 f'<rect x="76" y="12" width="3.6" height="3.6" rx="1" fill="{GREEN}" transform="rotate(-25 78 14)"/>'
                 f'<rect x="28" y="4" width="3.4" height="3.4" rx="1" fill="{RED}" transform="rotate(-15 30 6)"/>'
                 f'<circle cx="19" cy="28" r="2.2" fill="{BLUE_LIGHT}"/>'
                 f'<circle cx="81" cy="26" r="2" fill="{AMBER}"/>'
                 f'<path d="M22 8l-2.6-3.4M79 5l2.6-3.4" stroke="{AMBER}"'
                 ' stroke-width="2.4" stroke-linecap="round"/></g>'),
    "stuck": (f'<path d="M82 9c-4.4-3.2-8.6-2.3-9.4 1.5-.7 3.3 3.3 4.8 5.6 3.1'
              f' 2.5-1.9 1.3-6-2.3-6.3-3.8-.4-6.3 3-5.2 6.5" stroke="{BLUE_LIGHT}"'
              ' stroke-width="2.3" fill="none" stroke-linecap="round"/>'),
}

#: How much smaller the head is on the full body than in the bust. The sheet
#: asks for a standing fox rather than a chibi; drawn at bust scale the head
#: and ears were nearly half the standing figure. Scaled about the neck, so the
#: collar still meets it.
HEAD_SCALE = 0.84
HEAD_PIVOT = (50, 68)

#: Props that belong to the head rather than to a paw, and so have to be scaled
#: along with it or they drift off the ears.
HEAD_PROPS = ("thinking", "complete", "stuck")


def _scaled(markup: str) -> str:
    px, py = HEAD_PIVOT
    return (f'<g transform="translate({px} {py}) scale({HEAD_SCALE}) '
            f'translate({-px} {-py})">{markup}</g>')


def build_full(state: str) -> str:
    eye, mouth, brow = FACES[state]
    left, right = POSES[state]
    prop = PROPS_FULL.get(state, "")
    tilt = {"thinking": -4, "stuck": -5}.get(state, 0)
    drawn_head = head(eye, mouth, brow, tilt=tilt)
    if state in HEAD_PROPS:
        upper = _scaled(prop + drawn_head)
    else:
        upper = _scaled(drawn_head) + prop
    return svg(f'  <g>{torso(left, right)}</g>\n  {upper}')


def build_panel(state: str) -> str:
    """Head and shoulders, framed tightly. The same head as the full body."""
    eye, mouth, brow = FACES[state]
    prop = BUST_PROPS.get(state, "")
    tilt = {"thinking": -4, "stuck": -5}.get(state, 0)
    # Shoulders begin just under the muzzle, so the crop is face-first, and
    # stay narrower than the frame - filling it edge to edge read as a blue
    # band rather than as a character.
    shoulders = (
        f'<path d="M44.6 61.8c1.6 3.2 3.4 5.6 5.4 7.2 2-1.6 3.8-4 5.4-7.2 2.5 1.1 4.4 2.7 5.7 4.7-2.1 6.2-5.8 9.6-11.1 9.6s-9-3.4-11.1-9.6c1.3-2 3.2-3.6 5.7-4.7Z" fill="url(#cream)"/>'
        f'<path d="M28 80v-4c0-8.4 6-15.2 14-16.8h16c8 1.6 14 8.4 14 16.8v4Z" fill="url(#hoodie)"/>'
        f'<path d="M33 80v-4c0-5 2.2-9.6 5.8-12.6L40.6 80Z" fill="{BLUE_LIGHT}" opacity=".42"/>'
        f'<path d="M41 61.4c2.6 3.4 5.6 5.1 9 5.1s6.4-1.7 9-5.1c2.2.8 4 1.9 5.4 3.3-3.1 3.6-8 5.8-14.4 5.8s-11.3-2.2-14.4-5.8c1.4-1.4 3.2-2.5 5.4-3.3Z" fill="{BLUE_LIGHT}"/>'
        f'{badge(50, 74, 9.5)}')
    # Order matters: the thought bubble and the confetti sit behind Py, the book
    # and the laptop are held in front.
    behind = prop if state in ("thinking", "approval", "complete", "stuck") else ""
    front = prop if state in ("reading", "working") else ""
    return svg(f'  {behind}\n  <g>{shoulders}</g>\n  {head(eye, mouth, brow, tilt=tilt)}\n  {front}',
               view=BUST_VIEW)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet", help="also render a character reference sheet here")
    args = parser.parse_args()

    os.makedirs(OUT, exist_ok=True)
    for old in os.listdir(OUT):
        if old.endswith(".svg"):
            os.remove(os.path.join(OUT, old))
    written = []
    for state in FACES:
        for suffix, builder in (("full", build_full), ("panel", build_panel)):
            name = f"{state}-{suffix}.svg"
            with open(os.path.join(OUT, name), "w", encoding="utf-8") as handle:
                handle.write(builder(state))
            written.append(name)
    print(f"wrote {len(written)} files to {OUT}")
    if args.sheet:
        reference_sheet(args.sheet)
        print(f"wrote {args.sheet}")
    return 0


def reference_sheet(path: str) -> None:
    """One page showing both crops, every state and the palette.

    The point of a reference sheet is to see, at one glance, whether all
    fourteen drawings are the same character. Reading fourteen files never
    answers that; looking at them side by side does.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    states = list(FACES)
    image = QImage(1240, 980, QImage.Format.Format_ARGB32)
    image.fill(QColor("#FAFAFC"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    def draw(name: str, rect: QRectF) -> None:
        QSvgRenderer(os.path.join(OUT, name)).render(painter, rect)

    def label(text: str, x: float, y: float, size: int, bold: bool = False,
              colour: str = "#1E2430") -> None:
        font = QFont()
        font.setPointSize(size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(QColor(colour))
        painter.drawText(QRectF(x, y, 420, 26), int(Qt.AlignmentFlag.AlignLeft), text)

    label("Py", 40, 22, 24, True)
    label("Your AI companion in PyBrowser - stand-in artwork", 40, 58, 10,
          colour="#5A6070")

    label("7 FULL-BODY STATES   (new tab / large surfaces)", 40, 96, 9, True, BLUE)
    for index, state in enumerate(states):
        draw(f"{state}-full.svg", QRectF(40 + index * 170, 116, 155, 300))
        label(f"{index + 1}. {state.upper()}", 56 + index * 170, 424, 8, True)

    label("7 PANEL / BUST STATES   (agent panel / small surfaces)", 40, 470, 9,
          True, BLUE)
    for index, state in enumerate(states):
        draw(f"{state}-panel.svg", QRectF(40 + index * 170, 492, 155, 160))
        label(f"{index + 1}. {state.upper()}", 56 + index * 170, 660, 8, True)

    label("AT REAL SIZE   (44px agent panel, 34px narrow panel)", 40, 706, 9,
          True, BLUE)
    for index, state in enumerate(states):
        draw(f"{state}-panel.svg", QRectF(40 + index * 60, 728, 44, 44))
        draw(f"{state}-panel.svg", QRectF(40 + index * 60, 780, 34, 34))

    # The same busts on a dark ground: the sheet asks for artwork that works in
    # both themes, and orange on near-black is where that gets tested.
    painter.fillRect(QRectF(460, 716, 448, 112), QColor("#14141C"))
    label("ON DARK", 470, 700, 9, True, BLUE)
    for index, state in enumerate(states):
        draw(f"{state}-panel.svg", QRectF(474 + index * 60, 728, 44, 44))
        draw(f"{state}-panel.svg", QRectF(474 + index * 60, 780, 34, 34))

    label("PALETTE", 40, 856, 9, True, BLUE)
    swatches = ((BLUE, "hoodie"), (BLUE_LIGHT, "hood"), (BLUE_PALE, "trim"),
                (INK, "joggers"), (INK_SOFT, "highlight"), (FUR, "fur"),
                (CREAM, "muzzle"), (AMBER, "approval"), (RED, "deny"),
                (GREEN, "done"))
    for index, (colour, role) in enumerate(swatches):
        painter.fillRect(QRectF(40 + index * 118, 878, 34, 34), QColor(colour))
        label(colour, 82 + index * 118, 880, 7, True)
        label(role, 82 + index * 118, 896, 7, colour="#5A6070")

    painter.end()
    image.save(path, "PNG")
    del app


if __name__ == "__main__":
    raise SystemExit(main())
