"""Encode four-colour frames with two resident 256-pattern books.

Each line of a picture picks one book through LCDC, so a picture can draw
on 512 patterns. During vertical blank the player patches slots that the
picture on screen does not use, ready for the pictures that follow. Credits
stay ordinary tile cards and are left out of the scroll score.
"""

import glob
import struct
import sys
from collections import Counter

import numpy as np
from PIL import Image

SCREEN_W = 160
SCREEN_H = 144
CELLS = 10
BPV = 20
MAX_RECORD = 2560
PERSIST_CAP = 8
FLICKER_COST = 8
CANDIDATES = 96
HORIZON = 6
# A miss of two or more shades costs this many one-shade misses.
FAR_STEP = 2
# A changed cell keeps its previous pattern when that costs at most this
# much more than the nearest one, so motion does not reshuffle the picture.
KEEP_SLACK = 2
FILLER_COST = 16 * FAR_STEP + 1
LINE_BYTES = 1 + CELLS
PICTURE_BYTES = SCREEN_H * LINE_BYTES
PATCH_BYTES = 6
MAX_UPDATES = (MAX_RECORD - 3 - PICTURE_BYTES) // PATCH_BYTES
# LCDC for a line drawn from book A (map $9800, tiles 0-63) or book B
# (map $9C00, tiles 64-127), and where each book's tiles start in VRAM.
LCDC_OF = (0x91, 0x99)
BOOK_BASE = (0x8000, 0x8400)
GB_FPS = (1024.0 * 1024.0 * 4.0) / 70224.0

def shade_of(luma):
    if luma >= 220:
        return 0
    if luma >= 160:
        return 1
    if luma >= 80:
        return 2
    return 3


def _shade_gap_table():
    """For every pair of 4 packed shades, how many differ by one step and
    how many differ by two or more. One step is the next grey; two steps
    jumps across a grey and reads as a different shape."""
    near = bytearray(65536)
    far = bytearray(65536)
    for left in range(256):
        for right in range(256):
            near_count = 0
            far_count = 0
            for shift in (0, 2, 4, 6):
                gap = abs(((left >> shift) & 3) - ((right >> shift) & 3))
                near_count += gap == 1
                far_count += gap >= 2
            index = (left << 8) | right
            near[index] = near_count
            far[index] = far_count
    return bytes(near), bytes(far)


SHADE_NEAR, SHADE_FAR = _shade_gap_table()
SHADE_COST = bytes(near + FAR_STEP * far for near, far in zip(SHADE_NEAR, SHADE_FAR))


def shade_cost(left, right):
    """Shade-step cost of showing right where left belongs. Two steps cost
    FAR_STEP, matching what the slot allocator minimises."""
    return (
        SHADE_COST[((left >> 16) & 0xFF00) | ((right >> 24) & 255)]
        + SHADE_COST[((left >> 8) & 0xFF00) | ((right >> 16) & 255)]
        + SHADE_COST[(left & 0xFF00) | ((right >> 8) & 255)]
        + SHADE_COST[((left << 8) & 0xFF00) | (right & 255)]
    )


def shade_gap(left, right):
    near = 0
    far = 0
    for shift in (0, 8, 16, 24):
        index = (((left >> shift) & 255) << 8) | ((right >> shift) & 255)
        near += SHADE_NEAR[index]
        far += SHADE_FAR[index]
    return near, far


def pattern_bytes(pattern):
    shades = [(pattern >> (30 - 2 * index)) & 3 for index in range(16)]

    def pair(group):
        low = 0
        high = 0
        for index, shade in enumerate(group):
            bit = 7 - index
            low |= (shade & 1) << bit
            high |= ((shade >> 1) & 1) << bit
        return low, high

    left_low, left_high = pair(shades[:8])
    right_low, right_high = pair(shades[8:])
    return bytes((left_low, left_high, right_low, right_high))


def write_include(path, slots):
    lines = []
    for half in (0, 1):
        for pattern in slots:
            if pattern > 0xFFFFFFFF:
                pattern = 0xFFFFFFFF
            shades = [(pattern >> (30 - 2 * index)) & 3 for index in range(16)]
            group = shades[half * 8:(half + 1) * 8]
            low = 0
            high = 0
            for index, shade in enumerate(group):
                bit = 7 - index
                low |= (shade & 1) << bit
                high |= ((shade >> 1) & 1) << bit
            lines.append("  db $%02x, $%02x" % (low, high))
    with open(path, "w", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


class Book:
    def __init__(self, patterns):
        self.slots = []
        self.where = {}
        for pattern in patterns:
            if pattern in self.where or len(self.slots) == 256:
                continue
            self.where[pattern] = len(self.slots)
            self.slots.append(pattern)
        filler = 0x100000000
        while len(self.slots) < 256:
            self.slots.append(filler)
            filler += 1

    def clone(self):
        fresh = Book.__new__(Book)
        fresh.slots = list(self.slots)
        fresh.where = dict(self.where)
        return fresh

    def apply(self, writes):
        for slot, pattern in writes:
            if pattern > 0xFFFFFFFF:
                pattern = 0xFFFFFFFF
            old = self.slots[slot]
            if old in self.where and self.where[old] == slot:
                del self.where[old]
            self.slots[slot] = pattern
            if pattern <= 0xFFFFFFFF:
                self.where[pattern] = slot


def top_patterns(counter, fillers):
    chosen = []
    seen = set()
    for pattern, _count in counter.most_common(256):
        chosen.append(pattern)
        seen.add(pattern)
    if len(chosen) < 256:
        for pattern, _count in fillers:
            if pattern in seen:
                continue
            chosen.append(pattern)
            seen.add(pattern)
            if len(chosen) == 256:
                break
    return chosen


def load_cards(path):
    with open(path, "rb") as handle:
        count = struct.unpack("<H", handle.read(2))[0]
        shades = []
        maps = []
        for _card in range(count):
            shades.append(handle.read(SCREEN_W * SCREEN_H))
            maps.append(handle.read(18 * 20))
    return shades, maps


def load_frames(frame_dir, card_shades):
    paths = sorted(glob.glob(frame_dir + "/*.tga"))
    if not paths:
        sys.exit("no frames in " + frame_dir)
    stored = []
    for index, path in enumerate(paths):
        image = Image.open(path).convert("RGB")
        pixels = image.load()
        shades = bytearray(SCREEN_W * SCREEN_H)
        cursor = 0
        for y in range(SCREEN_H):
            for x in range(SCREEN_W):
                red, green, blue = pixels[x, y]
                shades[cursor] = shade_of((red * 77 + green * 150 + blue * 29) >> 8)
                cursor += 1
        card = -1
        blob = bytes(shades)
        for card_index, reference in enumerate(card_shades):
            if blob == reference:
                card = card_index
                break
        stored.append((shades, card))
        if index % 200 == 0:
            print("loaded", index, flush=True)
    # A shade that disagrees with both neighbours, while those neighbours
    # agree, is a one-frame sparkle from the compressed source. Settle it
    # so the picture does not blink between matching frames.
    settled = 0
    for index in range(1, len(stored) - 1):
        shades, card = stored[index]
        if card >= 0:
            continue
        previous, previous_card = stored[index - 1]
        following, following_card = stored[index + 1]
        if previous_card >= 0 or following_card >= 0:
            continue
        current = np.frombuffer(shades, dtype=np.uint8)
        before = np.frombuffer(previous, dtype=np.uint8)
        after = np.frombuffer(following, dtype=np.uint8)
        blink = (current != before) & (after == before)
        settled += int(blink.sum())
        current[blink] = before[blink]
    print("settled", settled, "single-frame shades", flush=True)
    frames = []
    total = Counter()
    for shades, card in stored:
        patterns = []
        counter = Counter()
        cursor = 0
        for _y in range(SCREEN_H):
            for _cell in range(CELLS):
                packed = 0
                for _x in range(16):
                    packed = (packed << 2) | shades[cursor]
                    cursor += 1
                patterns.append(packed)
                counter[packed] += 1
        frames.append({
            "patterns": patterns,
            "counter": counter,
            "card": card,
        })
        if card < 0:
            total.update(counter)
    return frames, total


def frame_lengths(count, rate):
    lengths = []
    tracking = 0.0
    step = GB_FPS / rate
    for _frame in range(count):
        tracking += step
        length = int(tracking)
        tracking -= length
        if length <= 0:
            sys.exit("frame length collapsed; rate %s is too high" % rate)
        lengths.append(length)
    return lengths


def align(blob):
    rom = 0x4000 + len(blob)
    off = rom & 0x3FFF
    if off >= 0x3600:
        blob.extend(b"\xff" * (0x4000 - off))


def visible(pattern):
    if pattern > 0xFFFFFFFF:
        return 0xFFFFFFFF
    return pattern


def _shades(patterns):
    shifts = (30 - 2 * np.arange(16)).astype(np.uint32)
    return ((patterns[:, None] >> shifts) & np.uint32(3)).astype(np.int16)


def _step_cost(delta):
    return np.where(delta >= 2, FAR_STEP, delta).sum(axis=-1).astype(np.int32)


def cost_gaps(left, right):
    """Shade-step cost between every left and right pattern."""
    delta = np.abs(_shades(left)[:, None, :] - _shades(right)[None, :, :])
    return _step_cost(delta)


def cost_paired(left, right):
    delta = np.abs(_shades(left) - _shades(right))
    return _step_cost(delta)


def book_gaps(book, patterns):
    """Cost from each pattern to every slot of book. Unused slots never win."""
    slots = np.array([visible(pattern) for pattern in book.slots], dtype=np.uint32)
    gaps = cost_gaps(patterns, slots)
    filler = np.array([pattern > 0xFFFFFFFF for pattern in book.slots])
    gaps[:, filler] = FILLER_COST
    return gaps


def render(sources, books, previous):
    """Choose a book for every line and a slot for every cell.

    The nearest pattern is the one with the least shade-step cost, so a
    miss of two shades is avoided before a miss of one. A cell whose source
    is unchanged keeps what it showed, unless its exact pattern is now
    resident. A changed cell keeps its previous pattern when that is nearly
    as close as the best one. Each line takes the book that costs less,
    counting a visible change in an unchanged cell as flicker.
    """
    array = np.asarray(sources, dtype=np.uint32)
    uniq, inverse = np.unique(array, return_inverse=True)
    nearest = []
    for book in books:
        gaps = book_gaps(book, uniq)
        best = gaps.argmin(axis=1)
        nearest.append((best[inverse].tolist(),
                        gaps[np.arange(len(uniq)), best][inverse].tolist()))
    out = bytearray(PICTURE_BYTES)
    shown = [0] * (SCREEN_H * CELLS)
    sides = [0] * SCREEN_H
    used = (set(), set())
    exact = 0
    for y in range(SCREEN_H):
        start = y * CELLS
        options = []
        for side, book in enumerate(books):
            slots = []
            cost = 0
            hits = 0
            near_slot, near_cost = nearest[side]
            for cursor in range(start, start + CELLS):
                pattern = sources[cursor]
                slot = near_slot[cursor]
                gap = near_cost[cursor]
                if gap == 0:
                    hits += 1
                elif previous is not None:
                    old = previous["shown"][cursor]
                    still = previous["sources"][cursor] == pattern
                    held = book.where.get(old)
                    if held is not None:
                        old_gap = shade_cost(pattern, old)
                        if still or old_gap <= gap + KEEP_SLACK:
                            slot = held
                            gap = old_gap
                    if still and visible(book.slots[slot]) != old:
                        gap += FLICKER_COST
                cost += gap
                slots.append(slot)
            options.append((cost, slots, hits))
        side = previous["sides"][y] if previous is not None else 0
        if options[1 - side][0] < options[side][0]:
            side = 1 - side
        _cost, slots, hits = options[side]
        exact += hits
        sides[y] = side
        line = y * LINE_BYTES
        out[line] = LCDC_OF[side]
        book = books[side]
        for cell, slot in enumerate(slots):
            out[line + 1 + cell] = (slot - y) & 255
            shown[start + cell] = visible(book.slots[slot])
            used[side].add(slot)
    return bytes(out), exact, {"sources": sources, "shown": shown, "sides": sides}, used


def cell_weights(frames):
    """Weigh each cell by how long the source keeps it. A lasting cell shows
    its error for longer; a passing one hides it in the motion."""
    arrays = [np.array(frame["patterns"], dtype=np.uint32) for frame in frames]
    weights = [None] * len(frames)
    persist = np.zeros(SCREEN_H * CELLS, dtype=np.int32)
    for index in range(len(frames) - 1, -1, -1):
        if frames[index]["card"] >= 0:
            persist = np.zeros_like(persist)
            continue
        if index + 1 < len(frames) and frames[index + 1]["card"] < 0:
            persist = np.where(arrays[index] == arrays[index + 1], persist + 1, 0)
        else:
            persist = np.zeros_like(persist)
        weights[index] = (1 + np.minimum(persist, PERSIST_CAP)).astype(np.float64)
    return arrays, weights


def improve_by_gain(book, protected, current, displayed, upcoming, weight, budget):
    """Greedily replace slots where that removes the most shade error.

    The cells are those of several upcoming pictures that are expected to
    draw from this book, so a pattern they share outranks one that only the
    next picture uses. Protected slots are on screen and cannot change.
    Returns (net gain, slot, pattern) per step, applied to book in order.
    """
    steps = []
    if budget <= 0 or not len(upcoming) or protected.all():
        return steps
    slots = np.array([visible(pattern) for pattern in book.slots], dtype=np.uint32)
    filler = np.array([pattern > 0xFFFFFFFF for pattern in book.slots])
    locked = upcoming == current
    keep = np.where(protected, np.inf, 0.0)
    uniq, inverse = np.unique(upcoming, return_inverse=True)
    free = ~locked
    free_weight = np.bincount(inverse[free], weights=weight[free], minlength=len(uniq))
    locked_cost = weight * cost_paired(upcoming, displayed)
    locked_gain = np.bincount(inverse[locked], weights=locked_cost[locked], minlength=len(uniq))
    gaps = cost_gaps(uniq, slots)
    gaps[:, filler] = FILLER_COST
    resident = np.array([int(pattern) in book.where for pattern in uniq])
    candidates = np.nonzero(~resident)[0]
    if not len(candidates):
        return steps
    rows = np.arange(len(uniq))
    nearest = gaps.min(axis=1)
    rough = nearest[candidates] * free_weight[candidates] + locked_gain[candidates]
    candidates = candidates[np.argsort(-rough)[:CANDIDATES]]
    reach = cost_gaps(uniq[candidates], uniq)
    taken = np.zeros(len(candidates), dtype=bool)
    limit = 16 * FAR_STEP
    for _step in range(budget):
        part = np.argpartition(gaps, 1, axis=1)
        best = part[:, 0]
        first = gaps[rows, best]
        second = np.minimum(gaps[rows, part[:, 1]], limit)
        loss = np.bincount(best, weights=free_weight * (second - first), minlength=256) + keep
        benefit = ((np.maximum(0, first[None, :] - reach) * free_weight[None, :]).sum(axis=1)
                   + locked_gain[candidates])
        benefit[taken] = -1
        pick = int(np.argmax(benefit))
        victim = int(np.argmin(loss))
        net = benefit[pick] - loss[victim]
        if not net > 0:
            break
        pattern = int(uniq[candidates[pick]])
        book.apply([(victim, pattern)])
        steps.append((float(net), victim, pattern))
        gaps[:, victim] = reach[pick]
        taken[pick] = True
    return steps


def coming_view(frames, arrays, weights, books, state, index):
    """Stack the next few scroll pictures behind the one on screen, and
    guess which book each of their lines will draw from.

    A line whose source is unchanged stays on its book. Any other line goes
    to the book that is closer now. The stack stops at a tile card.
    """
    chosen = []
    ahead = index + 1
    while (ahead < len(frames) and len(chosen) < HORIZON
           and frames[ahead]["card"] < 0):
        chosen.append(ahead)
        ahead += 1
    if not chosen:
        return None
    current = arrays[index]
    displayed = np.asarray(state["shown"], dtype=np.uint32)
    sides_now = np.asarray(state["sides"], dtype=np.int8)
    upcoming = np.concatenate([arrays[item] for item in chosen])
    uniq, inverse = np.unique(upcoming, return_inverse=True)
    near = [book_gaps(book, uniq).min(axis=1)[inverse] for book in books]
    cost = np.stack(near).reshape(2, len(chosen), SCREEN_H, CELLS).sum(axis=3)
    sides = (cost[1] < cost[0]).astype(np.int8)
    for row, item in enumerate(chosen):
        still = (arrays[item] == current).reshape(SCREEN_H, CELLS).all(axis=1)
        sides[row][still] = sides_now[still]
    count = len(chosen)
    return (
        np.tile(current, count),
        np.tile(displayed, count),
        upcoming,
        np.concatenate([weights[item] for item in chosen]),
        np.repeat(sides.reshape(-1), CELLS),
    )


def allocate(books, used, view, budget):
    """Spend up to budget patches on the two books, best gain first.

    Each book is planned on its own clone. The two step lists are then
    merged by gain, and only the chosen prefixes are applied.
    """
    if view is None or budget <= 0:
        return []
    current, displayed, upcoming, weight, sides = view
    plans = []
    for side, book in enumerate(books):
        mask = sides == side
        protected = np.zeros(256, dtype=bool)
        protected[list(used[side])] = True
        plans.append(improve_by_gain(
            book.clone(), protected, current[mask], displayed[mask],
            upcoming[mask], weight[mask], budget))
    taken = [0, 0]
    while taken[0] + taken[1] < budget:
        heads = [plans[side][taken[side]][0] if taken[side] < len(plans[side]) else 0
                 for side in (0, 1)]
        side = 0 if heads[0] >= heads[1] else 1
        if heads[side] <= 0:
            break
        taken[side] += 1
    writes = []
    for side in (0, 1):
        for _net, slot, pattern in plans[side][:taken[side]]:
            books[side].apply([(slot, pattern)])
            writes.append((side, slot, pattern))
    return writes


def opening_books(frames, fillers):
    """Book A holds the first picture. Book B holds what the next pictures
    need beyond that, then the commonest patterns of the whole stream."""
    first = Book(top_patterns(frames[0]["counter"], fillers))
    ahead = Counter()
    for frame in frames[1:1 + 4 * HORIZON]:
        if frame["card"] >= 0:
            break
        ahead.update(frame["counter"])
    extra = [pattern for pattern, _count in ahead.most_common() if pattern not in first.where]
    extra += [pattern for pattern, _count in fillers if pattern not in first.where]
    return first, Book(extra)


def build_plans(frames, lengths, fillers):
    books = opening_books(frames, fillers)
    initial = (list(books[0].slots), list(books[1].slots))
    arrays, weights = cell_weights(frames)
    plans = []
    totals = {
        "exact": 0, "scored": 0, "flicker": 0, "stable": 0, "fixed": 0,
        "near": 0, "far": 0, "lines_b": 0, "patches": 0,
    }
    state = None
    for index, frame in enumerate(frames):
        if frame["card"] >= 0:
            plans.append({
                "kind": "card",
                "card": frame["card"],
                "length": lengths[index],
            })
            state = None
            continue
        sources = frame["patterns"]
        scy, got, fresh, used = render(sources, books, state)
        totals["exact"] += got
        totals["scored"] += SCREEN_H * CELLS
        totals["lines_b"] += sum(fresh["sides"])
        for cursor, (src, new_show) in enumerate(zip(sources, fresh["shown"])):
            near, far = shade_gap(src, new_show)
            totals["near"] += near
            totals["far"] += far
            if state is not None and state["sources"][cursor] == src:
                totals["stable"] += 1
                if state["shown"][cursor] != new_show:
                    if new_show == src:
                        totals["fixed"] += 1
                    else:
                        totals["flicker"] += 1
        state = fresh
        updates = []
        # The opening picture waits for Start with no patching vblanks.
        if index > 0:
            budget = min(lengths[index] * BPV, MAX_UPDATES)
            view = coming_view(frames, arrays, weights, books, state, index)
            updates = allocate(books, used, view, budget)
        totals["patches"] += len(updates)
        plans.append({
            "kind": "scroll",
            "updates": updates,
            "scy": scy,
            "length": lengths[index],
            "first": index == 0,
        })
    return plans, initial, totals


def merge_plans(plans):
    merged = []
    for plan in plans:
        if plan["kind"] == "card":
            if (merged and merged[-1]["kind"] == "card"
                    and merged[-1]["card"] == plan["card"]
                    and merged[-1]["length"] + plan["length"] <= 255):
                merged[-1]["length"] += plan["length"]
            else:
                merged.append(dict(plan))
            continue
        previous = merged[-1] if merged else None
        updates = plan["updates"]
        if (previous and previous["kind"] == "scroll" and not previous["first"]
                and previous["scy"] == plan["scy"]
                and previous["length"] + plan["length"] <= 254
                and len(previous["updates"]) + len(updates) <= MAX_UPDATES):
            previous["length"] += plan["length"]
            previous["updates"].extend(updates)
            continue
        merged.append({
            "kind": "scroll",
            "updates": list(updates),
            "scy": plan["scy"],
            "length": plan["length"],
            "first": plan["first"],
        })
    return merged


def emit(plans, maps):
    blob = bytearray()
    for plan in plans:
        align(blob)
        if plan["kind"] == "card":
            blob.append(255)
            blob.append(plan["length"])
            blob.extend(maps[plan["card"]])
            continue
        updates = plan["updates"]
        record = bytearray()
        record.append(plan["length"] if plan["length"] <= 254 else 254)
        record.append(0)
        record.append(len(updates))
        for side, slot, pattern in updates:
            address = BOOK_BASE[side] + 2 * slot
            record.append(address & 255)
            record.append(address >> 8)
            record.extend(pattern_bytes(pattern))
        record.extend(plan["scy"])
        if len(record) > MAX_RECORD:
            sys.exit("scroll record is %d bytes" % len(record))
        if plan["length"] > 254:
            sys.exit("scroll repeat %d does not fit one record" % plan["length"])
        blob.extend(record)
    align(blob)
    blob.append(0)
    return blob


def check_budgets(plans):
    """Every patch has to land in one of the picture's own vblanks, in a
    slot the picture does not draw from."""
    for index, plan in enumerate(plans):
        if plan["kind"] != "scroll":
            continue
        cap = 0 if plan["first"] else min(plan["length"] * BPV, MAX_UPDATES)
        if len(plan["updates"]) > cap:
            sys.exit("record %d has %d updates and allows %d"
                     % (index, len(plan["updates"]), cap))
        drawn = set()
        for y in range(SCREEN_H):
            line = plan["scy"][y * LINE_BYTES:(y + 1) * LINE_BYTES]
            side = LCDC_OF.index(line[0])
            for value in line[1:]:
                drawn.add((side, (value + y) & 255))
        for side, slot, _pattern in plan["updates"]:
            if (side, slot) in drawn:
                sys.exit("record %d patches slot %d of book %d while drawing it"
                         % (index, slot, side))


def main():
    if len(sys.argv) != 7:
        sys.exit(
            "usage: pixel_encode.py frames_dir cards.bin patterns.inc "
            "bookb.inc video.bin rate"
        )
    frame_dir, cards_path, patterns_path, bookb_path, video_path, rate_text = sys.argv[1:]
    rate = float(rate_text.split("/")[0])
    if "/" in rate_text:
        rate /= float(rate_text.split("/")[1])
    card_shades, card_maps = load_cards(cards_path)
    frames, fillers = load_frames(frame_dir, card_shades)
    lengths = frame_lengths(len(frames), rate)
    plans, (book_a, book_b), totals = build_plans(frames, lengths, fillers.most_common())
    merged = merge_plans(plans)
    check_budgets(merged)
    blob = emit(merged, card_maps)
    with open(video_path, "wb") as handle:
        handle.write(blob)
    write_include(patterns_path, book_a)
    write_include(bookb_path, book_b)
    scrolls = sum(1 for plan in merged if plan["kind"] == "scroll")
    cards = sum(1 for plan in merged if plan["kind"] == "card")
    print(
        "Wrote %d pictures (%d scroll, %d tile cards), %d patches, %d bytes"
        % (scrolls + cards, scrolls, cards, totals["patches"], len(blob))
    )
    scored = totals["scored"]
    if scored:
        pixels = scored * 16
        matched = pixels - totals["near"] - totals["far"]
        print(
            "Exact 16-pixel cells %d / %d (%.2f%%), lines on book B %.1f%%"
            % (totals["exact"], scored, 100.0 * totals["exact"] / scored,
               100.0 * totals["lines_b"] * CELLS / scored)
        )
        print(
            "Matching shades %d / %d (%.2f%%); one step away %d, two or more %d"
            % (matched, pixels, 100.0 * matched / pixels,
               totals["near"], totals["far"])
        )
    if totals["stable"]:
        print(
            "Unchanged cells that switched to another wrong pattern %d / %d (%.2f%%),"
            " corrected to the source %d"
            % (totals["flicker"], totals["stable"],
               100.0 * totals["flicker"] / totals["stable"], totals["fixed"])
        )


if __name__ == "__main__":
    main()
