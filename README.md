# GBVideoPlayer2 for DMG

A fork of [LIJI32's GBVideoPlayer2](https://github.com/LIJI32/GBVideoPlayer2) that plays silent video on the original Game Boy.

The colour player rewrites the vertical scroll on every eighth pixel. That loop only meets the LCD if the CPU is in Game Boy Color double-speed mode, and the 528-colour figure comes from the eight background palettes plus blending on the LCD. Neither exists on the DMG. Dropping the audio track frees the vertical blank and a few instructions per line. It does not halve the pixel period. This player therefore uses a different picture format, aimed at keeping motion instead of pretending the colour image still fits.

## Picture

- 160×144 LCD pixels, 4 shades of grey
- Logical resolution 40×144. Each logical pixel is 4 LCD pixels wide, chosen from a complete codebook of 256 sixteen-pixel patterns
- Source frames are held for as many hardware frames as needed, so a 30 fps source stays near 30 pictures per second. The scroll trick itself runs at the full 59.7 Hz LCD rate
- No audio
- Similar pictures can be held instead of stored again. `QUALITY=0` only merges near-duplicates. The default `4` allows a small amount of hold. Higher values make smaller ROMs and choppier motion
- MBC5, up to 8 MB. A stream in which every frame is unique is about 43 KB/s at 30 fps, roughly three minutes in 8 MB

## Requirements

Hardware needs an MBC5 flash cart. Cartridges that only accept a 4 MB image cannot hold a single 8 MB ROM.

On an emulator, use a cycle-accurate DMG core. [SameBoy](https://sameboy.github.io) and [BGB](http://bgb.bircd.org) are the ones this timing was aimed at. Each visible line wakes the CPU from the mode-2 STAT interrupt and writes the ten scroll values during that line's pixel fetch. An inaccurate emulator will shear it.

Building needs GNU Make, a C11 compiler, [RGBDS](https://rgbds.gbdev.io/) 1.0 or newer, and FFmpeg (`ffmpeg` and `ffprobe` on `PATH`).

## Building

```
make SOURCE=/path/to/video.mp4
```

The ROM is written to `output/video/video.gb` when the source file is named `video.mp4`.

```
make SOURCE=/path/to/video.mp4 QUALITY=8
```

`QUALITY` is a non-negative integer. `0` stores a new picture whenever the frame changes. Higher values reuse the previous picture for longer.

## Format

Bank 0 is the player. Picture data starts at bank 1. A record is never split across a bank.

A scroll picture never crosses a bank. The player starts one only below `$7600`:

| Bytes | Meaning |
| --- | --- |
| 1 | How many hardware frames to show, from 1 to 254. `0` ends the stream. Playback returns to the first picture and waits for Start. `255` is not a repeat |
| 1 | Reserved, `0` |
| 1 | How many pattern patches follow |
| 6 each | VRAM address of the pattern's left half, then the left and right pixel bytes of that 16-pixel pattern |
| 1584 | 144 lines. Each line is an LCDC byte, `$91` for book A or `$99` for book B, then 10 scroll values |

Tiles 0–63 hold book A and tiles 64–127 hold book B, 256 patterns each. Every line picks its book, so one picture can use up to 512 patterns, although the ten cells of a line share a book. Patches are written during vertical blank, up to 20 per blank, and only into slots that the picture on screen does not draw. The encoder chooses the nearest resident pattern by shade distance, so a cell it cannot match exactly is off by one grey before it is off by two.

The `QUALITY` encoder used without `PIXEL=1` writes 10 scroll values per line and no patches; the player is assembled for whichever stream the build makes.

A full-screen tile card can stand anywhere in the stream. It is 362 bytes and uses tiles from 128 upwards:

| Bytes | Meaning |
| --- | --- |
| 1 | `255`, the tile-card mark |
| 1 | How many hardware frames to show, from 1 to 255 |
| 360 | 18 rows of 20 tile ids, drawn on the map at `$9C00` |

A scroll value of `pattern - line` selects one of the 256 baked 16-pixel patterns for that cell. The pattern index is four 2-bit shades, leftmost pixel in the top bits. Shade 0 is the lightest grey.

## Timing

`TUNE_NOPS` in `video.asm` places the scroll writes on the pixel fetch. Each nop slips them four dots later. If a build looks shifted or sliced on hardware, that constant is the adjustment. In the `PIXEL=1` build the LCDC write takes five of those cycles, during mode 2, so the scroll writes stay where they were.

## Licence

MIT, the same terms as GBVideoPlayer2. Copyright for the original player and encoder remains with Lior Halphon. See `LICENSE`.
