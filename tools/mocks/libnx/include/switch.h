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

/* ---------------------------------------------------------------------- */
/* P2 扩展 (post-6ad7d5f6 audrenInitialize NULL bug 实战驱动) — 2026-05-04   */
/* ---------------------------------------------------------------------- */

/* Audio renderer (audren) — charter 推荐 SDL2 audio, audren 仅作 mock 拦截入口 */
typedef struct AudioRendererConfig {
    uint32_t output_rate;       /* AudioRendererOutputRate; 真机要 32000 / 48000 */
    uint32_t num_voices;
    uint32_t num_effects;
    uint32_t num_sinks;
    uint32_t num_mix_buffers;
    uint32_t num_mix_objects;
    uint32_t reserved[8];
} AudioRendererConfig;

Result audrenInitialize(const AudioRendererConfig *config);
void   audrenExit(void);

/* Audio out (audout) — 旧 API, charter 同样推荐用 SDL2 audio 替代 */
Result audoutInitialize(void);
void   audoutExit(void);
Result audoutStartAudioOut(void);
void   audoutStopAudioOut(void);

/* Console debug (svc-based, 不抢 nwindow) */
typedef enum {
    debugDevice_NULL  = 0,
    debugDevice_SVC   = 1,
    debugDevice_CONSOLE = 2,
} DebugDevice;
void consoleDebugInit(DebugDevice dev);

/* Pad initialize (default 别名 — switchvideo 实战用 padInitializeDefault) */
void padInitializeDefault(PadState *pad);

/* Window / Framebuffer (libnx 现代 API) */
typedef struct NWindow {
    int placeholder;
} NWindow;

typedef struct Framebuffer {
    NWindow *win;
    uint32_t width;
    uint32_t height;
    uint32_t format;
    uint32_t buffer_count;
    uint8_t *fake_buffer;       /* mock 内部用的假帧缓冲指针 */
    uint32_t stride;
} Framebuffer;

#define PIXEL_FORMAT_RGBA_8888  1

NWindow *nwindowGetDefault(void);
Result   framebufferCreate(Framebuffer *fb, NWindow *win, uint32_t w, uint32_t h, uint32_t fmt, uint32_t buffers);
Result   framebufferMakeLinear(Framebuffer *fb);
uint8_t *framebufferBegin(Framebuffer *fb, uint32_t *out_stride);
void     framebufferEnd(Framebuffer *fb);
void     framebufferClose(Framebuffer *fb);

/* Hid 旧 API — charter 已禁用, mock 调用直接 abort 给 LLM 强反馈 */
void     hidScanInput(void);
uint64_t hidKeysDown(uint32_t controller);
uint64_t hidKeysHeld(uint32_t controller);
uint64_t hidKeysUp(uint32_t controller);

/* libnx 类型别名 (switchvideo main.cpp 用到) */
typedef uint32_t u32;
typedef int32_t  s32;

#ifdef __cplusplus
}
#endif

#endif /* MOCK_LIBNX_SWITCH_H */
