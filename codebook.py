"""Build the 256-pattern book for one pixel-art video.

Each pattern is one 16-pixel strip, which is the unit the player can select.
A glyph image, when given, is sliced on that same grid and those strips are
reserved first, so tile-sized text is stored exactly. The remaining entries
are the most common strips in the video. Anything left over is later
replaced by the nearest stored strip.
"""

import glob
import sys
from collections import Counter
from PIL import Image


def shade_of(luma):
    if luma >= 220:
        return 0
    if luma >= 160:
        return 1
    if luma >= 80:
        return 2
    return 3


def luma_of(rgb):
    red, green, blue = rgb
    return (red * 77 + green * 150 + blue * 29) >> 8


def row_bytes(pixels):
    low = 0
    high = 0
    for index, shade in enumerate(pixels):
        bit = 7 - index
        low |= (shade & 1) << bit
        high |= ((shade >> 1) & 1) << bit
    return low, high


def strips_in(path):
    image = Image.open(path).convert("RGB")
    pixels = image.load()
    width, height = image.size
    if width % 16 != 0:
        sys.exit("width of %s must be a multiple of 16" % path)
    found = []
    seen = set()
    for y in range(height):
        shades = [shade_of(luma_of(pixels[x, y])) for x in range(width)]
        for cell in range(width // 16):
            pattern = tuple(shades[cell * 16:(cell + 1) * 16])
            if pattern not in seen:
                seen.add(pattern)
                found.append(pattern)
    return found


def main():
    if len(sys.argv) not in (4, 5):
        sys.exit(
            "usage: codebook.py frames_dir codebook.bin patterns.inc [glyphs]"
        )
    frame_dir, codebook_path, include_path = sys.argv[1:4]
    glyph_path = sys.argv[4] if len(sys.argv) == 5 and sys.argv[4] else None
    frames = sorted(glob.glob(frame_dir + "/*.tga"))
    if not frames:
        sys.exit("no frames in " + frame_dir)

    reserved = strips_in(glyph_path) if glyph_path else []
    if len(reserved) > 256:
        sys.exit(
            "glyph image has %d strips; only 256 patterns fit" % len(reserved)
        )

    frequency = Counter()
    for path in frames:
        image = Image.open(path).convert("RGB")
        pixels = image.load()
        for y in range(144):
            shades = [shade_of(luma_of(pixels[x, y])) for x in range(160)]
            for cell in range(10):
                frequency[tuple(shades[cell * 16:(cell + 1) * 16])] += 1

    chosen = list(reserved)
    reserved_set = set(reserved)
    for pattern, _count in frequency.most_common():
        if len(chosen) == 256:
            break
        if pattern not in reserved_set:
            chosen.append(pattern)
    if len(chosen) < 256:
        flat = (3,) * 16
        while len(chosen) < 256:
            chosen.append(flat)

    covered = sum(frequency[pattern] for pattern in chosen[:256])
    total = sum(frequency.values())
    glyph_cells = sum(frequency[pattern] for pattern in reserved)
    print(
        "reserved %d glyph strips"
        % len(reserved)
    )
    print(
        "codebook %d unique, 256 patterns cover %d/%d (%.2f%%)"
        % (len(frequency), covered, total, 100.0 * covered / total)
    )
    if reserved:
        print(
            "glyph strips account for %d/%d cells"
            % (glyph_cells, total)
        )

    with open(codebook_path, "wb") as handle:
        for pattern in chosen:
            handle.write(bytes(pattern))

    lines = []
    for pattern in chosen:
        low, high = row_bytes(pattern[:8])
        lines.append("  db $%02x, $%02x" % (low, high))
    for pattern in chosen:
        low, high = row_bytes(pattern[8:])
        lines.append("  db $%02x, $%02x" % (low, high))
    with open(include_path, "w", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
