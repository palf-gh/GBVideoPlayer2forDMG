/* DMG bitstream encoder for GBVideoPlayer2forDMG.
 * Derived from LIJI32's GBVideoPlayer2 encoder.
 *
 * A scroll picture never crosses a ROM bank. The player starts a record
 * only while HL is below $7600, leaving 2560 bytes. Layout:
 *   u8  repeat          1-254. 0 ends the stream and the player waits
 *                       for Start. 255 introduces a tile card instead.
 *   u8  flags           bit 0 flips which codebook is on screen
 *   u8  updates         how many hidden-book patches follow
 *   updates * 5 u8      slot, left low, left high, right low, right high
 *   144 * 10 u8         SCY values, one per 16-pixel cell
 * A tile card is its own picture and may sit anywhere in the stream:
 *   u8  255
 *   u8  repeat          1-255
 *   18 * 20 u8          background tile ids, row by row
 * Each SCY byte is (pattern - y) mod 256. The pattern index holds four
 * 4-pixel shades, MSB on the left. Shade 0 is light and shade 3 is dark.
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

#define SCREEN_W 160
#define SCREEN_H 144
#define SCREEN_SIZE (SCREEN_W * SCREEN_H)
#define CELLS 10
#define FRAME_BYTES (3 + SCREEN_H * CELLS)
#define TILE_MARK 255
#define CARD_W 20
#define CARD_H 18
#define CARD_MAP (CARD_W * CARD_H)

_Static_assert(FRAME_BYTES == 1443, "scroll header is repeat, flags, count");

static const double GB_FPS = (1024.0 * 1024.0 * 4.0) / 70224.0;

static uint8_t output[1024 * 1024 * 8 + 0x4000];
static size_t pos = 0x4000;
static size_t frame_count_pos = 0;

static const int bayer[4][4] = {
    {0, 8, 2, 10},
    {12, 4, 14, 6},
    {3, 11, 1, 9},
    {15, 7, 13, 5},
};

static int quantise(int luma)
{
    if (luma >= 213) return 0;
    if (luma >= 128) return 1;
    if (luma >= 43) return 2;
    return 3;
}

/* Four-colour pixel art sits in clusters near luma 45, 129, 200 and 246.
 * The photo thresholds put the darkest cluster in shade 2, and the ordered
 * dither then flips the others. These cuts sit in the gaps between clusters. */
static int quantise_art(int luma)
{
    if (luma >= 220) return 0;
    if (luma >= 160) return 1;
    if (luma >= 80) return 2;
    return 3;
}

static int pixel_art = 0;
static int card_count = 0;
static int last_card = -1;
static uint8_t *card_shades = NULL;
static uint8_t *card_maps = NULL;
static uint8_t last_index[SCREEN_H * CELLS];
static uint8_t codebook[256][16];
static unsigned long exact_cells = 0;
static unsigned long total_cells = 0;
static unsigned tile_pictures = 0;

static int cell_code(const uint8_t *shades)
{
    int best_index = 0;
    int best_dist = 1 << 30;

    for (int i = 0; i < 256; i++) {
        int dist = 0;
        const uint8_t *pattern = codebook[i];
        for (int pixel = 0; pixel < 16; pixel++) {
            int delta = (int)shades[pixel] - (int)pattern[pixel];
            dist += delta < 0 ? -delta : delta;
        }
        if (dist < best_dist) {
            best_dist = dist;
            best_index = i;
            if (dist == 0) break;
        }
    }
    return best_index;
}

static int clamp_byte(int value)
{
    if (value < 0) return 0;
    if (value > 255) return 255;
    return value;
}

static void align_frame(void)
{
    /* Player switches when the high byte reaches $76, i.e. offset $3600. */
    size_t off = pos & 0x3fff;
    if (off >= 0x3600) {
        pos = (pos + 0x3fff) & ~(size_t)0x3fff;
    }
    if (pos / 0x4000 >= 0x200) {
        fprintf(stderr, "error: video exceeds 512 ROM banks\n");
        exit(1);
    }
}

static void encode_image(const uint8_t *luma, unsigned repeat)
{
    uint8_t frame[FRAME_BYTES];
    size_t n = 0;

    /* 255 is the tile-card mark, so a scroll repeat stops at 254. */
    if (repeat > 254) {
        encode_image(luma, 254);
        repeat -= 254;
    }

    frame[n++] = (uint8_t)repeat;
    frame[n++] = 0;
    frame[n++] = 0;
    for (int y = 0; y < SCREEN_H; y++) {
        const uint8_t *row = luma + (size_t)y * SCREEN_W;
        for (int cell = 0; cell < CELLS; cell++) {
            int index;
            if (pixel_art) {
                uint8_t shades[16];
                const uint8_t *px = row + cell * 16;
                for (int pixel = 0; pixel < 16; pixel++) {
                    shades[pixel] = (uint8_t)quantise_art(px[pixel]);
                }
                index = cell_code(shades);
            } else {
                int shade[4];
                for (int sub = 0; sub < 4; sub++) {
                    const uint8_t *px = row + (cell * 16) + sub * 4;
                    int average = (px[0] + px[1] + px[2] + px[3]) / 4;
                    int jitter = (bayer[y & 3][sub] - 8) * 4;
                    shade[sub] = quantise(clamp_byte(average + jitter));
                }
                index = (shade[0] << 6) | (shade[1] << 4) | (shade[2] << 2) | shade[3];
            }
            frame[n++] = (uint8_t)(index - y);
        }
    }
    if (n != FRAME_BYTES) {
        fprintf(stderr, "error: internal frame size %u\n", (unsigned)n);
        exit(1);
    }

    align_frame();
    frame_count_pos = pos;
    memcpy(output + pos, frame, FRAME_BYTES);
    pos += FRAME_BYTES;
}

static void encode_tile(const uint8_t *map, unsigned repeat)
{
    if (repeat > 255) {
        encode_tile(map, 255);
        repeat -= 255;
    }
    align_frame();
    output[pos] = TILE_MARK;
    frame_count_pos = pos + 1;
    output[pos + 1] = (uint8_t)repeat;
    memcpy(output + pos + 2, map, CARD_MAP);
    pos += 2 + CARD_MAP;
}

static int match_card(const uint8_t *luma)
{
    for (int card = 0; card < card_count; card++) {
        const uint8_t *ref = card_shades + (size_t)card * SCREEN_SIZE;
        int same = 1;
        for (int i = 0; i < SCREEN_SIZE; i++) {
            if (quantise_art(luma[i]) != ref[i]) {
                same = 0;
                break;
            }
        }
        if (same) return card;
    }
    return -1;
}

static void load_cards(void)
{
    FILE *in = fopen("output/cards.bin", "rb");
    if (!in) return;
    uint16_t count = 0;
    if (fread(&count, 2, 1, in) != 1) {
        fprintf(stderr, "error: short output/cards.bin\n");
        exit(1);
    }
    card_count = count;
    if (!card_count) {
        fclose(in);
        return;
    }
    card_shades = malloc((size_t)card_count * SCREEN_SIZE);
    card_maps = malloc((size_t)card_count * CARD_MAP);
    if (!card_shades || !card_maps) {
        fprintf(stderr, "error: out of memory for credit cards\n");
        exit(1);
    }
    for (int card = 0; card < card_count; card++) {
        if (fread(card_shades + (size_t)card * SCREEN_SIZE, 1, SCREEN_SIZE, in) != SCREEN_SIZE ||
            fread(card_maps + (size_t)card * CARD_MAP, 1, CARD_MAP, in) != CARD_MAP) {
            fprintf(stderr, "error: truncated output/cards.bin\n");
            exit(1);
        }
    }
    fclose(in);
}

static int read_tga(FILE *in, uint8_t *luma)
{
    uint8_t header[18];
    uint8_t bgr[SCREEN_SIZE * 3];

    if (fread(header, 1, sizeof header, in) != sizeof header) {
        fprintf(stderr, "error: short TGA header\n");
        return 0;
    }
    if ((header[2] != 2 && header[2] != 10) || header[16] != 24) {
        fprintf(stderr, "error: expected a 24-bit true-colour TGA\n");
        return 0;
    }
    unsigned width = (unsigned)header[12] | ((unsigned)header[13] << 8);
    unsigned height = (unsigned)header[14] | ((unsigned)header[15] << 8);
    if (width != SCREEN_W || height != SCREEN_H) {
        fprintf(stderr, "error: TGA is %ux%u, expected 160x144\n", width, height);
        return 0;
    }
    if (header[0] && fseek(in, header[0], SEEK_CUR) != 0) {
        fprintf(stderr, "error: TGA id skip failed\n");
        return 0;
    }
    if (header[2] == 2) {
        if (fread(bgr, 1, sizeof bgr, in) != sizeof bgr) {
            fprintf(stderr, "error: short TGA image\n");
            return 0;
        }
    } else {
        size_t filled = 0;
        while (filled < SCREEN_SIZE) {
            int packet = fgetc(in);
            if (packet == EOF) {
                fprintf(stderr, "error: truncated TGA RLE stream\n");
                return 0;
            }
            size_t count = (size_t)(packet & 0x7f) + 1;
            if (filled + count > SCREEN_SIZE) {
                fprintf(stderr, "error: TGA RLE overrun\n");
                return 0;
            }
            if (packet & 0x80) {
                uint8_t pixel[3];
                if (fread(pixel, 1, 3, in) != 3) {
                    fprintf(stderr, "error: truncated TGA RLE pixel\n");
                    return 0;
                }
                for (size_t i = 0; i < count; i++) {
                    memcpy(bgr + (filled + i) * 3, pixel, 3);
                }
            } else if (fread(bgr + filled * 3, 1, count * 3, in) != count * 3) {
                fprintf(stderr, "error: truncated TGA raw packet\n");
                return 0;
            }
            filled += count;
        }
    }

    int top_origin = (header[17] & 0x20) != 0;
    for (int y = 0; y < SCREEN_H; y++) {
        int src_y = top_origin ? y : (SCREEN_H - 1 - y);
        const uint8_t *src = bgr + (size_t)src_y * SCREEN_W * 3;
        uint8_t *dst = luma + (size_t)y * SCREEN_W;
        for (int x = 0; x < SCREEN_W; x++) {
            int b = src[x * 3];
            int g = src[x * 3 + 1];
            int r = src[x * 3 + 2];
            dst[x] = (uint8_t)((r * 77 + g * 150 + b * 29) >> 8);
        }
    }
    return 1;
}

int main(int argc, char **argv)
{
    if (argc != 4) {
        fprintf(stderr, "usage: %s frame_rate quality output.bin < frame_list.txt\n", argv[0]);
        return 1;
    }

    double source_fps = strtod(argv[1], NULL);
    const char *slash = strchr(argv[1], '/');
    if (slash) {
        double denominator = strtod(slash + 1, NULL);
        if (denominator != 0.0) source_fps /= denominator;
    }
    unsigned quality = 0;
    if (strcmp(argv[2], "pixel") == 0) {
        pixel_art = 1;
        FILE *codebook_file = fopen("output/codebook.bin", "rb");
        if (!codebook_file || fread(codebook, 16, 256, codebook_file) != 256) {
            fprintf(stderr, "error: could not read output/codebook.bin\n");
            if (codebook_file) fclose(codebook_file);
            return 1;
        }
        fclose(codebook_file);
        load_cards();
    } else {
        quality = (unsigned)atoi(argv[2]);
    }
    if (source_fps <= 0.0) {
        fprintf(stderr, "error: invalid frame rate \"%s\"\n", argv[1]);
        return 1;
    }
    if (quality > 64) quality = 64;
    uint32_t threshold = 16384u * (quality + 1u);
    double frame_multiplier = GB_FPS / source_fps;
    double fps_tracking = 0.0;

    uint8_t luma[SCREEN_SIZE];
    uint8_t encoded[SCREEN_SIZE];
    int have_frame = 0;
    unsigned pictures = 0;

    memset(output, 0xff, sizeof output);

    char filepath[1024];
    while (scanf("%1023s", filepath) == 1) {
        fps_tracking += frame_multiplier;
        unsigned frame_length = (unsigned)fps_tracking;
        if (frame_length == 0) continue;
        fps_tracking -= frame_length;

        FILE *in = fopen(filepath, "rb");
        if (!in) {
            perror(filepath);
            return 1;
        }
        int ok = read_tga(in, luma);
        fclose(in);
        if (!ok) {
            fprintf(stderr, "error: while reading %s\n", filepath);
            return 1;
        }

        uint32_t diff = 0;
        uint8_t art_index[SCREEN_H * CELLS];
        int card = pixel_art ? match_card(luma) : -1;
        if (card >= 0) {
            if (last_card == card && have_frame &&
                (unsigned)output[frame_count_pos] + frame_length <= 255) {
                output[frame_count_pos] =
                    (uint8_t)(output[frame_count_pos] + frame_length);
                continue;
            }
            encode_tile(card_maps + (size_t)card * CARD_MAP, frame_length);
            last_card = card;
            have_frame = 1;
            pictures++;
            tile_pictures++;
            printf("\rEncoded picture %u", pictures);
            fflush(stdout);
            continue;
        }
        if (pixel_art) {
            for (int y = 0; y < SCREEN_H; y++) {
                const uint8_t *row = luma + (size_t)y * SCREEN_W;
                for (int cell = 0; cell < CELLS; cell++) {
                    uint8_t shades[16];
                    const uint8_t *px = row + cell * 16;
                    for (int pixel = 0; pixel < 16; pixel++) {
                        shades[pixel] = (uint8_t)quantise_art(px[pixel]);
                    }
                    int index = cell_code(shades);
                    art_index[y * CELLS + cell] = (uint8_t)index;
                    total_cells++;
                    if (memcmp(shades, codebook[index], 16) == 0) exact_cells++;
                }
            }
        } else if (have_frame) {
            for (size_t i = 0; i < SCREEN_SIZE; i++) {
                diff += (uint32_t)abs((int)luma[i] - (int)encoded[i]);
            }
        }
        int same_art = last_card < 0 && pixel_art && have_frame &&
            memcmp(art_index, last_index, sizeof art_index) == 0;
        if (last_card < 0 && have_frame &&
            (same_art || (!pixel_art && diff <= threshold)) &&
            (unsigned)output[frame_count_pos] + frame_length <= 254) {
            output[frame_count_pos] = (uint8_t)(output[frame_count_pos] + frame_length);
            continue;
        }
        if (pixel_art) memcpy(last_index, art_index, sizeof art_index);
        last_card = -1;

        memcpy(encoded, luma, sizeof encoded);
        encode_image(encoded, frame_length);
        have_frame = 1;
        pictures++;
        printf("\rEncoded picture %u", pictures);
        fflush(stdout);
    }

    align_frame();
    output[pos++] = 0;

    FILE *out = fopen(argv[3], "wb");
    if (!out) {
        perror(argv[3]);
        return 1;
    }
    size_t nbytes = pos - 0x4000;
    if (fwrite(output + 0x4000, 1, nbytes, out) != nbytes) {
        fprintf(stderr, "error: short write to %s\n", argv[3]);
        fclose(out);
        return 1;
    }
    fclose(out);
    printf("\nWrote %u pictures (%u tile cards), %u bytes\n",
           pictures, tile_pictures, (unsigned)nbytes);
    if (pixel_art && total_cells) {
        printf("Exact 16-pixel cells %lu / %lu (%.2f%%)\n",
               exact_cells, total_cells,
               100.0 * (double)exact_cells / (double)total_cells);
    }
    return 0;
}
