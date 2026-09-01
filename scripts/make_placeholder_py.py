"""Generate the stand-in Py artwork.

    python scripts/make_placeholder_py.py

Writes ``app/ui/assets/mascot/placeholder/<state>-{full,panel}.svg`` - seven
states, two crops. These are a placeholder for the real illustrated Py, and
they exist so the browser looks like itself before that artwork lands.

Generated from one shared figure rather than drawn separately, because the
whole point is that every state is recognisably the *same character*: same
hair, same hoodie, same badge, same proportions. Only the pose, the props and
the face change.

Delete the output folder and the browser falls back to a plain drawn mark; drop
real artwork into ``assets/mascot/`` and it wins over all of this.
"""

from __future__ import annotations

import os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "app", "ui", "assets", "mascot", "placeholder")

# Py's palette, from the character sheet.
HAIR = "#4A2F22"
HAIR_MID = "#5E3C2B"
HAIR_HI = "#7A5138"
SKIN = "#F3CBA8"
SKIN_MID = "#E7B68E"
SKIN_SH = "#D79E74"
HOODIE = "#6C5CE7"
HOODIE_SH = "#5646C9"
HOODIE_HI = "#8172F0"
HOOD = "#A78BFA"
TROUSERS = "#2B2D42"
TROUSERS_HI = "#3A3D57"
SHOE = "#EDEAFB"
SHOE_SH = "#9E96D6"
INK = "#2B2D42"
WHITE = "#FFFFFF"
CYAN = "#5DD4FF"
YELLOW = "#FFD166"
ORANGE = "#FF8A65"
PAPER = "#F5F6FA"


def head(eyes: str, mouth: str, brow: str = "", cx: float = 0.0,
         tilt: float = 0.0, blush: bool = True) -> str:
    """Py's head. Identical everywhere; only the face inside it changes."""
    rot = f' transform="rotate({tilt} {50 + cx} 34)"' if tilt else ""
    cheeks = (f'<ellipse cx="{39.5 + cx}" cy="38.5" rx="3.4" ry="2.2" fill="{ORANGE}" opacity=".3"/>'
              f'<ellipse cx="{60.5 + cx}" cy="38.5" rx="3.4" ry="2.2" fill="{ORANGE}" opacity=".3"/>'
              ) if blush else ""
    return f"""<g id="head"{rot}>
    <ellipse cx="{33.5 + cx}" cy="33.5" rx="3" ry="4" fill="{SKIN_MID}"/>
    <ellipse cx="{66.5 + cx}" cy="33.5" rx="3" ry="4" fill="{SKIN_MID}"/>
    <path d="M{35 + cx} 29c0-10.4 6.6-17.4 15-17.4s15 7 15 17.4v6.4c0 9.8-6.4 16.2-15 16.2s-15-6.4-15-16.2Z" fill="{SKIN}"/>
    <path d="M{50 + cx} 51.6c-8.6 0-15-6.4-15-16.2v-2.2c2 6.6 7.6 11 15 11s13-4.4 15-11v2.2c0 9.8-6.4 16.2-15 16.2Z" fill="{SKIN_MID}" opacity=".45"/>
    <path d="M{34.6 + cx} 30.6C33.6 18.6 40.4 10.6 50 10.6s16.4 8 15.4 20c-1.2-4-3-6.6-4.9-8.2-1.5 1.9-3.7 2.8-6 2.3-2.8-.6-4-2.3-4.5-4-1.9 3.3-5.5 5.9-10.7 6.6-2.4.3-4.1 1.4-5.7 3.3Z" fill="{HAIR}"/>
    <path d="M{57 + cx} 11.8c3.3-3.3 6.8-4.2 8.2-2.7 1.4 1.5.3 4.1-3.2 6.9Z" fill="{HAIR_MID}"/>
    <path d="M{39 + cx} 16.4c2.6-2.6 6-4 9.6-4.2-3 1.4-5.4 3.2-7.2 5.6Z" fill="{HAIR_HI}" opacity=".8"/>
    {cheeks}
    {brow}
    {eyes}
    {mouth}
  </g>"""


# -- faces -------------------------------------------------------------------
def _eye(x: float, y: float, r: float = 3.0, look: tuple = (0.0, 0.0)) -> str:
    dx, dy = look
    return (f'<ellipse cx="{x}" cy="{y}" rx="{r}" ry="{r * 1.22}" fill="{INK}"/>'
            f'<circle cx="{x + dx + 0.9}" cy="{y + dy - 1.1}" r="1.15" fill="{WHITE}"/>')


def eyes(look=(0.0, 0.0), cx: float = 0.0) -> str:
    return _eye(42 + cx, 34, look=look) + _eye(58 + cx, 34, look=look)


def eyes_happy(cx: float = 0.0) -> str:
    return (f'<path d="M{38.4 + cx} 35.4q3.6-4.6 7.2 0M{54.4 + cx} 35.4q3.6-4.6 7.2 0"'
            f' stroke="{INK}" stroke-width="2.8" fill="none" stroke-linecap="round"/>')


def eyes_closed(cx: float = 0.0) -> str:
    return (f'<path d="M{38.4 + cx} 34.6q3.6 3.4 7.2 0M{54.4 + cx} 34.6q3.6 3.4 7.2 0"'
            f' stroke="{INK}" stroke-width="2.6" fill="none" stroke-linecap="round"/>')


SMILE = f'<path d="M45.4 43.4q4.6 3.8 9.2 0" stroke="{INK}" stroke-width="2.2" fill="none" stroke-linecap="round"/>'
SMILE_SOFT = f'<path d="M46.6 43.6q3.4 2.6 6.8 0" stroke="{INK}" stroke-width="2.1" fill="none" stroke-linecap="round"/>'
GRIN = (f'<path d="M44 42.2q6 6 12 0Z" fill="{INK}"/>'
        f'<path d="M46.6 45.6q3.4 2.4 6.8 0Z" fill="{ORANGE}"/>')
FLAT = f'<path d="M46.6 44h6.8" stroke="{INK}" stroke-width="2.1" stroke-linecap="round"/>'
OH = f'<ellipse cx="50" cy="44" rx="2.6" ry="3.2" fill="{INK}"/>'
BROW_UP = (f'<path d="M38 28.4q3.6-2 7.2-.6M54.8 27.8q3.6-1.4 7.2.6" stroke="{HAIR}"'
           ' stroke-width="2" fill="none" stroke-linecap="round"/>')
BROW_THINK = (f'<path d="M38 29q3.6-2.6 7.2 0M55.2 27.6q3.4-.8 6.8 1.2" stroke="{HAIR}"'
              ' stroke-width="2" fill="none" stroke-linecap="round"/>')

FACES = {
    "idle": (eyes(), SMILE, ""),
    "reading": (eyes(look=(0, 1.6)), SMILE_SOFT, ""),
    "thinking": (eyes(look=(1.8, -1.2)), FLAT, BROW_THINK),
    "working": (eyes(look=(0, 1.4)), SMILE_SOFT, ""),
    "approval": (eyes(), OH, BROW_UP),
    "complete": (eyes_happy(), GRIN, ""),
    "stuck": (eyes(look=(0, .8)), FLAT, BROW_THINK),
}


# -- full body ---------------------------------------------------------------
def torso(left_arm: str, right_arm: str) -> str:
    """Hoodie, legs and shoes: the same in every pose.

    The legs run most of the drawing's height on purpose. An earlier pass had
    Py as a chibi - head almost half the figure - and at new-tab size that read
    as a sticker rather than a character. Roughly four heads tall is the point
    where a small flat mascot still looks drawn rather than squashed.
    """
    return f"""
    <path d="M39.8 74h9.2v50.5h-8.8Z" fill="{TROUSERS}"/>
    <path d="M51 74h9.2l-.4 50.5h-8.8Z" fill="{TROUSERS}"/>
    <path d="M39.8 74h4.4v50.5h-4Z" fill="{TROUSERS_HI}" opacity=".45"/>
    <path d="M51 74h4.4v50.5H51Z" fill="{TROUSERS_HI}" opacity=".28"/>
    <path d="M39.6 122.5h9.2v6.2c0 1.3-.8 2-2.2 2H35.6c-1.1 0-1.7-.6-1.7-1.6 0-2.4 1.7-4.4 5.7-6.6Z" fill="{SHOE}"/>
    <path d="M51.2 122.5h9.2c4 2.2 5.7 4.2 5.7 6.6 0 1-.6 1.6-1.7 1.6H53.4c-1.4 0-2.2-.7-2.2-2Z" fill="{SHOE}"/>
    <path d="M33.9 128.6h14.9v2.1H35.6c-1.1 0-1.7-.6-1.7-1.4Zm17.3 0h14.9c0 1.5-.6 2.1-1.7 2.1H53.4c-1.4 0-2.2-.7-2.2-2Z" fill="{SHOE_SH}"/>
    <path d="M31 74.4c0-10.6 7.4-18.6 19-18.6s19 8 19 18.6v2.6c0 1.4-1 2.2-2.6 2.2H33.6c-1.6 0-2.6-.8-2.6-2.2Z" fill="{HOODIE}"/>
    <path d="M33.6 79.2c-1.6 0-2.6-.8-2.6-2.2v-2.6c0-8 4.2-14.6 11-17.3l-2.6 22.1Z" fill="{HOODIE_HI}" opacity=".5"/>
    <path d="M58 57.4l3 21.8h5.4c1.6 0 2.6-.8 2.6-2.2v-2.6c0-8.4-4.6-15.2-11-17Z" fill="{HOODIE_SH}" opacity=".45"/>
    <path d="M40.6 55.4c2.8 4.4 5.9 6.6 9.4 6.6s6.6-2.2 9.4-6.6c2.6.9 4.8 2.3 6.4 4-3.6 4.6-9.2 7.4-15.8 7.4s-12.2-2.8-15.8-7.4c1.6-1.7 3.8-3.1 6.4-4Z" fill="{HOOD}"/>
    <circle cx="50" cy="74.5" r="6.2" fill="{HOOD}"/>
    <text x="50" y="78" font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif"
          font-size="8.4" font-weight="700" fill="{WHITE}" text-anchor="middle">P.</text>
    {left_arm}
    {right_arm}
"""


#: The full body's crop. The figure is drawn in a 100-wide space but only
#: occupies the middle two thirds of it; left at 100 wide the new-tab page sized
#: Py by that empty margin and the character came out a thin strip in a wide
#: box. Trimmed to what is actually drawn - arms and props included.
FULL_VIEW = "16 0 68 136"


def svg(body: str, view: str = FULL_VIEW) -> str:
    """Wrap a body in an SVG whose intrinsic size matches its viewBox.

    The intrinsic size matters: the new-tab page sizes Py with `height` and
    `width: auto`, so the drawing's own aspect is what decides how wide the
    character ends up.
    """
    _, _, width, height = view.split()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" '
            f'width="{width}" height="{height}">\n{body}\n</svg>\n')


def arm(path: str, hand: tuple[float, float] | None = None,
        colour: str = HOODIE) -> str:
    out = f'<path d="{path}" stroke="{colour}" stroke-width="8.4" fill="none" stroke-linecap="round"/>'
    if hand:
        out += f'<circle cx="{hand[0]}" cy="{hand[1]}" r="4.4" fill="{SKIN}"/>'
    return out


ARM_REST_L = arm("M36 66c-3.4 3-5 6.6-5 10.6", (31, 78.6))
ARM_REST_R = arm("M64 66c3.4 3 5 6.6 5 10.6", (69, 78.6))

POSES = {
    # Standing, one hand raised in a small wave. The "meet Py" pose.
    "idle": (ARM_REST_L, arm("M64 66c4.6-1.6 7.6-5.6 8.4-10.4", (73.6, 53.2))),
    # Both hands holding a book, eyes down on it.
    "reading": (arm("M36 66c-1.6 4-1 7.6 1.6 10.4", (38.6, 78)),
                arm("M64 66c1.6 4 1 7.6-1.6 10.4", (61.4, 78))),
    # One hand to the chin.
    "thinking": (ARM_REST_L, arm("M64 66c3.4 1.6 3.6 6.6 1 9.8M65 75.8c-2.6-3-3.6-9-3.6-13.4",
                                 (61.4, 50.6))),
    # Both hands forward on a laptop.
    "working": (arm("M36 66c-.6 4.6 1 8 4 10", (41, 77.6)),
                arm("M64 66c.6 4.6-1 8-4 10", (59, 77.6))),
    # One hand up, holding a small sign.
    "approval": (ARM_REST_L, arm("M64 66c5.4-2.6 8-7.6 8-13", (72.4, 51.4))),
    # Both arms up.
    "complete": (arm("M36 66c-5.4-2.4-8.4-7.6-9-13.6", (26.6, 50.4)),
                 arm("M64 66c5.4-2.4 8.4-7.6 9-13.6", (73.4, 50.4))),
    # Chin resting on one hand, elbow tucked - patient, a bit flat.
    "stuck": (arm("M36 66c-3 3.4-3.4 7-1.4 10.2", (35.4, 78)),
              arm("M64 66c2.6 2 2.4 6 .4 8.6M64.4 74.6c-2.4-3.2-3-8.4-2.8-12.6",
                  (61.6, 50.4))),
}

PROPS_FULL = {
    "reading": (f'<g transform="translate(0 2)">'
                f'<path d="M36 74h28v13H36Z" fill="{PAPER}" stroke="{CYAN}" stroke-width="1.6"/>'
                f'<path d="M50 74v13" stroke="{CYAN}" stroke-width="1.4"/>'
                f'<path d="M39 78h8M39 81.4h8M53 78h8M53 81.4h8" stroke="{CYAN}"'
                ' stroke-width="1.2" stroke-linecap="round" opacity=".8"/></g>'),
    "thinking": (f'<g fill="{HOOD}"><circle cx="74" cy="16" r="6.4" opacity=".92"/>'
                 f'<circle cx="66" cy="24.4" r="3.2" opacity=".75"/>'
                 f'<circle cx="61.4" cy="30" r="1.9" opacity=".6"/></g>'),
    "working": (f'<g><path d="M34 88h32l3 5H31Z" fill="{HOODIE_SH}"/>'
                f'<rect x="36" y="72" width="28" height="16" rx="1.8" fill="{INK}"/>'
                f'<rect x="37.6" y="73.6" width="24.8" height="12.8" rx="1" fill="{CYAN}" opacity=".55"/>'
                f'<circle cx="50" cy="80" r="3" fill="{HOOD}"/></g>'),
    "approval": (f'<g><rect x="62" y="30" width="21" height="20" rx="3.4" fill="{YELLOW}"/>'
                 f'<path d="M72.5 35.4v7.6" stroke="{INK}" stroke-width="2.8" stroke-linecap="round"/>'
                 f'<circle cx="72.5" cy="46.4" r="1.6" fill="{INK}"/>'
                 f'<path d="M70 50l2.5 4 2.5-4Z" fill="{YELLOW}"/></g>'),
    "complete": (f'<g stroke-linecap="round">'
                 f'<path d="M22 40l-3 -4M78 40l3 -4M50 6v-4" stroke="{YELLOW}" stroke-width="2.4"/>'
                 f'<circle cx="18" cy="52" r="2.2" fill="{CYAN}"/>'
                 f'<circle cx="82" cy="52" r="2.2" fill="{ORANGE}"/>'
                 f'<rect x="24" y="20" width="4" height="4" rx="1" fill="{ORANGE}" transform="rotate(20 26 22)"/>'
                 f'<rect x="72" y="18" width="4" height="4" rx="1" fill="{CYAN}" transform="rotate(-25 74 20)"/>'
                 f'<rect x="60" y="10" width="3.4" height="3.4" rx="1" fill="{YELLOW}" transform="rotate(35 62 12)"/></g>'),
    "stuck": (f'<g><path d="M70 40h12l-6 7Zm0 20h12l-6-7Z" fill="{HOOD}" opacity=".9"/>'
              f'<path d="M69 39h14M69 61h14" stroke="{HOOD}" stroke-width="2.2" stroke-linecap="round"/>'
              f'<path d="M74.6 45.4h2.8l-1.4 1.8Z" fill="{YELLOW}"/></g>'),
}

# Props for the bust, kept inside the tighter crop below - anything drawn
# outside it is simply not on screen.
BUST_PROPS = {
    # Held up into the crop, so reading and working are still tellable apart
    # at 40 pixels - below the crop they were invisible and every state looked
    # like idle with different eyes.
    "reading": (f'<path d="M27 63h46v13H27Z" fill="{PAPER}" stroke="{CYAN}" stroke-width="2"/>'
                f'<path d="M50 63v13" stroke="{CYAN}" stroke-width="1.8"/>'
                f'<path d="M31.5 67.5h13M55.5 67.5h13" stroke="{CYAN}" stroke-width="1.6"'
                ' stroke-linecap="round" opacity=".85"/>'),
    "thinking": (f'<g fill="{HOOD}"><circle cx="72" cy="12" r="5.6" opacity=".92"/>'
                 f'<circle cx="65.5" cy="19" r="2.9" opacity=".75"/></g>'),
    "working": (f'<rect x="29" y="64" width="42" height="13" rx="2" fill="{INK}"/>'
                f'<rect x="31.2" y="66.2" width="37.6" height="8.6" rx="1.2" fill="{CYAN}" opacity=".55"/>'
                f'<circle cx="50" cy="70.5" r="2.4" fill="{HOOD}"/>'),
    "approval": (f'<g><rect x="62" y="7" width="18" height="17" rx="3" fill="{YELLOW}"/>'
                 f'<path d="M71 11.4v6.2" stroke="{INK}" stroke-width="2.6" stroke-linecap="round"/>'
                 f'<circle cx="71" cy="20.6" r="1.6" fill="{INK}"/></g>'),
    "complete": (f'<g stroke-linecap="round">'
                 f'<path d="M25 20l-2.6-3.4M75 20l2.6-3.4" stroke="{YELLOW}" stroke-width="2.6"/>'
                 f'<circle cx="23" cy="34" r="2.3" fill="{CYAN}"/>'
                 f'<circle cx="77" cy="34" r="2.3" fill="{ORANGE}"/></g>'),
    "stuck": (f'<g><path d="M67 13h11l-5.5 6.4Zm0 19h11l-5.5-6.4Z" fill="{HOOD}" opacity=".9"/>'
              f'<path d="M66.2 12h12.6M66.2 33h12.6" stroke="{HOOD}" stroke-width="2.2"'
              ' stroke-linecap="round"/></g>'),
}


def build_full(state: str) -> str:
    eye, mouth, brow = FACES[state]
    left, right = POSES[state]
    prop = PROPS_FULL.get(state, "")
    tilt = {"thinking": -4, "stuck": -3, "complete": 0}.get(state, 0)
    return svg(f'  <g>{torso(left, right)}</g>\n  {head(eye, mouth, brow, tilt=tilt)}\n  {prop}')


#: The bust's crop. Found by looking at it at 44 pixels rather than by
#: reasoning about it: too wide and Py is a smudge in the corner of the panel,
#: too tight and the book, the laptop and the sign - the only things that tell
#: the states apart at that size - are cropped away.
BUST_VIEW = "15 1 70 90"


def build_panel(state: str) -> str:
    """Head and shoulders, framed tightly. The same head as the full body."""
    eye, mouth, brow = FACES[state]
    prop = BUST_PROPS.get(state, "")
    tilt = {"thinking": -4, "stuck": -3}.get(state, 0)
    # Shoulders begin just under the chin, so the crop is face-first.
    shoulders = (
        f'<path d="M27 99v-8c0-10.8 7.6-19.6 17.2-21.4h11.6C65.4 71.4 73 80.2 73 91v8Z" fill="{HOODIE}"/>'
        f'<path d="M33 99v-8c0-6.6 2.8-12.6 7.4-16.4L42.6 99Z" fill="{HOODIE_HI}" opacity=".45"/>'
        f'<path d="M37 67.4c3.8 5.4 8.2 8 13 8s9.2-2.6 13-8c3.2 1.1 5.8 2.8 7.6 4.8-5 5.8-12.1 9.3-20.6 9.3s-15.6-3.5-20.6-9.3c1.8-2 4.4-3.7 7.6-4.8Z" fill="{HOOD}"/>'
        f'<circle cx="50" cy="84" r="6" fill="{HOOD}"/>'
        f'<text x="50" y="87.4" font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif"'
        f' font-size="8.2" font-weight="700" fill="{WHITE}" text-anchor="middle">P.</text>')
    # Order matters: the thought bubble and the sign sit behind Py, the book
    # and the laptop are held in front.
    behind = prop if state in ("thinking", "approval", "complete", "stuck") else ""
    front = prop if state in ("reading", "working") else ""
    return svg(f'  {behind}\n  <g>{shoulders}</g>\n  {head(eye, mouth, brow, tilt=tilt)}\n  {front}',
               view=BUST_VIEW)


def main() -> int:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
