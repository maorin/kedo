#!/usr/bin/env bash
# 验证 tools/mocks/libnx/ 的扩展 stub 行为正确.
# Run: bash scripts/smoke/mock_libnx_smoke.sh
# 注意: 不用 set -e — abort()/ASAN 触发的非零 exit 是测试预期, 不能让它中断脚本.

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INC="$REPO/tools/mocks/libnx/include"
SRC="$REPO/tools/mocks/libnx/src"
TMP="$(mktemp -d)"
trap "rm -rf $TMP" EXIT

CC="${CC:-cc}"
FLAGS="-fsanitize=address -g -O0 -Wno-unused-parameter -I$INC"
ALL_MOCKS="$SRC/mock_libnx.c $SRC/mock_libnx_audio.c $SRC/mock_libnx_video.c $SRC/mock_libnx_strict.c"

cd "$TMP"
PASS=0
FAIL=0

run_case() {
    local name="$1"; shift
    local expected_exit="$1"; shift
    local expected_marker="$1"; shift
    local src_code="$1"; shift

    echo "$src_code" > test.c
    if ! $CC $FLAGS $ALL_MOCKS test.c -o test 2>/tmp/build.log; then
        echo "[$name] FAIL — compile error:"
        tail -10 /tmp/build.log
        FAIL=$((FAIL + 1))
        return
    fi
    ./test > /tmp/run.log 2>&1
    local actual_exit=$?

    # expected_exit 支持: 具体码 / "nonzero" (任意非零) / "any" (任意)
    local pass="yes"
    case "$expected_exit" in
        nonzero)
            [ "$actual_exit" = "0" ] && pass="no — exit=0 (expected nonzero)"
            ;;
        any)  ;;
        *)
            [ "$actual_exit" != "$expected_exit" ] && pass="no — exit=$actual_exit (expected $expected_exit)"
            ;;
    esac
    if [ -n "$expected_marker" ] && ! grep -q "$expected_marker" /tmp/run.log; then
        pass="no — marker '$expected_marker' missing from output"
    fi

    if [ "$pass" = "yes" ]; then
        echo "[$name] PASS"
        PASS=$((PASS + 1))
    else
        echo "[$name] FAIL — $pass"
        head -5 /tmp/run.log
        FAIL=$((FAIL + 1))
    fi
}

# ============================================================
# Cases
# ============================================================

run_case "A1: audrenInitialize(NULL) → abort 2168-0002" 134 "audrenInitialize(NULL)" '
#include <switch.h>
int main(void) { audrenInitialize(NULL); return 0; }
'

run_case "A2: hidScanInput deprecated → abort" 134 "hidScanInput is deprecated" '
#include <switch.h>
int main(void) { hidScanInput(); return 0; }
'

run_case "A3: hidKeysDown deprecated → abort" 134 "hidKeysDown is deprecated" '
#include <switch.h>
int main(void) { uint64_t k = hidKeysDown(0); (void)k; return 0; }
'

run_case "A4: 正常 audren config → pass" 0 "test_a4 ok" '
#include <switch.h>
int main(void) {
    AudioRendererConfig cfg = { .output_rate = 48000, .num_voices = 24 };
    audrenInitialize(&cfg); audrenExit();
    printf("test_a4 ok\n"); return 0;
}
'

run_case "A5: AudioRendererConfig.output_rate=0 → abort" 134 "output_rate=0 is invalid" '
#include <switch.h>
int main(void) {
    AudioRendererConfig cfg = { .output_rate = 0, .num_voices = 24 };
    audrenInitialize(&cfg); return 0;
}
'

run_case "A6: AudioRendererConfig.num_voices=0 → abort" 134 "num_voices=0" '
#include <switch.h>
int main(void) {
    AudioRendererConfig cfg = { .output_rate = 48000, .num_voices = 0 };
    audrenInitialize(&cfg); return 0;
}
'

run_case "A7: framebuffer 流程 → pass" 0 "fb ok stride=" '
#include <switch.h>
int main(void) {
    Framebuffer fb;
    framebufferCreate(&fb, nwindowGetDefault(), 1280, 720, PIXEL_FORMAT_RGBA_8888, 2);
    framebufferMakeLinear(&fb);
    uint32_t stride = 0;
    uint8_t *p = framebufferBegin(&fb, &stride);
    if (!p || stride == 0) return 1;
    p[0] = 0xFF; p[stride * 720 - 1] = 0xFF;
    framebufferEnd(&fb); framebufferClose(&fb);
    printf("fb ok stride=%u\n", stride); return 0;
}
'

run_case "A8: framebuffer 越界 → ASAN heap-buffer-overflow" nonzero "heap-buffer-overflow" '
#include <switch.h>
int main(void) {
    Framebuffer fb;
    framebufferCreate(&fb, nwindowGetDefault(), 1280, 720, 1, 2);
    framebufferMakeLinear(&fb);
    uint32_t stride = 0;
    uint8_t *p = framebufferBegin(&fb, &stride);
    p[stride * 720 + 100] = 0xFF;
    return 0;
}
'

# A8 ASAN abort 的 exit code 在不同平台可能不同 (1 / 134 都见过)，宽松判:
# 重新跑一次 A8 看实际 exit
echo "$src_code" > test_a8.c
cat > test_a8.c << 'EOFA8'
#include <switch.h>
int main(void) {
    Framebuffer fb;
    framebufferCreate(&fb, nwindowGetDefault(), 1280, 720, 1, 2);
    framebufferMakeLinear(&fb);
    uint32_t stride = 0;
    uint8_t *p = framebufferBegin(&fb, &stride);
    p[stride * 720 + 100] = 0xFF;
    return 0;
}
EOFA8

run_case "A9: audoutInitialize → WARN + pass (charter 软偏好)" 0 "WARN: audoutInitialize" '
#include <switch.h>
int main(void) {
    audoutInitialize(); audoutExit();
    printf("audout ok\n"); return 0;
}
'

run_case "A10: padInitializeDefault (新别名) → pass" 0 "pad ok" '
#include <switch.h>
int main(void) {
    PadState pad;
    padConfigureInput(1, HidNpadStyleSet_NpadStandard);
    padInitializeDefault(&pad);
    padUpdate(&pad);
    printf("pad ok\n"); return 0;
}
'

# ============================================================

echo ""
echo "=== mock_libnx smoke: $PASS passed, $FAIL failed ==="
exit $FAIL
