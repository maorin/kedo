/* mock_libnx: minimal libnx surface for host build with ASAN/UBSAN
 *
 * Cover the bare minimum so a Switch homebrew main.c can link and run on
 * Linux/Mac. Add per-project stubs in `src/extra_*.c` rather than expanding
 * this header. Keep this file focused on the 80% case.
 */
#ifndef MOCK_LIBNX_SWITCH_H
#define MOCK_LIBNX_SWITCH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Result codes — libnx uses uint32_t Result; in mock everything is 0 (success) */
typedef uint32_t Result;
#define R_SUCCEEDED(res) ((res) == 0)
#define R_FAILED(res)    ((res) != 0)

/* Console */
typedef struct PrintConsole PrintConsole;
PrintConsole *consoleInit(PrintConsole *console);
void consoleClear(void);
void consoleUpdate(PrintConsole *console);
void consoleExit(PrintConsole *console);

/* Applet (main loop) */
bool appletMainLoop(void);
void mock_libnx_set_main_loop_iterations(int n); /* test helper */

/* Pad / input */
typedef struct PadState {
    uint64_t buttons_held;
    uint64_t buttons_down;
    uint64_t buttons_up;
} PadState;

#define HidNpadStyleSet_NpadStandard 1
#define PadButton_A     (1u << 0)
#define PadButton_B     (1u << 1)
#define PadButton_X     (1u << 2)
#define PadButton_Y     (1u << 3)
#define PadButton_Plus  (1u << 10)
#define PadButton_Minus (1u << 11)
#define PadButton_L     (1u << 6)
#define PadButton_R     (1u << 7)

void padConfigureInput(uint32_t max_players, uint32_t style_set);
void padInitializeAny(PadState *pad);
void padUpdate(PadState *pad);
uint64_t padGetButtons(const PadState *pad);
uint64_t padGetButtonsDown(const PadState *pad);
uint64_t padGetButtonsUp(const PadState *pad);

/* Test helper: inject button state for the next padUpdate */
void mock_libnx_set_buttons(uint64_t held, uint64_t down, uint64_t up);

/* Threads */
void svcSleepThread(uint64_t ns);

/* Socket (libcurl etc. depend on this) */
Result socketInitializeDefault(void);
void socketExit(void);

/* Filesystem stubs (commonly referenced) */
Result romfsInit(void);
void romfsExit(void);

/* nro/nso etc. */
Result hosversionGet(uint32_t *out);

#ifdef __cplusplus
}
#endif

#endif /* MOCK_LIBNX_SWITCH_H */
