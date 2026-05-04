/* mock_libnx_strict.c — abort 类 stub: charter 已禁用 / 已废弃 / 已知 svcBreak 触发器.
 *
 * 设计意图（P2）：把"已知错误模式"在 host_test 阶段就 abort, 让 LLM 不用等装到真机
 * 才发现错. 反馈速度 < 1s, file:line 由 ASAN frame 给出.
 *
 * 列表来源:
 *   - charter coding_conventions ("libnx 输入用 PadState API")
 *   - charter forbidden_patterns (实战暴露的 bug 模式)
 *   - libnx 源码标注的 deprecated API
 *
 * 注意: audrenInitialize NULL / framebufferBegin NULL 的 abort 在 audio.c / video.c 里,
 * 这里只放"任何调用都该 abort"的 deprecated API.
 */

#include "switch.h"

void hidScanInput(void) {
    fprintf(stderr,
        "[mock_libnx] FATAL: hidScanInput is deprecated (libnx 4.x+).\n"
        "Charter 要求 PadState API:\n"
        "  PadState pad;\n"
        "  padConfigureInput(1, HidNpadStyleSet_NpadStandard);\n"
        "  padInitializeDefault(&pad);\n"
        "  padUpdate(&pad);\n"
        "  if (padGetButtonsDown(&pad) & PadButton_A) { ... }\n");
    fflush(stderr);
    abort();
}

uint64_t hidKeysDown(uint32_t controller) {
    (void)controller;
    fprintf(stderr,
        "[mock_libnx] FATAL: hidKeysDown is deprecated.\n"
        "Use padGetButtonsDown(PadState*).\n");
    fflush(stderr);
    abort();
}

uint64_t hidKeysHeld(uint32_t controller) {
    (void)controller;
    fprintf(stderr,
        "[mock_libnx] FATAL: hidKeysHeld is deprecated.\n"
        "Use padGetButtons(PadState*).\n");
    fflush(stderr);
    abort();
}

uint64_t hidKeysUp(uint32_t controller) {
    (void)controller;
    fprintf(stderr,
        "[mock_libnx] FATAL: hidKeysUp is deprecated.\n"
        "Use padGetButtonsUp(PadState*).\n");
    fflush(stderr);
    abort();
}
