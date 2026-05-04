/* mock_libnx_audio.c — audren / audout host stubs.
 *
 * 设计意图（P2 — post-6ad7d5f6 audrenInitialize NULL 实战）：
 *   audrenInitialize 在真 Switch 上要求完整 AudioRendererConfig；传 NULL 必触发
 *   svcBreak 2168-0002 (x4a8). T1 (-Werror) 救不了 (NULL 编译期合法), T2 不救它
 *   (libnx-only API). 这里把 mock 做成"已知错误模式立即 abort + 输出诊断", 让
 *   host_test 阶段就能抓住, 反馈速度 < 1s.
 *
 * Charter 偏好层级 (switchvideo):
 *   1. 推荐: SDL2 audio (SDL_OpenAudioDevice + SDL_QueueAudio)
 *   2. 备选: audren (但要正确 config)
 *   3. 禁用: audout (旧 API)
 *
 * 这个 mock 实现:
 *   - audrenInitialize(NULL)        → abort (致死错误)
 *   - audrenInitialize(invalid cfg) → abort (output_rate=0 等明显错)
 *   - audrenInitialize(valid cfg)   → 返回 0 (host 上不真启动 audio service)
 *   - audoutInitialize              → 仅 stderr WARN, 返回 0 (charter 软偏好)
 */

#include "switch.h"

/* ---------------- audren ---------------- */

static int g_audren_init_count = 0;

Result audrenInitialize(const AudioRendererConfig *config) {
    if (!config) {
        fprintf(stderr,
            "[mock_libnx] FATAL: audrenInitialize(NULL) — "
            "AudioRendererConfig is required.\n"
            "On real Switch this triggers svcBreak 2168-0002 (x4a8).\n"
            "Either populate the AudioRendererConfig (output_rate=48000, num_voices=24, ...),\n"
            "or per charter use SDL2 audio (SDL_OpenAudioDevice + SDL_QueueAudio).\n");
        fflush(stderr);
        abort();
    }
    if (config->output_rate != 32000 && config->output_rate != 48000) {
        fprintf(stderr,
            "[mock_libnx] FATAL: AudioRendererConfig.output_rate=%u is invalid.\n"
            "Real Switch only accepts 32000 or 48000.\n",
            config->output_rate);
        fflush(stderr);
        abort();
    }
    if (config->num_voices == 0) {
        fprintf(stderr,
            "[mock_libnx] FATAL: AudioRendererConfig.num_voices=0.\n"
            "Real Switch requires num_voices >= 1.\n");
        fflush(stderr);
        abort();
    }
    g_audren_init_count++;
    return 0;
}

void audrenExit(void) {
    if (g_audren_init_count > 0) g_audren_init_count--;
}

/* ---------------- audout (charter 软禁) ---------------- */

static bool g_audout_init = false;

Result audoutInitialize(void) {
    if (!g_audout_init) {
        fprintf(stderr,
            "[mock_libnx] WARN: audoutInitialize — charter prefers SDL2 audio "
            "(SDL_OpenAudioDevice + SDL_QueueAudio); audout is the legacy API.\n");
        g_audout_init = true;
    }
    return 0;
}

void audoutExit(void) {
    g_audout_init = false;
}

Result audoutStartAudioOut(void) { return 0; }
void   audoutStopAudioOut(void)  { }
