"""Build the credit font and the tilemaps for full-screen text cards.

Each PNG in the cards directory is one 160x144 screen. Identical 8x8 cells
share a tile, starting at tile 128. Tiles 0-127 are the two scroll books.
The encoder matches a source frame to these screens wherever it occurs.
"""

import glob
import os
import struct
import sys
from PIL import Image

SCREEN_W = 160
SCREEN_H = 144
TILE = 8
MAP_W = 20
MAP_H = 18
FIRST_TILE = 128


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


def shades_of(path):
    image = Image.open(path).convert("RGB")
    if image.size != (SCREEN_W, SCREEN_H):
        sys.exit("%s is %dx%d, expected 160x144" % (path, *image.size))
    pixels = image.load()
    shades = []
    for y in range(SCREEN_H):
        for x in range(SCREEN_W):
            shades.append(shade_of(luma_of(pixels[x, y])))
    return shades


def cell_at(shades, tile_x, tile_y):
    cell = []
    for y in range(tile_y * TILE, tile_y * TILE + TILE):
        start = y * SCREEN_W + tile_x * TILE
        cell.extend(shades[start:start + TILE])
    return tuple(cell)


def row_bytes(row):
    low = 0
    high = 0
    for index, shade in enumerate(row):
        bit = 7 - index
        low |= (shade & 1) << bit
        high |= ((shade >> 1) & 1) << bit
    return low, high


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: cards.py cards_dir font.inc cards.bin")
    cards_dir, font_path, bin_path = sys.argv[1:4]
    paths = sorted(
        glob.glob(os.path.join(cards_dir, "*.png"))
        + glob.glob(os.path.join(cards_dir, "*.tga"))
    )
    screens = [shades_of(path) for path in paths]

    tiles = []
    index = {}

    def intern(cell):
        if cell not in index:
            if len(tiles) + FIRST_TILE > 255:
                sys.exit("credit font needs more tiles than 128-255")
            index[cell] = len(tiles)
            tiles.append(cell)
        return index[cell]

    maps = []
    for shades in screens:
        mapping = []
        for tile_y in range(MAP_H):
            for tile_x in range(MAP_W):
                mapping.append(FIRST_TILE + intern(cell_at(shades, tile_x, tile_y)))
        maps.append(bytes(mapping))

    lines = []
    for cell in tiles:
        for row in range(TILE):
            low, high = row_bytes(cell[row * TILE:(row + 1) * TILE])
            lines.append("  db $%02x, $%02x" % (low, high))
    with open(font_path, "w", newline="\n") as handle:
        handle.write("\n".join(lines) + ("\n" if lines else ""))

    with open(bin_path, "wb") as handle:
        handle.write(struct.pack("<H", len(screens)))
        for shades, mapping in zip(screens, maps):
            handle.write(bytes(shades))
            handle.write(mapping)

    print(
        "credit cards %d, font tiles %d (%d bytes)"
        % (len(screens), len(tiles), len(tiles) * 16)
    )


if __name__ == "__main__":
    main()
