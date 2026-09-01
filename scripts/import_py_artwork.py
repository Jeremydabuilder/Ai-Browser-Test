"""Turn supplied Py artwork into the fourteen asset files - by cropping it.

    python scripts/import_py_artwork.py FILE_OR_DIR [FILE ...] [options]

This never draws anything. Every pixel it writes came out of a file you gave
it. What it does is the mechanical part of the hand-off:

* knocks a flat background out to transparency, if the artwork has one
* trims the transparent margin tight to the character
* splits a contact sheet into its individual figures
* cuts a head-and-shoulders panel crop out of each full-body figure
* writes ``<state>-full`` and ``<state>-panel``, plus ``@2x`` where it can

Ways to call it:

    # one file per state, named for the state
    python scripts/import_py_artwork.py ~/py-art/

    # one sheet with the figures in a row, left to right
    python scripts/import_py_artwork.py sheet.png --states idle,reading,thinking

    # a single image, used for every state
    python scripts/import_py_artwork.py py.png --state idle

Nothing is overwritten without --force, and --dry-run reports what it would do
without writing. Run it again with a different --panel-* value if a bust comes
out framed wrong; the numbers it prints are the ones to adjust.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.mascot import ALL_STATES, ASSET_DIR, Variant  # noqa: E402

#: Formats Qt reads, in the order a file is looked for when a state is named
#: rather than pointed at.
READABLE = (".png", ".webp", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")

#: A pixel is "content" above this alpha. Not 0: artwork exported from a
#: painting tool carries a haze of nearly-transparent pixels around the figure,
#: and trimming to alpha > 0 leaves a margin of invisible nothing.
ALPHA_FLOOR = 12


def _load(path: str) -> QImage:
    image = QImage(path)
    if image.isNull():
        raise SystemExit(f"cannot read {path} - Qt does not recognise the format")
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def _pixels(image: QImage):
    """The raw ARGB32 bytes, the row stride, and the size.

    Working on the buffer rather than through pixel() matters: a 2000x2000
    sheet is four million pixels, and four million Python-level calls is the
    difference between a second and several minutes.
    """
    return memoryview(image.bits()), image.bytesPerLine(), image.width(), image.height()


def knock_out_background(image: QImage, tolerance: int = 26) -> tuple[QImage, bool]:
    """Make a flat background transparent, from the edges inward.

    Flood-filled from the border rather than matched globally, so a white
    highlight in an eye or a cream muzzle survives - only background connected
    to the edge of the canvas is removed. Artwork that already has transparent
    edges is returned untouched.
    """
    image = QImage(image)  # detach; we are about to write into it
    data, stride, width, height = _pixels(image)
    if not width or not height:
        return image, False

    def at(x: int, y: int) -> tuple[int, int, int, int]:
        i = y * stride + x * 4
        return data[i + 2], data[i + 1], data[i], data[i + 3]  # R G B A

    corners = [at(0, 0), at(width - 1, 0), at(0, height - 1), at(width - 1, height - 1)]
    if any(corner[3] < 250 for corner in corners):
        return image, False        # already transparent at the edges: leave it
    reds, greens, blues, _ = zip(*corners)
    if max(reds) - min(reds) > tolerance or max(greens) - min(greens) > tolerance \
            or max(blues) - min(blues) > tolerance:
        return image, False        # the corners disagree: not a flat backdrop
    base = (sum(reds) // 4, sum(greens) // 4, sum(blues) // 4)

    seen = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))

    cleared = 0
    while queue:
        x, y = queue.popleft()
        if x < 0 or y < 0 or x >= width or y >= height:
            continue
        index = y * width + x
        if seen[index]:
            continue
        seen[index] = 1
        red, green, blue, _ = at(x, y)
        if (abs(red - base[0]) > tolerance or abs(green - base[1]) > tolerance
                or abs(blue - base[2]) > tolerance):
            continue
        data[y * stride + x * 4 + 3] = 0
        cleared += 1
        queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    return image, cleared > 0


def content_box(image: QImage, box: QRect | None = None) -> QRect:
    """The tightest rectangle holding every pixel above the alpha floor."""
    data, stride, width, height = _pixels(image)
    x0, y0 = (box.left(), box.top()) if box else (0, 0)
    x1 = box.right() if box else width - 1
    y1 = box.bottom() if box else height - 1
    left, top, right, bottom = width, height, -1, -1
    for y in range(max(0, y0), min(height, y1 + 1)):
        row = y * stride
        for x in range(max(0, x0), min(width, x1 + 1)):
            if data[row + x * 4 + 3] > ALPHA_FLOOR:
                if x < left:
                    left = x
                if x > right:
                    right = x
                if y < top:
                    top = y
                bottom = y
    if right < 0:
        return QRect()
    return QRect(left, top, right - left + 1, bottom - top + 1)


def _runs(occupied: list[bool], gap: int) -> list[tuple[int, int]]:
    """Index ranges that hold content, merging gaps shorter than `gap`."""
    runs, start = [], None
    for index, filled in enumerate(occupied):
        if filled and start is None:
            start = index
        elif not filled and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(occupied) - 1))
    merged: list[tuple[int, int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    return merged


def split_figures(image: QImage, gap: int = 12,
                  min_side: int = 24) -> list[QRect]:
    """Cut a contact sheet into its individual figures.

    Rows first, then columns inside each row, because that is how a character
    sheet is laid out and because it keeps a raised arm in one cell from being
    merged with the neighbour it nearly touches.
    """
    data, stride, width, height = _pixels(image)
    row_has = [False] * height
    column_of: list[list[bool]] = []
    for y in range(height):
        row = y * stride
        filled = [data[row + x * 4 + 3] > ALPHA_FLOOR for x in range(width)]
        column_of.append(filled)
        row_has[y] = any(filled)

    boxes: list[QRect] = []
    for top, bottom in _runs(row_has, gap):
        column_has = [False] * width
        for y in range(top, bottom + 1):
            filled = column_of[y]
            for x in range(width):
                if filled[x]:
                    column_has[x] = True
        for left, right in _runs(column_has, gap):
            box = content_box(image, QRect(left, top, right - left + 1,
                                           bottom - top + 1))
            if box.width() >= min_side and box.height() >= min_side:
                boxes.append(box)
    return boxes


def _row_widths(image: QImage, figure: QRect, rows: int) -> list[int]:
    """How wide the artwork is on each of the first `rows` rows of a figure.

    One pass over the buffer rather than a content_box call per row: on a
    2000-pixel-tall drawing that is the difference between a moment and a
    minute.
    """
    data, stride, width, height = _pixels(image)
    widths: list[int] = []
    left_edge = max(0, figure.left())
    right_edge = min(width - 1, figure.right())
    for y in range(figure.top(), min(height, figure.top() + rows)):
        row = y * stride
        first = last = -1
        for x in range(left_edge, right_edge + 1):
            if data[row + x * 4 + 3] > ALPHA_FLOOR:
                if first < 0:
                    first = x
                last = x
        widths.append(0 if first < 0 else last - first + 1)
    return widths


def panel_box(image: QImage, figure: QRect, *, probe: float = 0.55,
              spread: float = 1.95, headroom: float = 0.07,
              shoulder: float = 1.45) -> QRect:
    """A head-and-shoulders crop of a full-body figure.

    Found from the artwork rather than guessed. Walking down from the top, the
    head is the narrow part and the shoulders are where the drawing suddenly
    gets `shoulder` times wider; the crop is the head's own width times
    `spread`, centred on the head and dropped by `headroom` so the ears are not
    against the top edge.

    Taking a fixed fraction of the height instead put the crop on the chest of
    any figure with a wide stance - the probe band swallowed the shoulders and
    "the head" came out as the whole torso. Every number is an option, and the
    box that gets chosen is printed, because no single framing is right for
    every drawing.
    """
    scanned = max(4, int(figure.height() * probe))
    widths = _row_widths(image, figure, scanned)
    if not widths:
        return QRect(figure)

    peak_band = max(2, int(len(widths) * 0.35))
    head_peak = max(widths[:peak_band]) or max(widths)
    neck = len(widths)
    for index in range(peak_band, len(widths)):
        if widths[index] > head_peak * shoulder:
            neck = index
            break

    head = content_box(image, QRect(figure.left(), figure.top(),
                                    figure.width(), max(1, neck)))
    if head.isNull():
        head = figure

    # Clamped against the canvas as well as the figure: a generous --panel-
    # spread used to walk the crop straight off the edge of the image.
    side = max(1, int(head.width() * spread))
    side = min(side, figure.height(), image.width(), image.height())
    centre = head.left() + head.width() // 2
    left = max(0, min(centre - side // 2, image.width() - side))
    top = max(0, min(head.top() - int(side * headroom), image.height() - side))
    return QRect(left, top, side, side)


def _save(image: QImage, path: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"    would write {os.path.basename(path)}  "
              f"{image.width()}x{image.height()}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not image.save(path, "PNG"):
        raise SystemExit(f"could not write {path}")
    print(f"    wrote {os.path.basename(path)}  {image.width()}x{image.height()}")


def _scaled_copy(image: QImage, longest: int) -> QImage:
    """Down to a sensible 1x, keeping the aspect. Never scaled up: enlarging
    supplied artwork is the one way to make it look worse than it is."""
    if max(image.width(), image.height()) <= longest:
        return image
    return image.scaled(longest, longest,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)


def emit(source: QImage, figure: QRect, state: str, out_dir: str, *,
         dry_run: bool, retina: bool, full_longest: int, panel_longest: int,
         panel_opts: dict) -> None:
    full = source.copy(figure)
    box = panel_box(source, figure, **panel_opts)
    panel = source.copy(box)
    print(f"  {state}: figure {figure.width()}x{figure.height()} at "
          f"({figure.left()},{figure.top()})  panel {box.width()}x{box.height()} "
          f"at ({box.left()},{box.top()})")
    for variant, image, longest in ((Variant.FULL, full, full_longest),
                                    (Variant.PANEL, panel, panel_longest)):
        one_x = _scaled_copy(image, longest)
        _save(one_x, os.path.join(out_dir, f"{state}-{variant}.png"), dry_run=dry_run)
        if retina and (image.width() > one_x.width() or image.height() > one_x.height()):
            two_x = _scaled_copy(image, longest * 2)
            _save(two_x, os.path.join(out_dir, f"{state}-{variant}@2x.png"),
                  dry_run=dry_run)


def _states_from(text: str | None) -> list[str]:
    if not text:
        return list(ALL_STATES)
    wanted = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [state for state in wanted if state not in ALL_STATES]
    if unknown:
        raise SystemExit(f"not a Py state: {', '.join(unknown)}\n"
                         f"known states: {', '.join(ALL_STATES)}")
    return wanted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crop supplied Py artwork into the fourteen asset files.")
    parser.add_argument("sources", nargs="+",
                        help="image files, or a directory of files named for states")
    parser.add_argument("--out", default=ASSET_DIR,
                        help=f"where to write (default: {ASSET_DIR})")
    parser.add_argument("--states",
                        help="for a sheet: which states its figures are, left to right")
    parser.add_argument("--state",
                        help="for one image: which single state it is")
    parser.add_argument("--all-states", action="store_true",
                        help="write one supplied image to every state")
    parser.add_argument("--no-split", action="store_true",
                        help="treat each file as one figure, never as a sheet")
    parser.add_argument("--keep-background", action="store_true",
                        help="do not knock a flat background out to transparency")
    parser.add_argument("--tolerance", type=int, default=26,
                        help="how close to the corner colour counts as background")
    parser.add_argument("--full-size", type=int, default=880,
                        help="longest side of the 1x full-body file")
    parser.add_argument("--panel-size", type=int, default=256,
                        help="longest side of the 1x panel file")
    parser.add_argument("--no-retina", action="store_true", help="skip @2x files")
    parser.add_argument("--panel-probe", type=float, default=0.55,
                        help="fraction of the figure's height scanned for the neck")
    parser.add_argument("--panel-shoulder", type=float, default=1.45,
                        help="how much wider than the head the shoulders are")
    parser.add_argument("--panel-spread", type=float, default=1.95,
                        help="crop width as a multiple of the head's width")
    parser.add_argument("--panel-headroom", type=float, default=0.07,
                        help="space above the head, as a fraction of the crop")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen and write nothing")
    args = parser.parse_args()

    app = QApplication.instance() or QApplication([])  # noqa: F841

    files: list[str] = []
    for source in args.sources:
        if os.path.isdir(source):
            files.extend(sorted(
                os.path.join(source, name) for name in os.listdir(source)
                if os.path.splitext(name)[1].lower() in READABLE))
        else:
            files.append(source)
    if not files:
        raise SystemExit("no readable images found")

    if not args.force and not args.dry_run:
        clash = [name for name in os.listdir(args.out)
                 if name.lower().endswith(READABLE)] if os.path.isdir(args.out) else []
        if clash:
            raise SystemExit(
                f"{args.out} already holds artwork ({', '.join(sorted(clash)[:4])}"
                f"{'...' if len(clash) > 4 else ''}).\n"
                "Pass --force to replace it, or --dry-run to see what would happen.")

    panel_opts = dict(probe=args.panel_probe, spread=args.panel_spread,
                      headroom=args.panel_headroom, shoulder=args.panel_shoulder)
    done: dict[str, str] = {}

    for path in files:
        print(f"{path}")
        image = _load(path)
        if not args.keep_background:
            image, removed = knock_out_background(image, args.tolerance)
            print(f"  background: {'knocked out' if removed else 'left as supplied'}")
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        figures = [content_box(image)] if args.no_split else split_figures(image)
        figures = [box for box in figures if not box.isNull()]
        if not figures:
            print("  nothing above the alpha floor - skipped")
            continue
        print(f"  {len(figures)} figure(s) found")

        if args.state:
            states = [args.state]
        elif args.all_states and len(figures) == 1:
            states = list(ALL_STATES)
        elif len(figures) == 1 and stem in ALL_STATES:
            states = [stem]
        elif len(figures) == 1 and stem.split("-")[0] in ALL_STATES:
            states = [stem.split("-")[0]]
        else:
            states = _states_from(args.states)

        if args.all_states and len(figures) == 1:
            for state in states:
                emit(image, figures[0], state, args.out, dry_run=args.dry_run,
                     retina=not args.no_retina, full_longest=args.full_size,
                     panel_longest=args.panel_size, panel_opts=panel_opts)
                done[state] = f"{os.path.basename(path)} (whole image)"
            continue

        if len(figures) != len(states):
            print(f"  ! {len(figures)} figures but {len(states)} states named. "
                  f"Pass --states with {len(figures)} names, or --no-split.")
            if len(figures) > len(states):
                figures = figures[:len(states)]
            else:
                states = states[:len(figures)]
        for box, state in zip(figures, states):
            emit(image, box, state, args.out, dry_run=args.dry_run,
                 retina=not args.no_retina, full_longest=args.full_size,
                 panel_longest=args.panel_size, panel_opts=panel_opts)
            done[state] = (f"{os.path.basename(path)}"
                           + (f" figure at x={box.left()}" if len(figures) > 1 else ""))

    print("\n--- states ---")
    for state in ALL_STATES:
        if state in done:
            print(f"  {state:9s} <- {done[state]}")
        else:
            print(f"  {state:9s} -- not supplied; falls back to "
                  f"{'idle' if 'idle' in done else 'the placeholder'}")
    missing = [state for state in ALL_STATES if state not in done]
    if missing:
        print(f"\n{len(missing)} state(s) not supplied: {', '.join(missing)}")
        print("The browser falls back <state>-<variant> -> <state> -> "
              "idle-<variant> -> idle, so this is fine - Py just wears the same "
              "face for those.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
