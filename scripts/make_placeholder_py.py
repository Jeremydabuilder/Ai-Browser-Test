"""Generate the stand-in Py artwork.

    python scripts/make_placeholder_py.py            # write the asset files
    python scripts/make_placeholder_py.py --sheet X  # also render a reference sheet

Writes ``app/ui/assets/mascot/placeholder/<state>-{full,panel}.svg`` - seven
states, two crops - built from the Py character sheet: purple hoodie, white
rounded "P." badge, dark messy hair, black joggers, purple-and-white high-tops.

This is a placeholder. The character sheet is a polished 3D-style render and
this is flat vector, so it cannot be that; what it can be is the same
*character* - same silhouette, same outfit, same palette, the same seven poses
and the same seven lines - so the browser looks like itself, and so the final
artwork is a drop-in rather than a redesign.

Generated from one shared figure rather than drawn separately, because the
whole point is that every state is recognisably the same person: same hair,
same hoodie, same badge, same proportions. Only the pose, the props and the
face change.

Delete the output folder and the browser falls back to a plain drawn mark; drop
real artwork into ``assets/mascot/`` and it wins over all of this.
"""

from __future__ import annotations

import argparse
import os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "app", "ui", "assets", "mascot", "placeholder")

# Py's palette, taken from the character sheet's own swatches.
PURPLE = "#6C4CFF"        # hoodie
PURPLE_LIGHT = "#8A6BFF"  # hood lining, highlights
PURPLE_PALE = "#EDE9FF"   # high-tops, paper
PURPLE_DEEP = "#5238D6"   # hoodie shadow; derived from the sheet, not on it
INK = "#1F1F2E"           # joggers, linework
INK_SOFT = "#2D2D3F"      # jogger highlight
AMBER = "#FFC97A"         # the approval sign
RED = "#FF6B6B"           # deny
GREEN = "#17C37B"         # done

# Skin and hair are not on the swatch row; these read as the sheet's warm
# mid-tone skin and dark brown hair.
SKIN = "#F6CFA8"
SKIN_MID = "#E7B68E"
SKIN_SH = "#D2966B"
HAIR = "#3B2418"
HAIR_MID = "#54341F"
HAIR_HI = "#7A5138"
WHITE = "#FFFFFF"

#: Shared gradients. Flat fills are exactly the "generic corporate vector art"
#: the sheet asks Py not to look like; a little depth on the hoodie, the hair
#: and the skin is what separates a drawn character from a clip-art one.
DEFS = f"""<defs>
    <linearGradient id="hoodie" x1="0" y1="0" x2=".35" y2="1">
      <stop offset="0" stop-color="{PURPLE_LIGHT}"/>
      <stop offset=".55" stop-color="{PURPLE}"/>
      <stop offset="1" stop-color="{PURPLE_DEEP}"/>
    </linearGradient>
    <linearGradient id="skin" x1=".25" y1="0" x2=".8" y2="1">
      <stop offset="0" stop-color="{SKIN}"/>
      <stop offset="1" stop-color="{SKIN_MID}"/>
    </linearGradient>
    <linearGradient id="hair" x1=".2" y1="0" x2=".9" y2="1">
      <stop offset="0" stop-color="{HAIR_HI}"/>
      <stop offset=".45" stop-color="{HAIR_MID}"/>
      <stop offset="1" stop-color="{HAIR}"/>
    </linearGradient>
    <linearGradient id="legs" x1="0" y1="0" x2="1" y2=".2">
      <stop offset="0" stop-color="{INK_SOFT}"/>
      <stop offset="1" stop-color="{INK}"/>
    </linearGradient>
  </defs>"""


# -- the head ----------------------------------------------------------------
def head(eyes: str, mouth: str, brow: str = "", tilt: float = 0.0) -> str:
    """Py's head. Identical everywhere; only the face inside it changes.

    The hair is short, dark and deliberately untidy - a spiked fringe over the
    forehead and three tufts standing up off the crown. An earlier pass gave Py
    a top-knot, which read as a different character entirely.
    """
    rot = f' transform="rotate({tilt} 50 36)"' if tilt else ""
    return f"""<g id="head"{rot}>
    <path d="M45.2 44h9.6v11h-9.6Z" fill="{SKIN_MID}"/>
    <path d="M45.2 44h9.6v4.6c-3.4 1.4-6.6 1.4-9.6 0Z" fill="{SKIN_SH}" opacity=".55"/>
    <ellipse cx="34.2" cy="35" rx="3.1" ry="4.2" fill="{SKIN_MID}"/>
    <ellipse cx="65.8" cy="35" rx="3.1" ry="4.2" fill="{SKIN_MID}"/>
    <path d="M35 30.4c0-10.8 6.7-18 15-18s15 7.2 15 18v6c0 10.2-6.5 16.8-15 16.8S35 46.6 35 36.4Z" fill="url(#skin)"/>
    <path d="M50 53.2c-8.5 0-15-6.6-15-16.8v-2.6c2.2 7 7.8 11.6 15 11.6s12.8-4.6 15-11.6v2.6c0 10.2-6.5 16.8-15 16.8Z" fill="{SKIN_MID}" opacity=".38"/>
    <ellipse cx="39.6" cy="40.2" rx="3.4" ry="2.1" fill="{RED}" opacity=".22"/>
    <ellipse cx="60.4" cy="40.2" rx="3.4" ry="2.1" fill="{RED}" opacity=".22"/>
    <g fill="url(#hair)">
      <path d="M34.7 31.8C33.7 19.8 40.5 11.8 50 11.8s16.3 8 15.3 20c-1.2-4-3-6.6-4.9-8.2-1.5 1.9-3.7 2.8-6 2.3-2.8-.6-4-2.3-4.5-4-1.9 3.3-5.5 5.9-10.7 6.6-2.4.3-4.1 1.4-5.7 3.3Z"/>
      <path d="M52.6 13c2.7-3.6 5.6-4.8 8.7-3.7-2.5 1.3-4.2 3.2-5 5.7Z"/>
      <path d="M45.2 12.4c1-3.1 2.9-4.8 5.6-5-1.5 1.9-2.1 4-1.9 6.2Z"/>
      <path d="M58.8 15.2c2.9-2.5 5.6-3.1 8.1-1.9-2.3.6-4 2-5.4 4Z"/>
    </g>
    <path d="M38.6 17.6c2.6-3.2 6-5 10.2-5.4-3.4 1.6-6 3.8-8 6.6Z" fill="{HAIR_HI}" opacity=".7"/>
    {brow}
    {eyes}
    {mouth}
  </g>"""


# -- faces -------------------------------------------------------------------
def _eye(x: float, y: float, look: tuple = (0.0, 0.0)) -> str:
    """A big, wet, expressive eye - the sheet's most recognisable feature."""
    dx, dy = look
    return (f'<ellipse cx="{x}" cy="{y}" rx="4.3" ry="4.9" fill="{WHITE}"/>'
            f'<ellipse cx="{x + dx}" cy="{y + dy}" rx="3.3" ry="3.8" fill="{HAIR}"/>'
            f'<ellipse cx="{x + dx}" cy="{y + dy + .5}" rx="1.9" ry="2.2" fill="{INK}"/>'
            f'<circle cx="{x + dx - 1.2}" cy="{y + dy - 1.6}" r="1.4" fill="{WHITE}"/>'
            f'<circle cx="{x + dx + 1.4}" cy="{y + dy + 1.5}" r=".8" fill="{WHITE}" opacity=".65"/>')


def eyes(look=(0.0, 0.0)) -> str:
    return _eye(41.8, 35, look) + _eye(58.2, 35, look)


def eyes_happy() -> str:
    return (f'<path d="M37.6 36.4q4.2-5.4 8.4 0M54 36.4q4.2-5.4 8.4 0"'
            f' stroke="{INK}" stroke-width="3" fill="none" stroke-linecap="round"/>')


SMILE = (f'<path d="M44.8 44.6q5.2 4.6 10.4 0" stroke="{INK}" stroke-width="2.3"'
         ' fill="none" stroke-linecap="round"/>')
SMILE_SOFT = (f'<path d="M46.2 44.8q3.8 3 7.6 0" stroke="{INK}" stroke-width="2.2"'
              ' fill="none" stroke-linecap="round"/>')
GRIN = (f'<path d="M43.4 43.4q6.6 7 13.2 0Z" fill="{INK}"/>'
        f'<path d="M46.4 47.6q3.6 2.8 7.2 0Z" fill="{RED}" opacity=".85"/>')
FLAT = f'<path d="M46.4 45.4h7.2" stroke="{INK}" stroke-width="2.2" stroke-linecap="round"/>'
WAVY = (f'<path d="M45.6 45.4q1.8-1.8 3.6 0t3.6 0" stroke="{INK}" stroke-width="2.1"'
        ' fill="none" stroke-linecap="round"/>')
OH = f'<ellipse cx="50" cy="45.4" rx="2.7" ry="3.3" fill="{INK}"/>'

BROW_UP = (f'<path d="M37.4 28.4q4-2.2 8-.6M54.6 27.8q4-1.6 8 .8" stroke="{HAIR}"'
           ' stroke-width="2.2" fill="none" stroke-linecap="round"/>')
BROW_THINK = (f'<path d="M37.4 29.2q4-2.8 8 0M55 27.4q3.8-.8 7.6 1.4" stroke="{HAIR}"'
              ' stroke-width="2.2" fill="none" stroke-linecap="round"/>')
BROW_SOFT = (f'<path d="M37.6 28.6q4-1.6 8 0M54.4 28.6q4-1.6 8 0" stroke="{HAIR}"'
             ' stroke-width="2.1" fill="none" stroke-linecap="round"/>')

FACES = {
    "idle": (eyes(), SMILE, ""),
    "reading": (eyes(look=(0, 1.7)), SMILE_SOFT, BROW_SOFT),
    "thinking": (eyes(look=(1.9, -1.3)), FLAT, BROW_THINK),
    "working": (eyes(look=(0, 1.5)), SMILE_SOFT, BROW_SOFT),
    "approval": (eyes(), OH, BROW_UP),
    "complete": (eyes_happy(), GRIN, ""),
    "stuck": (eyes(look=(-1.4, .8)), WAVY, BROW_THINK),
}


# -- the body ----------------------------------------------------------------
def badge(cx: float, cy: float, size: float) -> str:
    """The chest badge: a white rounded square with a purple "P.", as drawn on
    the sheet and in the PyBrowser logo. It is the one mark that has to survive
    all the way down to 34 pixels, so it is a solid shape and not an outline.
    """
    half = size / 2
    return (f'<rect x="{cx - half}" y="{cy - half}" width="{size}" height="{size}"'
            f' rx="{round(size * 0.28, 2)}" fill="{WHITE}"/>'
            f'<text x="{cx}" y="{cy + size * 0.30}" text-anchor="middle"'
            f' font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif"'
            f' font-size="{round(size * 0.62, 2)}" font-weight="700" fill="{PURPLE}">P.</text>')


def torso(left_arm: str, right_arm: str) -> str:
    """Hoodie, joggers and high-tops: the same in every pose.

    The legs run most of the drawing's height on purpose. An earlier pass had
    Py as a chibi - head almost half the figure - and the sheet asks for "a
    small human companion rather than a chibi mascot". Around four heads tall
    is where a small flat character still looks drawn rather than squashed.
    """
    return f"""
    <path d="M38.4 74h11.1v46.6h-10.7Z" fill="url(#legs)"/>
    <path d="M50.5 74h11.1l-.4 46.6h-10.7Z" fill="url(#legs)"/>
    <path d="M38.4 74h5v46.6h-4.6Z" fill="{INK_SOFT}" opacity=".55"/>
    <path d="M38.1 118h11.4v7c0 1.6-1 2.4-2.7 2.4H33.6c-1.3 0-2-.7-2-1.9 0-2.9 2.2-5.2 6.5-7.5Z" fill="{PURPLE_PALE}"/>
    <path d="M50.5 118h11.4c4.3 2.3 6.5 4.6 6.5 7.5 0 1.2-.7 1.9-2 1.9H53.2c-1.7 0-2.7-.8-2.7-2.4Z" fill="{PURPLE_PALE}"/>
    <path d="M38.1 118h11.4v3.6H36.2c.4-1.2 1.6-2.4 3.7-3.6Zm12.4 0h11.4c2.1 1.2 3.3 2.4 3.7 3.6H50.5Z" fill="{PURPLE_LIGHT}" opacity=".5"/>
    <path d="M31.6 124.4h17.9v3H33.6c-1.3 0-2-.7-2-1.9Zm18.9 0h17.9c0 2.2-.7 3-2 3H53.2c-1.7 0-2.7-.8-2.7-2.4Z" fill="{PURPLE}"/>
    <path d="M33 74.8c0-10.6 6.8-19 17-19s17 8.4 17 19v2.4c0 1.7-1 2.6-2.9 2.6-9.4 1.2-18.8 1.2-28.2 0-1.9 0-2.9-.9-2.9-2.6Z" fill="url(#hoodie)"/>
    <path d="M35.9 79.8c-1.9 0-2.9-.9-2.9-2.6v-2.4c0-8 4-14.6 10.4-17.4l-2.6 22.6Z" fill="{PURPLE_LIGHT}" opacity=".45"/>
    <path d="M57 57.2l2.9 22.6h4.2c1.9 0 2.9-.9 2.9-2.6v-2.4c0-8.4-4.2-15.2-10-17.6Z" fill="{PURPLE_DEEP}" opacity=".4"/>
    <path d="M41 55.1c2.7 4.4 5.7 6.6 9 6.6s6.3-2.2 9-6.6c2.5.9 4.5 2.3 6.1 4-3.5 4.5-8.8 7.2-15.1 7.2s-11.6-2.7-15.1-7.2c1.6-1.7 3.6-3.1 6.1-4Z" fill="{PURPLE_LIGHT}"/>
    <path d="M44.6 61.4c1.7.4 3.5.5 5.4.5s3.7-.1 5.4-.5l-1.4 5.6h-8Z" fill="{PURPLE_DEEP}" opacity=".35"/>
    {badge(50, 75, 13)}
    {left_arm}
    {right_arm}
"""


#: The full body's crop. The figure is drawn in a 100-wide space but only
#: occupies the middle two thirds of it; left at 100 wide the new-tab page sized
#: Py by that empty margin and the character came out a thin strip in a wide
#: box. Trimmed to what is actually drawn - arms and props included.
FULL_VIEW = "16 0 68 132"

#: The bust's crop. Found by looking at it at 44 pixels rather than by
#: reasoning about it: too wide and Py is a smudge in the corner of the panel,
#: too tight and the book, the laptop and the sign - the only things that tell
#: the states apart at that size - are cropped away.
BUST_VIEW = "14 1 72 94"


def svg(body: str, view: str = FULL_VIEW) -> str:
    """Wrap a body in an SVG whose intrinsic size matches its viewBox.

    The intrinsic size matters: the new-tab page sizes Py with `height` and
    `width: auto`, so the drawing's own aspect is what decides how wide the
    character ends up.
    """
    _, _, width, height = view.split()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" '
            f'width="{width}" height="{height}">\n  {DEFS}\n{body}\n</svg>\n')


def arm(path: str, hand: tuple[float, float] | None = None) -> str:
    """A sleeve and the hand on the end of it.

    Drawn a shade darker than the hoodie body on purpose: at the sheet's mid
    purple the arms disappeared into the torso and Py looked like a hoodie with
    hands floating beside it. The silhouette has to survive at 34 pixels.
    """
    out = (f'<path d="{path}" stroke="{PURPLE_DEEP}" stroke-width="8.4" fill="none"'
           ' stroke-linecap="round"/>')
    if hand:
        out += f'<circle cx="{hand[0]}" cy="{hand[1]}" r="5.2" fill="{SKIN}"/>'
    return out


def fist(x: float, y: float) -> str:
    return (f'<circle cx="{x}" cy="{y}" r="5" fill="{SKIN}"/>'
            f'<path d="M{x - 3.4} {y - .6}h6.8" stroke="{SKIN_SH}" stroke-width="1.3"'
            ' stroke-linecap="round" opacity=".8"/>')


ARM_REST_L = arm("M34.6 63c-3.6 3.8-5 8-4.4 12.4", (30.2, 77.8))
ARM_REST_R = arm("M65.4 63c3.6 3.8 5 8 4.4 12.4", (69.8, 77.8))

POSES = {
    # Standing, one hand raised in a small wave. The "meet Py" pose.
    "idle": (ARM_REST_L, arm("M65.4 63c4.8-1.8 7.8-6 8.4-11", (74.6, 50.6))),
    # Both hands holding an open book, eyes down on it.
    "reading": (arm("M34.6 63c-1.4 4.6-.6 8.6 2.4 11.6", (37.4, 76.4)),
                arm("M65.4 63c1.4 4.6.6 8.6-2.4 11.6", (62.6, 76.4))),
    # One hand to the chin.
    "thinking": (ARM_REST_L, arm("M65.4 63c3.6 2 3.8 7.2 1 10.6M66.4 73.6c-3-3.4-4-9.8-3.8-14.6",
                                 (61.8, 52.4))),
    # Both hands forward on a laptop.
    "working": (arm("M34.6 63c-.4 5.2 1.4 9 4.8 11.2", (39.8, 76)),
                arm("M65.4 63c.4 5.2-1.4 9-4.8 11.2", (60.2, 76))),
    # One hand up, holding a small sign.
    "approval": (ARM_REST_L, arm("M65.4 63c5.6-2.8 8.2-8 8.2-13.6", (73.6, 49))),
    # One fist punched up, the other hand relaxed - the sheet's celebration.
    "complete": (ARM_REST_L,
                 arm("M65.4 63c5.8-3 8.6-9 8.8-15.6") + fist(74.2, 46.2)),
    # Hand to the back of the neck - the universal "I have no idea".
    "stuck": (ARM_REST_L,
              arm("M65.4 63c4.6-1.2 6.8-5 6.8-10.2M72.2 52.8c-3.6-.6-6.4-2.8-8.2-6.2",
                  (62.6, 45.4))),
}

PROPS_FULL = {
    "reading": (f'<g transform="translate(0 8)">'
                f'<path d="M36 74h28v13H36Z" fill="{PURPLE_PALE}" stroke="{PURPLE_LIGHT}" stroke-width="1.6"/>'
                f'<path d="M50 74v13" stroke="{PURPLE_LIGHT}" stroke-width="1.4"/>'
                f'<path d="M39 78h8M39 81.4h8M53 78h8M53 81.4h8" stroke="{PURPLE_LIGHT}"'
                ' stroke-width="1.2" stroke-linecap="round" opacity=".8"/></g>'),
    "thinking": (f'<g fill="{PURPLE_LIGHT}"><circle cx="74" cy="14" r="6.4" opacity=".9"/>'
                 f'<circle cx="66.4" cy="23" r="3.2" opacity=".72"/>'
                 f'<circle cx="62" cy="29.4" r="1.9" opacity=".55"/></g>'),
    "working": (f'<g><path d="M34 96h32l3 5H31Z" fill="{PURPLE_DEEP}"/>'
                f'<rect x="36" y="80" width="28" height="16" rx="1.8" fill="{INK}"/>'
                f'<rect x="37.6" y="81.6" width="24.8" height="12.8" rx="1" fill="{PURPLE_LIGHT}" opacity=".55"/>'
                f'{badge(50, 88, 7.4)}</g>'),
    "approval": (f'<g><rect x="63" y="30" width="20" height="19" rx="4" fill="{AMBER}"/>'
                 f'<path d="M73 35v7.2" stroke="{INK}" stroke-width="2.8" stroke-linecap="round"/>'
                 f'<circle cx="73" cy="45.6" r="1.6" fill="{INK}"/>'
                 f'<path d="M70.6 49h4.8L73 53Z" fill="{AMBER}"/></g>'),
    # Confetti in the sheet's five colours, scattered but not symmetrical - a
    # mirrored burst reads as a pattern rather than as a moment.
    "complete": (f'<g>'
                 f'<rect x="24" y="20" width="4.4" height="4.4" rx="1.1" fill="{AMBER}" transform="rotate(20 26 22)"/>'
                 f'<rect x="60" y="9" width="3.8" height="3.8" rx="1" fill="{GREEN}" transform="rotate(35 62 11)"/>'
                 f'<rect x="33" y="8" width="3.6" height="3.6" rx="1" fill="{RED}" transform="rotate(-18 35 10)"/>'
                 f'<rect x="78" y="26" width="4" height="4" rx="1" fill="{PURPLE}" transform="rotate(-28 80 28)"/>'
                 f'<circle cx="20" cy="40" r="2.3" fill="{PURPLE_LIGHT}"/>'
                 f'<circle cx="80" cy="14" r="2.1" fill="{AMBER}"/>'
                 f'<circle cx="28" cy="34" r="1.7" fill="{GREEN}"/>'
                 f'<path d="M22 12l-2.6-3.6M46 4.6V1M68 20l3-3.4" stroke="{AMBER}"'
                 ' stroke-width="2.4" stroke-linecap="round"/></g>'),
    # A scribble over the head. It is the one state that must never be mistaken
    # for a good outcome, so it gets the most obviously unhappy prop.
    "stuck": (f'<path d="M64.4 14.6c-4.6-3.4-9-2.4-9.8 1.6-.7 3.4 3.4 5 5.8 3.2'
              f' 2.6-2 1.4-6.2-2.4-6.6-4-.4-6.6 3.2-5.4 6.8" stroke="{PURPLE_LIGHT}"'
              ' stroke-width="2.4" fill="none" stroke-linecap="round"/>'),
}

# Props for the bust, kept inside the tighter crop below - anything drawn
# outside it is simply not on screen.
BUST_PROPS = {
    # Held up into the crop, so reading and working are still tellable apart at
    # 44 pixels - below the crop they were invisible and every state looked
    # like idle with different eyes.
    "reading": (f'<path d="M27 63h46v13H27Z" fill="{PURPLE_PALE}" stroke="{PURPLE_LIGHT}" stroke-width="2"/>'
                f'<path d="M50 63v13" stroke="{PURPLE_LIGHT}" stroke-width="1.8"/>'
                f'<path d="M31.5 67.5h13M55.5 67.5h13" stroke="{PURPLE_LIGHT}" stroke-width="1.6"'
                ' stroke-linecap="round" opacity=".85"/>'),
    "thinking": (f'<g fill="{PURPLE_LIGHT}"><circle cx="72" cy="11" r="5.6" opacity=".9"/>'
                 f'<circle cx="65.5" cy="18" r="2.9" opacity=".72"/></g>'),
    "working": (f'<rect x="29" y="64" width="42" height="13" rx="2" fill="{INK}"/>'
                f'<rect x="31.2" y="66.2" width="37.6" height="8.6" rx="1.2" fill="{PURPLE_LIGHT}" opacity=".55"/>'
                f'{badge(50, 70.5, 6.4)}'),
    "approval": (f'<g><rect x="62" y="6" width="18" height="17" rx="3.6" fill="{AMBER}"/>'
                 f'<path d="M71 10.4v6.2" stroke="{INK}" stroke-width="2.6" stroke-linecap="round"/>'
                 f'<circle cx="71" cy="19.6" r="1.6" fill="{INK}"/></g>'),
    "complete": (f'<g>'
                 f'<rect x="24" y="18" width="4" height="4" rx="1" fill="{AMBER}" transform="rotate(20 26 20)"/>'
                 f'<rect x="72" y="14" width="3.6" height="3.6" rx="1" fill="{GREEN}" transform="rotate(-25 74 16)"/>'
                 f'<rect x="30" y="8" width="3.4" height="3.4" rx="1" fill="{RED}" transform="rotate(-15 32 10)"/>'
                 f'<circle cx="21" cy="32" r="2.3" fill="{PURPLE_LIGHT}"/>'
                 f'<circle cx="78" cy="30" r="2.1" fill="{AMBER}"/>'
                 f'<path d="M25 12l-2.6-3.4M75 8l2.6-3.4" stroke="{AMBER}"'
                 ' stroke-width="2.6" stroke-linecap="round"/></g>'),
    "stuck": (f'<path d="M66.4 10.6c-4.6-3.4-9-2.4-9.8 1.6-.7 3.4 3.4 5 5.8 3.2'
              f' 2.6-2 1.4-6.2-2.4-6.6-4-.4-6.6 3.2-5.4 6.8" stroke="{PURPLE_LIGHT}"'
              ' stroke-width="2.4" fill="none" stroke-linecap="round"/>'),
}

#: How much smaller the head is on the full body than in the bust. The sheet
#: asks for "a small human companion rather than a chibi mascot"; drawn at bust
#: scale the head was a third of the standing figure. Scaled about the chin, so
#: the neck and the collar still meet it.
HEAD_SCALE = 0.9
HEAD_PIVOT = (50, 53.2)

#: Props that belong to the head rather than to a hand, and so have to be
#: scaled along with it or they drift off the crown.
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
    # Shoulders begin just under the chin, so the crop is face-first, and stay
    # narrower than the frame - filling it edge to edge read as a purple band
    # rather than as a person.
    shoulders = (
        f'<path d="M45.2 44h9.6v13h-9.6Z" fill="{SKIN_MID}"/>'
        f'<path d="M26 101v-7c0-11.6 8-20.8 18-22.8h12c10 2 18 11.2 18 22.8v7Z" fill="url(#hoodie)"/>'
        f'<path d="M32.6 101v-7c0-7 3-13.4 8-17.4L43 101Z" fill="{PURPLE_LIGHT}" opacity=".42"/>'
        f'<path d="M37 67.4c3.8 5.4 8.2 8 13 8s9.2-2.6 13-8c3.2 1.1 5.8 2.8 7.6 4.8-5 5.8-12.1 9.3-20.6 9.3s-15.6-3.5-20.6-9.3c1.8-2 4.4-3.7 7.6-4.8Z" fill="{PURPLE_LIGHT}"/>'
        f'{badge(50, 87, 11)}')
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
    """One page showing the master character, both crops and the palette.

    The point of a reference sheet is to see, at one glance, whether all
    fourteen drawings are the same person. Reading fourteen files never answers
    that; looking at them side by side does.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    states = list(FACES)
    image = QImage(1240, 940, QImage.Format.Format_ARGB32)
    image.fill(QColor("#FAFAFC"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    def draw(name: str, rect: QRectF) -> None:
        QSvgRenderer(os.path.join(OUT, name)).render(painter, rect)

    def label(text: str, x: float, y: float, size: int, bold: bool = False,
              colour: str = "#1F1F2E") -> None:
        font = QFont()
        font.setPointSize(size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(QColor(colour))
        painter.drawText(QRectF(x, y, 420, 26), int(Qt.AlignmentFlag.AlignLeft), text)

    label("Py", 40, 22, 24, True)
    label("Your AI companion in PyBrowser - stand-in artwork", 40, 58, 10,
          colour="#5A5A6E")

    label("7 FULL-BODY STATES   (new tab / large surfaces)", 40, 96, 9, True, PURPLE)
    for index, state in enumerate(states):
        draw(f"{state}-full.svg", QRectF(40 + index * 170, 116, 150, 300))
        label(f"{index + 1}. {state.upper()}", 56 + index * 170, 424, 8, True)

    label("7 PANEL / BUST STATES   (agent panel / small surfaces)", 40, 470, 9,
          True, PURPLE)
    for index, state in enumerate(states):
        draw(f"{state}-panel.svg", QRectF(40 + index * 170, 492, 150, 150))
        label(f"{index + 1}. {state.upper()}", 56 + index * 170, 650, 8, True)

    label("AT REAL SIZE   (44px agent panel, 34px narrow panel)", 40, 700, 9,
          True, PURPLE)
    for index, state in enumerate(states):
        draw(f"{state}-panel.svg", QRectF(40 + index * 60, 722, 44, 44))
        draw(f"{state}-panel.svg", QRectF(40 + index * 60, 774, 34, 34))

    label("PALETTE", 40, 832, 9, True, PURPLE)
    swatches = ((PURPLE, "hoodie"), (PURPLE_LIGHT, "hood"), (PURPLE_PALE, "high-tops"),
                (INK, "joggers"), (INK_SOFT, "highlight"), (AMBER, "approval"),
                (RED, "deny"), (GREEN, "done"))
    for index, (colour, role) in enumerate(swatches):
        painter.fillRect(QRectF(40 + index * 120, 854, 34, 34), QColor(colour))
        label(colour, 84 + index * 120, 856, 7, True)
        label(role, 84 + index * 120, 872, 7, colour="#5A5A6E")

    painter.end()
    image.save(path, "PNG")
    del app


if __name__ == "__main__":
    raise SystemExit(main())
