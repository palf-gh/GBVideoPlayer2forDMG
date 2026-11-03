; GBVideoPlayer2 for the original Game Boy.
; Derived from LIJI32's GBVideoPlayer2. The colour player spends an 8 MHz
; scanline rewriting SCY every 8 pixels and reloading CGB palettes. This
; build runs at 4 MHz, so the same two-instruction write covers 16 pixels.
; Each 16-pixel cell is one of 256 codebook entries: four 4-pixel shades.
; Audio is omitted. The freed time is the margin that keeps the copy loop
; inside one scanline, not a way to recover 8-pixel cells.

INCLUDE "gbhw.asm"

; One hardware frame of picture data. Keep this agreed with encoder.c.
DEF FRAME_BYTES EQU 1 + 144 * 10

; Mode 2 begins on dot 0 of every visible line. The fastest timer ticks
; every 16 T-cycles, and 456 is not a multiple of 16, so the LCD STAT
; vector wakes the CPU instead. Each nop slips the scroll writes 4 dots
; later. A two-write probe put the early write at handler cycle 22 and
; showed the next write, gap M-cycles later, at x = 24 + 4*gap. The burst
; gaps are 4, so the first write has to end at cycle 16 for the following
; edges to land on x = 16, 32, 48, ... That is TUNE_NOPS = 12.
IF !DEF(TUNE_NOPS)
DEF TUNE_NOPS EQU 12
ENDC
DEF STAT_MODE2 EQU $20

DEF frame_repeat EQU $FF80
DEF repeat_bank EQU $FF81
DEF repeat_bank_high EQU $FF82
DEF repeat_sp EQU $FF83
DEF rom_bank EQU $FF85
DEF rom_bank_high EQU $FF86
; hold is 1 while the current picture stays up until Start. armed becomes
; 1 once Start has been seen released, so a held button cannot replay.
DEF hold EQU $FF87
DEF armed EQU $FF88
; The picture on screen uses one 256-pattern book. Patches land in the other.
DEF upd_count EQU $FF89
DEF upd_ptr EQU $FF8A
DEF hidden_base EQU $FF8C
DEF lcdc_scroll EQU $FF8D
DEF map_dirty EQU $FF8E
DEF upd_batch EQU $FF8F
; Bit 5 clear selects A, B, Select and Start. Bit 4 clear selects
; the d-pad. $20 is the d-pad, so a Start press is read with $10.
DEF JOY_BUTTONS EQU $10
DEF JOY_IDLE EQU $30
DEF PAD_START EQU $08

SECTION "StatVector", ROM0[$48]
; Entered on mode 2. HL is the line, C is rSCY, B counts lines still to show.
StatLine:
REPT TUNE_NOPS
  nop
ENDR
REPT 10
  ld a, [hli]
  ldh [c], a
ENDR
  dec b
  jp z, FinishedPicture
  reti

SECTION "Entry", ROM0[$100]
  di
  jp Start
  ds $150 - @

SECTION "Player", ROM0[$150]
Start:
  di
  ld sp, $FFFE
  xor a
  ldh [rNR52], a
  call LCDOff
  ld a, $E4
  ldh [rBGP], a
  xor a
  ldh [rSCX], a
  ldh [rSCY], a

BootVideo:
  call LCDOff
  call LoadTiles
  call CreateMap
  ld a, $84
  ldh [hidden_base], a
  ld a, $91
  ldh [lcdc_scroll], a
  xor a
  ldh [upd_count], a
  ldh [map_dirty], a
  ldh [rom_bank_high], a
  ld [$3000], a
  inc a
  ldh [rom_bank], a
  ld [$2000], a
  ld hl, $4000
  ld a, 1
  ldh [hold], a
  ldh [armed], a
  jr LoadFrame

; HL is at the next record. A record starts only below $7600, leaving
; 2560 bytes, so the following bank holds anything that would not fit.
EnsureBank:
  ld a, h
  cp $76
  jr c, .ok
.switch
  ldh a, [rom_bank]
  inc a
  ldh [rom_bank], a
  jr z, .high
  ld [$2000], a
  ld hl, $4000
  jr .ok
.high
  ldh a, [rom_bank_high]
  inc a
  ldh [rom_bank_high], a
  ld [$3000], a
  xor a
  ld [$2000], a
  ldh [rom_bank], a
  ld hl, $4000
.ok
  ret

; HL points at a record. $00 ends the stream. $FF is a tile card, followed
; by a repeat and 18 rows of 20 tile ids. Anything else is a scroll repeat.
LoadFrame:
  ld a, [hli]
  and a
  jp z, StopEmpty
  cp $FF
  jp z, ShowTile

; A is the scroll repeat. C is set to rSCY. hold is left as BootVideo set it.
OpenScroll:
  call PrepareScroll
  ld b, 144
  ld c, LOW(rSCY)
  ldh a, [map_dirty]
  and a
  jr z, .lcd
  call LCDOff
  ld h, $9C
  ld b, 64
  call BuildMap
  xor a
  ldh [map_dirty], a
  ld a, $91
  ldh [lcdc_scroll], a
  ld a, $84
  ldh [hidden_base], a
  ld b, 144
  ld c, LOW(rSCY)
.lcd
  ldh a, [rLCDC]
  add a, a
  jr c, .map
  ldh a, [lcdc_scroll]
  ldh [rLCDC], a
  jr .waitblank
.map
  call WaitVBlank
  ldh a, [lcdc_scroll]
  ldh [rLCDC], a
.waitblank
  ldh a, [rLY]
  cp 144
  jr c, .waitblank
  ld a, STAT_MODE2
  ldh [rSTAT], a
  xor a
  ldh [rIF], a
  ld a, 1 << LCD_STAT
  ldh [rIE], a
  jp MainLoop

; Entered with HL on the repeat byte. The LCD is switched off, the card is
; copied to map $9C00, then shown for the repeat with the scroll burst stopped.
ShowTile:
  ld a, [hli]
  ldh [frame_repeat], a
  ldh a, [rLCDC]
  add a, a
  jr nc, .copy
  call WaitVBlank
  xor a
  ldh [rLCDC], a
.copy
  ld de, $9C00
  ld c, 18
.row
  ld b, 20
.cell
  ld a, [hli]
  ld [de], a
  inc de
  dec b
  jr nz, .cell
  ld a, e
  add a, 12
  ld e, a
  jr nc, .next
  inc d
.next
  dec c
  jr nz, .row
  xor a
  ldh [rSCX], a
  ldh [rSCY], a
  ld a, $99
  ldh [rLCDC], a
.frames
  call NextFrame
  ldh a, [frame_repeat]
  dec a
  ldh [frame_repeat], a
  jr nz, .frames
  ld a, 1
  ldh [map_dirty], a
  call EnsureBank
  ; Credits close the stream, so this zero is read here rather than in
  ; FinishedPicture. Show the opening picture and wait for Start again.
  ld a, [hl]
  and a
  jp z, Restart
  jp LoadFrame

; Count one displayed frame. A call made in vblank waits for the next one.
NextFrame:
.vis
  ldh a, [rLY]
  cp 144
  jr nc, .vis
.blank
  ldh a, [rLY]
  cp 144
  jr c, .blank
  ret

WaitVBlank:
  ldh a, [rLY]
  cp 144
  jr c, WaitVBlank
  ret

StopEmpty:
  xor a
  ldh [rIE], a
  ldh [rLCDC], a
.hang
  halt
  nop
  jr .hang

; The terminating count is read from inside the handler. Drop the interrupt
; frame and show the first picture again, held until Start.
Restart:
  xor a
  ldh [rIF], a
  ld sp, $FFFE
  jp BootVideo

; IME is enabled for the halt, so mode 2 vectors to StatLine and returns
; here. Clearing IF first keeps a stale latch from becoming the halt bug.
MainLoop:
  xor a
  ldh [rIF], a
  ei
  halt
  jr MainLoop

FinishedPicture:
  ; The ten writes for this line have already landed. A repeat rewinds HL.
  ; Only a new frame can cross a bank. The threshold matches encoder.c.
  ; The rewind then waits for vblank, so the next mode 2 is line 0 and the
  ; first data line is not latched halfway down the screen.
  ; While hold is set, rewind every frame and sample Start here. The burst
  ; itself stays untouched. A press is taken only after a release.
  ldh a, [hold]
  and a
  jr z, .playing
  ldh a, [repeat_bank]
  ld [$2000], a
  ldh [rom_bank], a
  ldh a, [repeat_bank_high]
  ld [$3000], a
  ldh [rom_bank_high], a
  ldh a, [repeat_sp]
  ld l, a
  ldh a, [repeat_sp+1]
  ld h, a
  ld b, 144
  ld a, JOY_BUTTONS
  ldh [rJOYP], a
  ldh a, [rJOYP]
  ldh a, [rJOYP]
  ld d, a
  ld a, JOY_IDLE
  ldh [rJOYP], a
  ld a, d
  and PAD_START
  jr nz, .startUp
  ldh a, [armed]
  and a
  jr z, .resync
  ; This picture has already been on screen. Skip its remaining repeats
  ; so Start shows the next picture on the following frame.
  xor a
  ldh [hold], a
  ldh [armed], a
  ld a, l
  add a, LOW(1440)
  ld l, a
  ld a, h
  adc a, HIGH(1440)
  ld h, a
  jp .advance
.startUp
  ld a, 1
  ldh [armed], a
  jr .resync
.playing
  ldh a, [frame_repeat]
  dec a
  jr z, .advance
  ldh [frame_repeat], a
  ldh a, [repeat_bank]
  ld [$2000], a
  ldh [rom_bank], a
  ldh a, [repeat_bank_high]
  ld [$3000], a
  ldh [rom_bank_high], a
  ldh a, [repeat_sp]
  ld l, a
  ldh a, [repeat_sp+1]
  ld h, a
  ld b, 144
  jr .resyncplay
.advance
  xor a
  ldh [rIF], a
.advwait
  ldh a, [rLY]
  cp 144
  jr c, .advwait
  call DrainUpdates
  call EnsureBank
.count
  ld a, [hli]
  and a
  jp z, Restart
  cp $FF
  jr nz, .scroll
  xor a
  ldh [rIE], a
  ldh [rIF], a
  ld sp, $FFFE
  jp ShowTile
.scroll
  call PrepareScroll
  ld b, 144
  ld c, LOW(rSCY)
  jr .resync
.resyncplay
  xor a
  ldh [rIF], a
.waitplay
  ldh a, [rLY]
  cp 144
  jr c, .waitplay
  call DrainUpdates
  ld b, 144
  ld c, LOW(rSCY)
.resync
  xor a
  ldh [rIF], a
.waitblank
  ldh a, [rLY]
  cp 144
  jr c, .waitblank
  xor a
  ldh [rIF], a
  reti

LCDOff:
  ldh a, [rLCDC]
  add a, a
  ret nc
.wait
  ldh a, [rLY]
  cp 144
  jr c, .wait
  xor a
  ldh [rLCDC], a
  ret

LoadTiles:
  ld hl, Patterns
  ld de, $8000
  ld bc, 1024
  call CopyBytes
IF DEF(CUSTOM_PATTERNS)
  ld hl, BookB
ELSE
  ld hl, Patterns
ENDC
  ld de, $8400
  ld bc, 1024
  call CopyBytes
  ld hl, FontTiles
  ld de, $8800
  ld bc, FontTilesEnd - FontTiles
  ld a, b
  or c
  ret z
  jp CopyBytes

CopyBytes:
  ld a, [hli]
  ld [de], a
  inc de
  dec bc
  ld a, b
  or c
  jr nz, CopyBytes
  ret

; H is $98 or $9C and B is the tile-id base. Even columns store the row
; plus that base; odd columns store the same plus 32. LCD is off.
BuildMap:
  ld l, 0
  ld c, 0
.row
  ld a, c
  add a, b
  ld e, 10
.pair
  ld [hli], a
  add a, 32
  ld [hli], a
  sub 32
  dec e
  jr nz, .pair
  ld a, l
  add a, 12
  ld l, a
  jr nc, .next
  inc h
.next
  inc c
  ld a, c
  cp 32
  jr nz, .row
  ret

CreateMap:
  ld h, $98
  ld b, 0
  call BuildMap
  ld h, $9C
  ld b, 64
  jp BuildMap

; A is the repeat and HL points at the flags byte. Leaves HL on the first
; SCY byte and remembers the hidden-book patches that follow the flags.
PrepareScroll:
  ldh [frame_repeat], a
  ld a, [hli]
  bit 0, a
  jr z, .noflip
  ldh a, [lcdc_scroll]
  xor $08
  ldh [lcdc_scroll], a
  ldh a, [rLCDC]
  add a, a
  jr nc, .flipbase
  ldh a, [lcdc_scroll]
  ldh [rLCDC], a
.flipbase
  ldh a, [hidden_base]
  xor $04
  ldh [hidden_base], a
.noflip
  ld a, [hli]
  ldh [upd_count], a
  ld a, l
  ldh [upd_ptr], a
  ld a, h
  ldh [upd_ptr+1], a
  ldh a, [upd_count]
  ld b, a
  ld e, a
  xor a
  ld d, a
  sla e
  rl d
  sla e
  rl d
  ld a, e
  add a, b
  ld e, a
  ld a, d
  adc a, 0
  ld d, a
  add hl, de
  ld a, l
  ldh [repeat_sp], a
  ld a, h
  ldh [repeat_sp+1], a
  ldh a, [rom_bank]
  ldh [repeat_bank], a
  ldh a, [rom_bank_high]
  ldh [repeat_bank_high], a
  ret

; Patch up to 20 entries of the hidden book, stopping before line 0.
; HL belongs to the caller. It is this picture's scroll pointer, or the
; next record once the last line has been shown. Only the patch walk
; borrows it.
DrainUpdates:
  ldh a, [upd_count]
  and a
  ret z
  push hl
  cp 20
  jr c, .setb
  ld a, 20
.setb
  ld b, a
  ldh [upd_batch], a
  ldh a, [upd_ptr]
  ld l, a
  ldh a, [upd_ptr+1]
  ld h, a
  ldh a, [hidden_base]
  ld c, a
.one
  ldh a, [rLY]
  cp 153
  jr nc, .save
  ld a, [hli]
  add a, a
  ld e, a
  ld d, c
  jr nc, .left
  inc d
.left
  ld a, [hli]
  ld [de], a
  inc e
  ld a, [hli]
  ld [de], a
  inc d
  inc d
  dec e
  ld a, [hli]
  ld [de], a
  inc e
  ld a, [hli]
  ld [de], a
  dec b
  jr nz, .one
.save
  ldh a, [upd_batch]
  sub b
  ld b, a
  ldh a, [upd_count]
  sub b
  ldh [upd_count], a
  ld a, l
  ldh [upd_ptr], a
  ld a, h
  ldh [upd_ptr+1], a
  pop hl
  ld c, LOW(rSCY)
  ret

; 256 patterns in tiles 0-63, and a second book in tiles 64-127.
DEF q0 = 0
DEF q1 = 0
DEF q2 = 0
DEF q3 = 0

Patterns:
IF DEF(CUSTOM_PATTERNS)
INCLUDE "output/patterns.inc"
ELSE
FOR idx, 256
  REDEF q0 = (idx >> 6) & 3
  REDEF q1 = (idx >> 4) & 3
  REDEF q2 = (idx >> 2) & 3
  REDEF q3 = idx & 3
  db ((q0 & 1) * $F0) | ((q1 & 1) * $0F)
  db ((q0 >> 1) * $F0) | ((q1 >> 1) * $0F)
ENDR
FOR idx, 256
  REDEF q0 = (idx >> 6) & 3
  REDEF q1 = (idx >> 4) & 3
  REDEF q2 = (idx >> 2) & 3
  REDEF q3 = idx & 3
  db ((q2 & 1) * $F0) | ((q3 & 1) * $0F)
  db ((q2 >> 1) * $F0) | ((q3 >> 1) * $0F)
ENDR
ENDC

IF DEF(CUSTOM_PATTERNS)
BookB:
INCLUDE "output/bookb.inc"
ENDC

; Credit cards use ordinary tiles from 128 upward. Tiles 0-127 are the
; two scroll books. An empty font means this build has no tile cards.
FontTiles:
IF DEF(CUSTOM_PATTERNS)
INCLUDE "output/font.inc"
ENDC
FontTilesEnd:

SECTION "Bank0End", ROM0[$3FFF]
  db 0
