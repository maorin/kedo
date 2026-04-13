"""
平台知识库 — 按 profile.type 提供 CMakeLists 模板和开发规范

设计理念：
  - 已知平台（switch_homebrew 等）用手工维护的知识，准确可靠
  - 未知平台返回 None，由 LLM 自行推断（G1 的库清单仍然生效）
  - 每个平台一个 dict，包含 cmake_template + pitfalls
  - 模板用 {{PROJECT_NAME}} 占位，agent_loop 替换后注入 code_generate
"""
from __future__ import annotations

from typing import Optional


def get_platform_knowledge(profile_type: str) -> Optional[dict]:
    """
    根据 profile.type 返回平台知识，找不到返回 None。

    返回: {
        "cmake_template": str | None,   # CMakeLists.txt 骨架模板
        "pitfalls": str,                 # 平台特定的开发规范和陷阱
    }
    """
    # 规范化 type 名：小写 + 去空格
    key = (profile_type or "").lower().strip().replace(" ", "_").replace("-", "_")
    return _PLATFORMS.get(key)


# ==============================================================
# Switch Homebrew (devkitPro / libnx)
# ==============================================================

_SWITCH_HOMEBREW = {
    "cmake_template": """\
cmake_minimum_required(VERSION 3.13)
project({{PROJECT_NAME}} LANGUAGES C CXX)

# devkitPro toolchain file handles compiler/sysroot/platform flags.
# Do NOT manually set CMAKE_C_COMPILER or CMAKE_CXX_COMPILER.
# Do NOT add -specs=switch.specs (toolchain file already does this).

# Find packages via pkg-config (devkitPro portlibs)
include(FindPkgConfig)
pkg_check_modules(SDL2 REQUIRED IMPORTED_TARGET sdl2)
pkg_check_modules(SDL2_IMAGE IMPORTED_TARGET SDL2_image)
pkg_check_modules(SDL2_TTF IMPORTED_TARGET SDL2_ttf)
pkg_check_modules(CURL IMPORTED_TARGET libcurl)

add_executable({{PROJECT_NAME}}
    # === LLM: list all .cpp/.c source files here ===
    src/main.cpp
)

# romfs support (for bundled assets like fonts)
# Uncomment if project has a romfs/ directory:
# dkp_add_asset_target({{PROJECT_NAME}}_romfs romfs)
# nx_add_romfs({{PROJECT_NAME}} {{PROJECT_NAME}}_romfs)

target_include_directories({{PROJECT_NAME}} PRIVATE src)

# === LLM: add/remove libraries as needed ===
# IMPORTANT: static link order matters! Dependencies go AFTER dependents.
# Common order: app libs -> SDL2 libs -> system libs -> nx -> m
target_link_libraries({{PROJECT_NAME}}
    PkgConfig::SDL2
    $<$<BOOL:${SDL2_TTF_FOUND}>:PkgConfig::SDL2_TTF>
    $<$<BOOL:${SDL2_IMAGE_FOUND}>:PkgConfig::SDL2_IMAGE>
    $<$<BOOL:${CURL_FOUND}>:PkgConfig::CURL>
    # Direct libs (when pkg-config target not available):
    # freetype bz2 jpeg png16 webp z
    # EGL glapi drm_nouveau
    nx m
)

# Package into .nro
nx_create_nro({{PROJECT_NAME}}
    # NACP metadata (no quotes around arguments!)
    NACP_NAME "{{PROJECT_NAME}}"
    NACP_AUTHOR "Developer"
    NACP_VERSION "1.0.0"
    # Uncomment for custom icon:
    # ICON ${CMAKE_SOURCE_DIR}/assets/icon.jpg
    # Uncomment for romfs:
    # ROMFS {{PROJECT_NAME}}_romfs
)
""",

    "pitfalls": """\
## Switch Homebrew (devkitPro) Development Rules

1. **Headers**: Use `<SDL2/SDL.h>`, NOT `<SDL.h>`. Same for SDL2_image, SDL2_ttf.
2. **Input API**: Use modern PadState API (`padInitializeDefault`, `padUpdate`, \
`padGetButtonsDown`). Do NOT use deprecated `hidScanInput`/`hidKeysDown`.
3. **Network**: Call `socketInitializeDefault()` BEFORE any curl/network calls. \
Call `socketExit()` on cleanup.
4. **Link order**: Static libraries must be in dependency order. \
Dependents first, dependencies last. `nx` and `m` always go last.
5. **No -specs=switch.specs**: The CMake toolchain file already adds this. \
Adding it again causes duplicate symbol errors.
6. **No manual CMAKE_C_COMPILER**: The toolchain file sets this. \
Setting it manually conflicts with cross-compilation.
7. **elf2nro NACP**: Do NOT quote --nacp= arguments in CMake custom commands. \
CMake VERBATIM causes double-escaping.
8. **SDL audio**: Use `AUDIO_S16SYS` (system-native byte order), not `AUDIO_S16LSB`. \
Pre-buffer 1-2 seconds before `SDL_PauseAudioDevice(dev, 0)`.
9. **Icon files**: Must be real JPEG/PNG files, not scripts that generate them. \
Use ImageMagick `convert` to create placeholder icons.
10. **Text rendering**: Use SDL2_ttf with fonts loaded from romfs. \
Font path on Switch: `romfs:/fonts/YourFont.ttf`.
""",
}


# ==============================================================
# 平台注册表
# ==============================================================

_PLATFORMS: dict[str, dict] = {
    "switch_homebrew": _SWITCH_HOMEBREW,
    "switch": _SWITCH_HOMEBREW,
    "nintendo_switch": _SWITCH_HOMEBREW,
    # 未来扩展：
    # "android_native": _ANDROID_NATIVE,
    # "esp_idf": _ESP_IDF,
    # "raspberry_pi": _RASPBERRY_PI,
}
