# Quality: 0 repeats a frame only when it is almost identical to the last
# stored picture. Higher values hold the previous picture for longer.
# PIXEL=1 scales with nearest neighbour and maps four-colour pixel art
# onto the shade clusters, without the photo dither.
# CARDS is a directory of 160x144 text screens. Each matching source
# frame, wherever it sits, is stored as tiles instead of scroll patterns.
QUALITY ?= 4
PIXEL ?= 0
CARDS ?= glyphs/cards

RGBASM ?= rgbasm
RGBLINK ?= rgblink
RGBFIX ?= rgbfix
CC ?= gcc

ifeq ($(OS),Windows_NT)
  EXE := .exe
  PATHCONV := cygpath -w -f -
else
  EXE :=
  PATHCONV := cat
endif

ifneq ($(MAKECMDGOALS),clean)
ifeq ($(SOURCE),)
$(error Missing video source. Use "make SOURCE=...")
endif
endif

OUT := output/$(basename $(notdir $(SOURCE)))
$(shell mkdir -p $(OUT))
FFMPEG := ffmpeg -loglevel warning -stats -hide_banner
ENCODER := output/encoder$(EXE)

TITLE = "\033[1m\033[36m"
TITLE_END = "\033[0m"

.PHONY: clean

$(OUT)/$(basename $(notdir $(SOURCE))).gb: output/player.gb $(OUT)/video.bin
	@echo $(TITLE)Creating ROM...$(TITLE_END)
	cat $^ > $@
	$(RGBFIX) -v -p 0xff -t "GBVPDMG" -m 25 $@
	cp output/video.sym $(basename $@).sym

output/player.gb: video.asm gbhw.asm
	@echo $(TITLE)Compiling player...$(TITLE_END)
	$(RGBASM) $(RGBASM_FLAGS) -o output/player.o video.asm
	$(RGBLINK) -o $@ -n output/video.sym -m output/video.map output/player.o
	@test $$(wc -c < $@) -eq 16384

ifeq ($(PIXEL),1)
  ENCODER_MODE := pixel
  SCALE_FILTER := scale=160:144:flags=neighbor
  RGBASM_FLAGS := -D CUSTOM_PATTERNS
else
  ENCODER_MODE := $(QUALITY)
  SCALE_FILTER := scale=160:144:force_original_aspect_ratio=increase,crop=160:144
  RGBASM_FLAGS :=
endif

ifeq ($(PIXEL),1)
output/player.gb: output/patterns.inc output/bookb.inc output/font.inc

output/patterns.inc output/bookb.inc: pixel_encode.py output/cards.bin $(OUT)/frames
	@echo $(TITLE)Encoding video...$(TITLE_END)
	python pixel_encode.py $(OUT)/frames output/cards.bin output/patterns.inc output/bookb.inc $(OUT)/video.bin \
		$(shell ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 "$(SOURCE)")

$(OUT)/video.bin: output/patterns.inc
	@test -s $@

output/font.inc output/cards.bin: cards.py $(wildcard $(CARDS)/*.png) $(wildcard $(CARDS)/*.tga)
	@echo $(TITLE)Building credit font...$(TITLE_END)
	python cards.py $(CARDS) output/font.inc output/cards.bin
endif

ifneq ($(PIXEL),1)
$(OUT)/video.bin: $(ENCODER) $(OUT)/frames
	@echo $(TITLE)Encoding video...$(TITLE_END)
	find $(OUT)/frames -name '*.tga' | sort | $(PATHCONV) | \
	$(ENCODER) $(shell ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 "$(SOURCE)") \
	           $(ENCODER_MODE) \
	           $@
endif

$(ENCODER): encoder.c
	@echo $(TITLE)Compiling encoder...$(TITLE_END)
	$(CC) -g -O2 -std=c11 -Werror -Wall -o $@ $^

$(OUT)/frames: $(SOURCE)
	@echo $(TITLE)Extracting frames...$(TITLE_END)
	rm -rf $@
	mkdir -p $@
	$(FFMPEG) -i "$(SOURCE)" -an \
	  -vf "$(SCALE_FILTER)" \
	  $@/%05d.tga

clean:
	rm -rf output
