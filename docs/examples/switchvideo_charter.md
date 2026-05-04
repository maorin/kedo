---
schema_version: 1
mutable: false
last_changed: 2026-04-26
last_change_reason: "Initial charter — frozen after switchvideo 6075f8ec drift incident"
project_kind: switch_homebrew

build:
  system: cmake
  must_have_files:
    - CMakeLists.txt
  forbidden_files:
    - Makefile
    - GNUmakefile
    - makefile
    - configure
    - configure.ac
    - Cargo.toml
    - setup.py
    - package.json
  command: "cmake -B build -S . -DCMAKE_TOOLCHAIN_FILE=$DEVKITPRO/cmake/Switch.cmake && cmake --build build --parallel"

artifact:
  target_name: switchvideo
  output_path: build/switchvideo.nro

deploy:
  command: "$DEVKITPRO/tools/bin/nxlink -a $SWITCH_IP build/switchvideo.nro"

coding_conventions:
  - "libnx 输入用 PadState API (padInitializeDefault / padUpdate / padGetButtonsDown), 不要用旧 hidScanInput / hidKeysDown"
  - "SDL2 头文件用 <SDL2/SDL.h>, 不是 <SDL.h>"
  - "网络初始化前必须 socketInitializeDefault()"
  - "DEVKITPRO 工具链 only — 不允许引入 host gcc / clang 编译"
  - "字体通过 romfs 打包 (LiberationSans-Regular.ttf), 路径 /fonts/...ttf"
  - "音频 SDL_OpenAudioDevice + SDL_QueueAudio, AUDIO_S16SYS, 预缓冲 1.5s"

forbidden_actions:
  - "introducing a 2nd build system (e.g. hand-written Makefile when CMake already exists)"
  - "renaming CMake target without updating artifact.target_name + deploy.command in the same change"
  - "removing -DCMAKE_TOOLCHAIN_FILE from build command (would degrade to host gcc)"
  - "rewriting an entire build/profile/CMakeLists file when a targeted edit suffices"
  - "writing API keys / secrets to source files"

# 结构化代码模式硬拦截 (ProfileGuard 在 file_write 时 grep, 命中拒写).
# coding_conventions 是给 LLM 看的软提示, 这里是工具层不可绕过的硬约束.
# 实战暴露过的 bug 模式必须在这里登记, 以防 LLM 忽视 conventions 又踩.
forbidden_patterns:
  # ─── audio: 必须用 SDL2, 不准用 libnx audren / audout ───
  - pattern: 'audrenInitialize\s*\(\s*NULL\s*\)'
    reason: "audrenInitialize 要求完整 AudioRendererConfig, 传 NULL 必触发 svcBreak 2168-0002 (实测 task 6ad7d5f6). charter 已规定用 SDL_OpenAudioDevice + SDL_QueueAudio."
    applies_to: "*.c,*.cpp,*.cc"
    severity: block
  - pattern: '\baudrenInitialize\b'
    reason: "charter 禁用 libnx audren, 要求 SDL2 audio (SDL_OpenAudioDevice + SDL_QueueAudio, AUDIO_S16SYS)."
    applies_to: "*.c,*.cpp,*.cc"
    severity: block
  - pattern: '\baudoutInitialize\b'
    reason: "charter 禁用 libnx audout, 要求 SDL2 audio."
    applies_to: "*.c,*.cpp,*.cc"
    severity: block
  # ─── input: 必须用 PadState, 不准用旧 hid API ───
  - pattern: '\bhidScanInput\b'
    reason: "charter 要求 PadState API (padInitializeDefault / padUpdate / padGetButtonsDown), 旧 hidScanInput 已弃用."
    applies_to: "*.c,*.cpp,*.cc"
    severity: block
  - pattern: '\bhidKeysDown\b'
    reason: "charter 要求 padGetButtonsDown, 不要用旧 hidKeysDown."
    applies_to: "*.c,*.cpp,*.cc"
    severity: block
  # ─── SDL header: 用 <SDL2/SDL.h> 不是 <SDL.h> ───
  - pattern: '#include\s*<SDL\.h>'
    reason: "charter 要求 SDL2 头文件用 <SDL2/SDL.h>, devkitPro portlibs 路径下 SDL2 子目录."
    applies_to: "*.c,*.cpp,*.cc,*.h,*.hpp"
    severity: block

# Charter 期望的外部依赖 (deliverable). provider=task 表示 task 必须把这个东西写进 changed_files,
# Reviewer 的 deliverable_completeness 维度会检查实际是否提供; provider=user 表示由用户自己准备.
external_services:
  - name: video_server.py
    provider: task
    description: "运行在 LAN 主机 (192.168.1.8) 上的 HTTP/转码服务器, switch 端拉视频"
    expected_path: "scripts/video_server.py"
---

# Project Charter — switchvideo

This is a Nintendo Switch homebrew application that streams videos from an NFS share
(served by a companion `video_server.py` running on the LAN). It uses **CMake** with
the devkitPro Switch toolchain as the **single, authoritative build system**.

## Why this charter exists

On 2026-04-25 task `6075f8ec`, while attempting to fix Switch error code 2168-0002,
the agent introduced a hand-written devkitPro `switch_rules` Makefile *alongside* the
existing CMakeLists.txt, renamed the CMake target, and rewrote `profile.build.command`
to point at the Makefile path — without removing the CMake setup. The project ended
up with two conflicting build systems, the Reviewer never got a chance to gate the
damage (it only gates `commit_candidate`, and the build never reached green), and the
task hit `max_turns` 95% before being killed. This charter freezes the contract that
was implicitly violated.

## Hard rules (machine-enforced via ProfileGuard)

- The project uses **CMake**, not Makefile / Cargo / npm / setuptools. Period.
- The CMake target is **`switchvideo`**. Any rename must propose a charter change.
- `deploy.command` references `build/switchvideo.nro`. Renaming target without
  updating deploy.command in the same approved patch is a violation.

## Soft rules (Reviewer cites these)

See `coding_conventions` and `forbidden_actions` above. Reviewer should cite
violations as `charter:coding_conventions[index]` or `charter:forbidden_actions[index]`.

## How to change this charter

The charter is `mutable: false`. The agent must call the `propose_charter_change`
tool with the field path and a clear reason; the user approves/rejects on the
dashboard. Direct edits to this file by the agent are blocked by ProfileGuard.
