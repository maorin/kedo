/* mock_libnx: minimal stubs for host build with ASAN/UBSAN. */

#include "switch.h"

/* PrintConsole is opaque in the public header; keep it that way for the mock too. */
struct PrintConsole {
    int placeholder;
};

static struct PrintConsole g_default_console = {0};
static int g_main_loop_remaining = 1000; /* default: stop after 1000 iterations */

/* Pad button state — set via mock_libnx_set_buttons before padUpdate */
static uint64_t g_btn_held = 0;
static uint64_t g_btn_down = 0;
static uint64_t g_btn_up   = 0;

PrintConsole *consoleInit(PrintConsole *console) {
    if (!console) console = &g_default_console;
    fprintf(stdout, "[mock_libnx] consoleInit\n");
    fflush(stdout);
    return console;
}

void consoleClear(void) {
    fprintf(stdout, "\n[mock_libnx] consoleClear\n");
    fflush(stdout);
}

void consoleUpdate(PrintConsole *console) {
    (void)console;
    fflush(stdout);
}

void consoleExit(PrintConsole *console) {
    (void)console;
    fprintf(stdout, "[mock_libnx] consoleExit\n");
    fflush(stdout);
}

bool appletMainLoop(void) {
    if (g_main_loop_remaining <= 0) return false;
    g_main_loop_remaining--;
    return true;
}

void mock_libnx_set_main_loop_iterations(int n) {
    g_main_loop_remaining = n;
}

void padConfigureInput(uint32_t max_players, uint32_t style_set) {
    (void)max_players; (void)style_set;
}

void padInitializeAny(PadState *pad) {
    if (!pad) return;
    pad->buttons_held = 0;
    pad->buttons_down = 0;
    pad->buttons_up   = 0;
}

void padUpdate(PadState *pad) {
    if (!pad) return;
    pad->buttons_held = g_btn_held;
    pad->buttons_down = g_btn_down;
    pad->buttons_up   = g_btn_up;
    /* Auto-clear edge events so successive padUpdate() reads are not sticky. */
    g_btn_down = 0;
    g_btn_up   = 0;
}

uint64_t padGetButtons(const PadState *pad)     { return pad ? pad->buttons_held : 0; }
uint64_t padGetButtonsDown(const PadState *pad) { return pad ? pad->buttons_down : 0; }
uint64_t padGetButtonsUp(const PadState *pad)   { return pad ? pad->buttons_up   : 0; }

void mock_libnx_set_buttons(uint64_t held, uint64_t down, uint64_t up) {
    g_btn_held = held;
    g_btn_down = down;
    g_btn_up   = up;
}

void svcSleepThread(uint64_t ns) {
    /* convert ns to us, cap at 100ms to keep tests fast */
    uint64_t us = ns / 1000;
    if (us > 100000) us = 100000;
    if (us > 0) usleep((useconds_t)us);
}

Result socketInitializeDefault(void) { return 0; }
void   socketExit(void)              { }

Result romfsInit(void) { return 0; }
void   romfsExit(void) { }

Result hosversionGet(uint32_t *out) {
    if (out) *out = 0x000B0000; /* fake "11.0.0" */
    return 0;
}
