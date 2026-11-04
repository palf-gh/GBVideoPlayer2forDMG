"""Encode four-colour frames with two resident 256-pattern books.

The player can show one book and, during vertical blank, patch the other.
A source frame therefore keeps the patterns it is already showing and spends
its blank time installing patterns for the frame that follows. Credits stay
ordinary tile cards and are left out of the scroll score.
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
# Larger than the gain of rewriting the whole horizon, so a pattern the next
# picture still shows is never the slot that gets replaced.
KEEP_SENTINEL = 1 << 24
GB_FPS = (1024.0 * 1024.0 * 4.0) / 70224.0

MISMATCH = bytes(
    sum(((i >> shift) & 3) != 0 for shift in (0, 2, 4, 6)) for i in range(256)
)


def shade_of(luma):
    if luma >= 220:
        return 0
    if luma >= 160:
        return 1
    if luma >= 80:
        return 2
    return 3


def dist(left, right):
    value = left ^ right
    return (
        MISMATCH[value & 255]
        + MISMATCH[(value >> 8) & 255]
        + MISMATCH[(value >> 16) & 255]
        + MISMATCH[(value >> 24) & 255]
    )


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
        self.cache = {}
        for pattern in patterns:
            if pattern in self.where or len(self.slots) == 256:
                continue
            self.where[pattern] = len(self.slots)
            self.slots.append(pattern)
        filler = 0x100000000
        while len(self.slots) < 256:
            self.slots.append(filler)
            filler += 1

    def cover(self, counter):
        return sum(count for pattern, count in counter.items() if pattern in self.where)

    def slot_of(self, pattern):
        found = self.where.get(pattern)
        if found is not None:
            return found
        cached = self.cache.get(pattern)
        if cached is not None:
            return cached
        best = 0
        best_dist = 99
        for index, stored in enumerate(self.slots):
            if stored > 0xFFFFFFFF:
                continue
            gap = dist(pattern, stored)
            if gap < best_dist:
                best = index
                best_dist = gap
                if gap == 0:
                    break
        self.cache[pattern] = best
        return best

    def improve(self, counter, budget):
        updates = []
        if budget <= 0:
            return updates
        missing = [pattern for pattern, _count in counter.most_common()
                   if pattern not in self.where]
        for pattern in missing:
            if len(updates) >= budget:
                break
            slot = min(range(256), key=lambda index: counter.get(self.slots[index], 0))
            if counter.get(self.slots[slot], 0) >= counter[pattern]:
                break
            old = self.slots[slot]
            if old in self.where and self.where[old] == slot:
                del self.where[old]
            self.slots[slot] = pattern
            self.where[pattern] = slot
            updates.append((slot, pattern))
        if updates:
            self.cache.clear()
        return updates

    def clone(self):
        fresh = Book.__new__(Book)
        fresh.slots = list(self.slots)
        fresh.where = dict(self.where)
        fresh.cache = {}
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
        if writes:
            self.cache.clear()


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


def differing(left, right):
    slots = []
    for index, (have, want) in enumerate(zip(left.slots, right.slots)):
        if visible(have) != visible(want):
            slots.append(index)
    return slots


def scy_of(patterns, book, previous_sources=None, previous=None):
    """Bytes for one picture. A cell the source left alone keeps what it
    showed, unless its exact pattern has arrived. A changed cell keeps its
    previous pattern when that is nearly as close as the best one."""
    out = bytearray(SCREEN_H * CELLS)
    shown = [0] * (SCREEN_H * CELLS)
    exact = 0
    cursor = 0
    for y in range(SCREEN_H):
        for _cell in range(CELLS):
            pattern = patterns[cursor]
            resident = book.where.get(pattern)
            if resident is not None:
                slot = resident
                exact += 1
            elif (previous is not None and previous_sources[cursor] == pattern
                    and previous[cursor] in book.where):
                slot = book.where[previous[cursor]]
            else:
                slot = book.slot_of(pattern)
                if previous is not None:
                    old = previous[cursor]
                    if old <= 0xFFFFFFFF and old in book.where:
                        chosen = book.slots[slot]
                        chosen_gap = 99 if chosen > 0xFFFFFFFF else dist(pattern, chosen)
                        if dist(pattern, old) <= chosen_gap + 2:
                            slot = book.where[old]
            out[cursor] = (slot - y) & 255
            shown[cursor] = visible(book.slots[slot])
            cursor += 1
    return bytes(out), exact, shown


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


def improve_by_gain(book, current, displayed, upcoming, weight, budget):
    """Spend up to budget slots where they remove the most shade error.

    A miss of two or more shades costs more than a neighbouring shade. The
    cells of several upcoming pictures are stacked, so a pattern they share
    outranks one that only the next picture uses. A still run therefore fills
    its spare slots with the picture that ends the run.
    """
    if budget <= 0:
        return []
    slots = np.array([visible(pattern) for pattern in book.slots], dtype=np.uint32)
    filler = np.array([pattern > 0xFFFFFFFF for pattern in book.slots])
    shown = np.asarray(displayed, dtype=np.uint32)
    current = np.asarray(current, dtype=np.uint32)
    upcoming = np.asarray(upcoming, dtype=np.uint32)
    locked = upcoming == current
    keep = np.zeros(256)
    first = SCREEN_H * CELLS
    for cell in np.nonzero(locked)[0]:
        slot = book.where.get(int(shown[cell]))
        if slot is None:
            continue
        if cell < first:
            keep[slot] += KEEP_SENTINEL
        else:
            keep[slot] += weight[cell] * FLICKER_COST
    uniq, inverse = np.unique(upcoming, return_inverse=True)
    free = ~locked
    free_weight = np.bincount(inverse[free], weights=weight[free], minlength=len(uniq))
    locked_cost = weight * cost_paired(upcoming, shown)
    locked_gain = np.bincount(inverse[locked], weights=locked_cost[locked], minlength=len(uniq))
    gaps = cost_gaps(uniq, slots)
    gaps[:, filler] = 16 * FAR_STEP + 1
    resident = np.array([int(pattern) in book.where for pattern in uniq])
    candidates = np.nonzero(~resident)[0]
    if not len(candidates):
        return []
    rows = np.arange(len(uniq))
    nearest = gaps.min(axis=1)
    rough = nearest[candidates] * free_weight[candidates] + locked_gain[candidates]
    candidates = candidates[np.argsort(-rough)[:CANDIDATES]]
    reach = cost_gaps(uniq[candidates], uniq)
    used = np.zeros(len(candidates), dtype=bool)
    writes = []
    limit = 16 * FAR_STEP
    for _step in range(budget):
        part = np.argpartition(gaps, 1, axis=1)
        best = part[:, 0]
        first = gaps[rows, best]
        second = np.minimum(gaps[rows, part[:, 1]], limit)
        loss = np.bincount(best, weights=free_weight * (second - first), minlength=256) + keep
        benefit = ((np.maximum(0, first[None, :] - reach) * free_weight[None, :]).sum(axis=1)
                   + locked_gain[candidates])
        benefit[used] = -1
        pick = int(np.argmax(benefit))
        victim = int(np.argmin(loss))
        if benefit[pick] - loss[victim] <= 0:
            break
        pattern = int(uniq[candidates[pick]])
        book.apply([(victim, pattern)])
        writes.append((victim, pattern))
        gaps[:, victim] = reach[pick]
        keep[victim] = 0
        used[pick] = True
    return writes


def prepare_hidden(shown, hidden, gain, flip_budget, hold_budget):
    """Point the off-screen book at a small edit of the one on screen.

    The edit is revealed only when every changed slot fits in the vblanks
    before the switch. The switch vblank itself stays empty, so the LCDC
    write happens at the start of vertical blank rather than the end.
    """
    stale = differing(hidden, shown)
    if len(stale) > flip_budget:
        writes = [(slot, visible(shown.slots[slot])) for slot in stale[:hold_budget]]
        hidden.apply(writes)
        return writes, False
    target = shown.clone()
    spare = flip_budget - len(stale)
    gained = improve_by_gain(target, *gain, spare) if spare > 0 else []
    slots = differing(hidden, target)
    if gained and len(slots) <= flip_budget:
        writes = [(slot, visible(target.slots[slot])) for slot in slots]
        hidden.apply(writes)
        return writes, True
    writes = [(slot, visible(shown.slots[slot])) for slot in stale[:hold_budget]]
    hidden.apply(writes)
    return writes, False


def same_pixels(left, right):
    return left["patterns"] == right["patterns"]


def coming_view(frames, arrays, weights, displayed, index):
    """Stack the next few scroll pictures behind the one on screen.

    A pattern that keeps occurring outranks a pattern that the next picture
    alone would spend a slot on. The stack stops at a tile card.
    """
    chosen = []
    ahead = index + 1
    while (ahead < len(frames) and len(chosen) < HORIZON
           and frames[ahead]["card"] < 0):
        chosen.append(ahead)
        ahead += 1
    if not chosen:
        return None
    count = len(chosen)
    return (
        np.tile(arrays[index], count),
        np.tile(np.asarray(displayed, dtype=np.uint32), count),
        np.concatenate([arrays[item] for item in chosen]),
        np.concatenate([weights[item] for item in chosen]),
    )


def build_plans(frames, lengths, fillers):
    count = len(frames)
    opening = 1
    while (opening < count and frames[opening]["card"] < 0
           and same_pixels(frames[0], frames[opening])):
        opening += 1
    book_a = Book(top_patterns(frames[0]["counter"], fillers))
    if opening < count and frames[opening]["card"] < 0:
        book_b = Book(top_patterns(frames[opening]["counter"], fillers))
    else:
        book_b = Book(top_patterns(frames[0]["counter"], fillers))
    initial_a = list(book_a.slots)
    initial_b = list(book_b.slots)
    arrays, weights = cell_weights(frames)
    shown = book_a
    hidden = book_b
    pending_flip = False
    plans = []
    exact = 0
    scored = 0
    flicker = 0
    stable = 0
    errors = {
        "still": 0, "moving": 0, "moving_cells": 0, "fixed": 0,
        "near": 0, "far": 0,
    }
    previous_sources = None
    previous_shown = None
    for index, frame in enumerate(frames):
        if frame["card"] >= 0:
            plans.append({
                "kind": "card",
                "card": frame["card"],
                "length": lengths[index],
            })
            # The player shows physical book A again. The objects stay tied
            # to those banks; only the roles change.
            shown = book_a
            hidden = book_b
            pending_flip = False
            previous_sources = None
            previous_shown = None
            continue
        flip = False
        if pending_flip:
            shown, hidden = hidden, shown
            flip = True
            pending_flip = False
        sources = frame["patterns"]
        scy, got, displayed = scy_of(sources, shown, previous_sources, previous_shown)
        exact += got
        scored += SCREEN_H * CELLS
        for cursor, (src, new_show) in enumerate(zip(sources, displayed)):
            wrong = dist(src, new_show)
            near, far = shade_gap(src, new_show)
            errors["near"] += near
            errors["far"] += far
            if previous_sources is not None and previous_sources[cursor] == src:
                stable += 1
                errors["still"] += wrong
                if previous_shown[cursor] != new_show:
                    if new_show == src:
                        errors["fixed"] += 1
                    else:
                        flicker += 1
            else:
                errors["moving_cells"] += 1
                errors["moving"] += wrong
        previous_sources = sources
        previous_shown = displayed
        updates = []
        if index == opening - 1 and opening < count and frames[opening]["card"] < 0:
            pending_flip = True
        elif index >= opening and index + 1 < count and frames[index + 1]["card"] < 0:
            hold_budget = lengths[index] * BPV
            flip_budget = (lengths[index] - 1) * BPV
            gain = coming_view(frames, arrays, weights, displayed, index)
            updates, pending_flip = prepare_hidden(
                shown, hidden, gain, flip_budget, hold_budget)
        plans.append({
            "kind": "scroll",
            "flip": flip,
            "updates": updates,
            "scy": scy,
            "length": lengths[index],
        })
    return plans, initial_a, initial_b, exact, scored, flicker, stable, errors


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
        if (previous and previous["kind"] == "scroll" and not plan["flip"]
                and previous["scy"] == plan["scy"]
                and previous["length"] + plan["length"] <= 254
                and len(previous["updates"]) + len(updates) <= 223):
            previous["length"] += plan["length"]
            previous["updates"].extend(updates)
            continue
        merged.append({
            "kind": "scroll",
            "flip": plan["flip"],
            "updates": list(updates),
            "scy": plan["scy"],
            "length": plan["length"],
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
        record.append(1 if plan["flip"] else 0)
        record.append(len(updates))
        for slot, pattern in updates:
            record.append(slot)
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
    """A switch has to be the first thing in its vblank, so the picture
    before it cannot still be patching when that vblank starts."""
    for index, plan in enumerate(plans):
        if plan["kind"] != "scroll":
            continue
        nxt = plans[index + 1] if index + 1 < len(plans) else None
        flips_next = nxt is not None and nxt["kind"] == "scroll" and nxt["flip"]
        cap = (plan["length"] - (1 if flips_next else 0)) * BPV
        if len(plan["updates"]) > cap:
            sys.exit(
                "record %d has %d updates and the next switch allows %d"
                % (index, len(plan["updates"]), cap)
            )


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
    plans, book_a, book_b, exact, scored, flicker, stable, errors = build_plans(
        frames, lengths, fillers.most_common())
    merged = merge_plans(plans)
    check_budgets(merged)
    blob = emit(merged, card_maps)
    with open(video_path, "wb") as handle:
        handle.write(blob)
    write_include(patterns_path, book_a)
    write_include(bookb_path, book_b)
    scrolls = sum(1 for plan in merged if plan["kind"] == "scroll")
    cards = sum(1 for plan in merged if plan["kind"] == "card")
    flips = sum(1 for plan in merged if plan["kind"] == "scroll" and plan["flip"])
    print(
        "Wrote %d pictures (%d scroll, %d tile cards, %d book flips), %d bytes"
        % (scrolls + cards, scrolls, cards, flips, len(blob))
    )
    if scored:
        pixels = scored * 16
        matched = pixels - errors["near"] - errors["far"]
        print(
            "Exact 16-pixel cells %d / %d (%.2f%%)"
            % (exact, scored, 100.0 * exact / scored)
        )
        print(
            "Matching shades %d / %d (%.2f%%); one step away %d, two or more %d"
            % (matched, pixels, 100.0 * matched / pixels,
               errors["near"], errors["far"])
        )
    if stable:
        print(
            "Unchanged cells that switched to another wrong pattern %d / %d (%.2f%%),"
            " corrected to the source %d"
            % (flicker, stable, 100.0 * flicker / stable, errors["fixed"])
        )
        print(
            "Wrong pixels per 16: unchanged cells %.3f, moving cells %.3f"
            % (errors["still"] / stable,
               errors["moving"] / max(1, errors["moving_cells"]))
        )


if __name__ == "__main__":
    main()
