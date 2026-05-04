/* mock_libnx_video.c — nwindow / framebuffer host stubs.
 *
 * 设计意图（P2）：让 switchvideo main.cpp 类的 framebuffer 渲染流程能在 host 编通,
 * framebufferBegin 返回一块假帧缓冲, framebufferEnd 静默返回. 实际像素不渲染 (host
 * 没显示器), 但业务逻辑能跑完一轮 appletMainLoop, host_test + ASAN 能抓"画到帧缓冲
 * 时越界 / null deref" 类 bug.
 *
 * 已知坑 (历史 task 4 连漂移):
 *   consoleInit 与 framebufferCreate 共抢 default nwindow → 真机崩.
 *   这里 mock 不抢, 但加 WARN 提醒 LLM.
 */

#include "switch.h"

#define MOCK_FB_WIDTH   1280
#define MOCK_FB_HEIGHT  720
#define MOCK_FB_BPP     4   /* RGBA8 */

static NWindow      g_default_nwindow = {0};
static bool         g_console_init_done = false;   /* 见 mock_libnx.c consoleInit */

NWindow *nwindowGetDefault(void) {
    return &g_default_nwindow;
}

Result framebufferCreate(Framebuffer *fb, NWindow *win, uint32_t w, uint32_t h, uint32_t fmt, uint32_t buffers) {
    if (!fb) {
        fprintf(stderr, "[mock_libnx] FATAL: framebufferCreate(fb=NULL)\n");
        fflush(stderr);
        abort();
    }
    if (!win) {
        fprintf(stderr, "[mock_libnx] FATAL: framebufferCreate(win=NULL) — "
                        "use nwindowGetDefault() first\n");
        fflush(stderr);
        abort();
    }
    /* 真机上若 console 已 init 在同一 nwindow → 资源冲突. 这里仅 WARN. */
    if (g_console_init_done && win == &g_default_nwindow) {
        fprintf(stderr,
            "[mock_libnx] WARN: framebufferCreate on default nwindow after consoleInit "
            "— historical bug pattern: console + framebuffer fight over default nwindow "
            "→ svcBreak 2168-0002. Use consoleDebugInit(debugDevice_SVC) instead, or "
            "create framebuffer FIRST.\n");
    }
    fb->win = win;
    fb->width = w ? w : MOCK_FB_WIDTH;
    fb->height = h ? h : MOCK_FB_HEIGHT;
    fb->format = fmt;
    fb->buffer_count = buffers;
    fb->stride = fb->width * MOCK_FB_BPP;
    /* 一次性分配假帧缓冲, framebufferEnd 不释放, framebufferClose 释放 */
    fb->fake_buffer = (uint8_t *)calloc((size_t)fb->stride * fb->height, 1);
    if (!fb->fake_buffer) {
        fprintf(stderr, "[mock_libnx] framebufferCreate: alloc failed\n");
        return 0xC8A5;   /* fake error */
    }
    return 0;
}

Result framebufferMakeLinear(Framebuffer *fb) {
    (void)fb;
    return 0;
}

uint8_t *framebufferBegin(Framebuffer *fb, uint32_t *out_stride) {
    if (!fb || !fb->fake_buffer) {
        fprintf(stderr, "[mock_libnx] FATAL: framebufferBegin on uninit Framebuffer "
                        "— call framebufferCreate first\n");
        fflush(stderr);
        abort();
    }
    if (out_stride) *out_stride = fb->stride;
    return fb->fake_buffer;
}

void framebufferEnd(Framebuffer *fb) {
    (void)fb;
    /* 真机会 swap buffer + present, host 上无意义. 静默. */
}

void framebufferClose(Framebuffer *fb) {
    if (fb && fb->fake_buffer) {
        free(fb->fake_buffer);
        fb->fake_buffer = NULL;
    }
}

/* consoleDebugInit — 不抢 nwindow, 只配置 debug 输出渠道; mock 静默 */
void consoleDebugInit(DebugDevice dev) {
    (void)dev;
    /* SVC 模式: stderr 已经是 stdout, 无需特别处理 */
}

/* padInitializeDefault — 与 padInitializeAny 行为相同 (mock 层不区分) */
void padInitializeDefault(PadState *pad) {
    padInitializeAny(pad);
}

/* mock_libnx.c 里的 consoleInit 改成在调用时设置全局 flag, 让 framebufferCreate
 * 能检测到 console + framebuffer 抢 nwindow 模式. 但 mock_libnx.c 已经写完且
 * 这次不动它, 改用一个轻量 setter (host_main 测试可显式调以模拟此场景). */
void mock_libnx_set_console_initialized(bool flag) {
    g_console_init_done = flag;
}
