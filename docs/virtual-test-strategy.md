# 虚拟环境测试策略 — 让 kedo 自闭环

> **状态**：2026-05-04 三层全部 kedo-侧落地（A/B/C）。仍需用户侧准备：
> - T1 strict_warnings：**默认空**，由 LLM 在生成 profile 时填或人工补
> - T2 host_test：mock_libnx 样板已提供（`tools/mocks/libnx/`），具体项目要写自己的 host_main.c
> - T3 emulator：Ryubing 二进制 + Switch firmware 必须用户在目标机器手动准备（见末尾"Ryubing 安装步骤"）
>
> 落地代码：
> - `tools/build_tool.py` — strict_warnings 注入 + warning surface
> - `tools/static_check.py` — 新工具（clang-tidy/pyright/cppcheck/eslint wrapper）
> - `tools/host_test.py` — 新工具，T2 host mock + ASAN/UBSAN，build 成功后自动调
> - `tools/emulator_test.py` — 新工具，T3 emulator wrapper，commit_candidate gate
> - `tools/mocks/libnx/` — Switch homebrew 的 host mock 桩样板
> - `core/project_profile.py` — profile 加 `strict_warnings` / `host_test` / `emulator` 字段
> - `core/react_agent.py` — build 成功后自动调 host_test 回灌结果
> - `tools/commit_candidate_tool.py` — Reviewer 通过后再过 emulator gate

## 为什么需要

kedo 的 ReAct loop 速度取决于反馈速度：
- LLM 写代码 → build → test → eval → 决定下一步
- 单步反馈 < 30s 时 LLM 能高效迭代修复
- 反馈 > 5 分钟（人工真机）时 LLM 失去耐心、容易 respond 早撤、用户体验崩

**实战观察**（switchvideo 项目）：
- 每个 commit 候选都要人工部署到 Switch 真机 → 跑 → 报错码
- "Error code 2168-0002"（svcBreak / null deref）这种**基础内存问题**也要人工跑一轮才暴露
- LLM 拿到错误码时早就脱离上下文，要重新读全部文件
- 6 个常见 bug 类型里，4 个其实在编译期或 ASAN 就能抓

**目标**：把"基础问题"留在虚拟环境闭环里 LLM 自己修；只让"真机特定行为"（GPU/输入设备/真实平台 quirk）走人工。

## 三层模型

| Tier | 时机 | 反馈速度 | 抓的 bug | 工程量 |
|---|---|---|---|---|
| **T1 编译时强化** | 每次 build | <1s | 缺声明、签名漂移、未初始化变量、未使用值 | 配置项 + 工具 30 分钟 |
| **T2 宿主机 mock** | build 通过后 | 1-10s | null deref、内存越界、stack overflow、初始化顺序错、协议错 | 半天-1 天/平台 |
| **T3 真模拟器** | commit candidate 前最后一关 | 10s-2min | 平台特定 svcBreak / 服务交互 / GPU 真渲染失败 | 1-3 天/平台 + firmware 准备 |

**核心原则：T1 + T2 永远开，T3 只在 commit 候选关卡跑一次**。LLM 每轮都跑 T3 会拖死整个 ReAct loop。

## Tier 1 — 编译时强化（始终值得做）

每个项目的 `Makefile` / `CMakeLists.txt` 都该开严格警告。kedo 的 BuildTool 已经会读 stderr，但 warning 不会进 stderr，需要单独 surface。

**通用 flag 集**：
- C/C++: `-Wall -Wextra -Werror -Wundef -Wmissing-prototypes -Wimplicit-function-declaration`
- Rust: 默认就严，可加 `-D warnings`
- Python: `mypy --strict` + `ruff check --select=ALL`
- TypeScript: `"strict": true` + `--noUnusedLocals --noUnusedParameters`

**额外静态检查**：
- C/C++: `clang-tidy` 全 checks 或 `cppcheck`
- Python: `pyright` strict + `bandit`（安全）
- 跨语言: `semgrep` 规则集

**kedo 集成路径**：
1. ProjectProfile 加字段 `strict_warnings: list[str]` — 保存项目特化的 warning flags
2. BuildTool 在 build command 末尾自动追加（如果没显式禁用）
3. BuildTool 把 build 输出里的 `warning:` 行也当 LLM 反馈（不只 error），让 LLM 能在编译通过的边缘修代码
4. 加可选的 `tools/static_check.py` 工具：调 clang-tidy / pyright，结构化输出

**ROI**：30 分钟改 BuildTool + profile，能拦住 60-80% 的"声明/类型/未初始化"类 bug。switchvideo 的 b943c9d0、3786a985 这两个 task 卡住的 bug 都属于这类。

## Tier 2 — 宿主机 mock 编译运行（性价比最高）

**思路**：把目标平台的 SDK API 用 stub 实现一份能在 Linux/Mac 上编过的 mock，源代码不改一行，host 编一遍跑出二进制，用 ASAN/UBSAN 跑测试数据。

**抓得到的**：
- null pointer dereference
- buffer overflow / use-after-free / double free
- 未初始化内存读取
- stack overflow
- 协议层逻辑错（mock 出协议端就行）
- 状态机错误转换
- 数值溢出

**抓不到的**：
- GPU shader / framebuffer 真实渲染
- 平台特定信号量 / 中断
- 真实 I/O 延迟相关 race
- 平台 OS 服务初始化顺序的 quirk

**按平台模板**：

| 目标平台 | mock 内容 | 主测器 | 备注 |
|---|---|---|---|
| **Switch (libnx)** | `mock_libnx.h` 桩 200+ API 为 stdout printf | gcc + ASAN | hidGet*/svc*/audio* 全 stub |
| **嵌入式 ARM no-OS** | HAL 层 stub（GPIO/SPI/UART → 文件） | gcc + ASAN | 输入用 stdin 模拟外设事件 |
| **iOS app** | UIKit / Foundation stub | clang + simulator headless | 用 iOS Simulator 即可，无需 mock |
| **Android NDK** | JNI stub | gcc + ASAN | UI 层走 Robolectric |
| **Web 前端** | jsdom / happy-dom | Vitest / Jest + jsdom | 已成熟 |
| **后端服务** | 数据库/外部 API mock | pytest + asan | docker-compose 起依赖 |

**kedo 集成路径**：
1. ProjectProfile 加字段 `host_test`：`{enabled, build_command, run_command, expected_exit_code}`
2. 加新工具 `host_test`：执行 mock 编译 + 运行 + 抓 ASAN/sanitizer 输出
3. ReactAgent 在 build 通过后自动调一次（如果 profile 启用）
4. ASAN 报告里的"具体哪个文件哪一行 use-after-free"作为 LLM 下一轮 prompt

**ROI**：mock 层一次性写好（1 天），之后所有 .c/.cpp 改动都能秒级抓 80% 内存类 bug。switchvideo 的"Error code 2168-0002"这种 svcBreak 80% 是 null deref，mock 层 + ASAN 能抓到。

## Tier 3 — 真模拟器（最后一关，慎重）

**只推荐在 commit_candidate 关卡前跑一次**，不参与 ReAct 主循环。

**按平台**：

| 平台 | 模拟器 | Linux 支持 | Headless | 兼容性 | 法律 |
|---|---|---|---|---|---|
| **Switch** | Ryubing（Ryujinx 主分叉） | ✅ AppImage/build | xvfb 包装 | 70-90% | 灰，需自己 dump firmware |
| **iOS** | Xcode iOS Simulator | ❌（仅 macOS） | `xcrun simctl boot/install/launch` | 99% | 苹果许可 |
| **Android** | Android Studio AVD / Genymotion | ✅ | `emulator -no-window` | 95% | 免费 |
| **Embedded ARM** | QEMU system-mode | ✅ | 默认 headless | 看 BSP | 完全免费 |
| **嵌入式 RTOS (Zephyr/FreeRTOS)** | renode / QEMU | ✅ | 默认 headless | 看 BSP | 免费 |
| **macOS app** | macOS sandbox + headless run | ✅（仅 macOS） | LSUIElement | 100% | 苹果许可 |
| **Web 全链路** | Playwright headless Chromium | ✅ | 默认 | 99% | 免费 |
| **PS4/PS5 / Xbox / 老主机** | 几乎没有靠谱选项 | - | - | - | - |

**Switch 特例（Ryubing）实操**：
```bash
# 安装（一次）
sudo apt install dotnet-sdk-8.0 xvfb
git clone https://github.com/Ryubing/Ryujinx ~/Ryubing
cd ~/Ryubing && dotnet build -c Release -p:ExtraDefineConstants=DISABLE_UPDATER

# 准备 firmware（一次）— 用 Lockpick_RCM 从自己 Switch dump
# 把 prod.keys + firmware/*.nca 放进 ~/.config/Ryujinx/

# 每次 commit 候选跑一次
xvfb-run -a -s "-screen 0 1280x720x24" \
  ~/Ryubing/src/Ryujinx.Headless.SDL2/bin/Release/Ryujinx.Headless.SDL2 \
  --memory-manager-mode HostMappedUnsafe \
  /path/to/build.nro 2>&1 | tee /tmp/nro_run.log &
EMULATOR_PID=$!
sleep 30
kill $EMULATOR_PID
# 在 nro_run.log 里 grep "svcBreak|Result code|panic|0x[0-9a-f]+"
```

**kedo 集成路径**：
1. ProjectProfile 加字段 `emulator`：`{enabled, command_template, timeout_s, success_patterns, crash_patterns}`
2. 新工具 `emulator_test(artifact_path)`：包装 + timeout + 模式匹配
3. 在 commit_candidate 工具内部调用，作为额外 gate（pre-commit gate 已有 Reviewer 审查；emulator 是另一道客观闸）
4. 失败时把 console output / crash signature 写入 `ToolResult.error`

**Switch Ryubing 已知坑**：
- Vulkan 后端在 Linux + xvfb 下经常黑屏 → 用 OpenGL（默认）
- libcurl 走 socket 模拟实现，复杂网络场景可能超时
- Audio 在 xvfb 下无声但不崩
- 启动 5-30s + 跑 30s + 退出 5s ≈ 40-70s/次 → 不能放进 ReAct 主循环

## 选 tier 的决策表

| 我现在卡的是什么 | 选 |
|---|---|
| 编译错（缺声明/签名错） | T1 — 别浪费 T2/T3 |
| 编过了但运行 segfault | T2 mock + ASAN |
| 编过了 ASAN 也过了但目标平台崩 | T3 模拟器 |
| 单纯逻辑 bug（算法错） | 单元测试，连 T2 都不用 |
| 业务流程 / API 集成 | docker-compose 集成测试，T2 范畴 |
| GPU/shader/UI 真实渲染 | T3 模拟器 + 截图比对，或人工真机 |

## 为什么不无脑上 T3

**T3 慢且不可靠**：
- 启动开销 5-30s（vs T2 < 1s）
- 模拟器兼容性永远 < 100%
- 真机/模拟器行为差异本身就是 bug 来源（"在 Ryubing 跑通真机崩"也常见）
- LLM 拿到模拟器报告时上下文已飘远

**正确姿势**：T2 抓 80% bug 在反馈圈内修，T3 当"出门前最后照镜子"用一次。

## 不推荐的几条死路

- **QEMU user-mode 跑 Switch 二进制**：Switch 不是 Linux，syscall 不兼容
- **Docker 容器跑 Switch homebrew**：同上
- **WSL2 跑 Switch 模拟器**：Vulkan 路径在 WSL 不稳，audio 没驱动
- **找网上下载 firmware**：违法 + 病毒高发，必须自己 dump

## kedo 当前路径推荐

按 ROI 分阶段：

**Phase A（明天可做，~30 分钟）— Tier 1 全面铺开**
- BuildTool 把 stderr 里的 `warning:` 行也作为 LLM 反馈
- ProjectProfile 加 `strict_warnings`，build command 自动追加 `-Werror -Wall -Wextra` 等
- 加 `tools/static_check.py` 工具（clang-tidy / pyright wrapper）

**Phase B（本周可做，~1-2 天）— Tier 2 落地一个平台**
- 选 switchvideo 做样板（mock_libnx + Linux + ASAN）
- 加 `tools/host_test.py` 工具
- ReactAgent 在 build 通过后自动调
- 验证能抓到 task ee55f537 那种 svcBreak 类 bug

**Phase C（验证 Phase B 必要后再做，~1-3 天/平台）— Tier 3 接入**
- Ryubing 装到 192.168.1.8（含 firmware 准备）
- 加 `tools/emulator_test.py` 工具
- 接入 commit_candidate pre-commit gate（和 Reviewer 并列）
- 单 task 启动一次，不进 ReAct 主循环

## 一句话总结

> 自动化测试的 ROI 不在"测得多真"，而在"反馈多快"。T1 + T2 占 80% 价值，T3 只是最后一道客观闸。**先把 mock 层建起来，模拟器之后再说**。

## Ryubing 安装步骤（T3 启用前的用户准备）

> kedo 不能也不该自动做这步：firmware 必须用户从自己 Switch dump，不能通过网络获取。

```bash
# 1. 装 .NET 8 + xvfb
sudo apt install -y dotnet-sdk-8.0 xvfb

# 2. 拉源码 + build（约 10 分钟）
git clone https://github.com/Ryubing/Ryujinx ~/Ryubing
cd ~/Ryubing
dotnet build -c Release -p:ExtraDefineConstants=DISABLE_UPDATER

# 3. 准备 Switch firmware（必须自己 Switch dump）
#    a. 在自己 Switch 上跑 Lockpick_RCM 拿到 prod.keys + title.keys
#    b. 在自己 Switch 上 dump firmware/*.nca
#    c. 把这些文件放到 ~/.config/Ryujinx/ 对应目录
#    详见 https://ryujinx.org/quickstart

# 4. 验证可启动一个 nro
xvfb-run -a -s "-screen 0 1280x720x24" \
  ~/Ryubing/src/Ryujinx.Headless.SDL2/bin/Release/Ryujinx.Headless.SDL2 \
  --memory-manager-mode HostMappedUnsafe /path/to/test.nro 2>&1 | head -50
```

### 然后在 profile.emulator 里启用

```json
{
  "emulator": {
    "enabled": true,
    "command_template": "xvfb-run -a -s '-screen 0 1280x720x24' /home/user/Ryubing/src/Ryujinx.Headless.SDL2/bin/Release/Ryujinx.Headless.SDL2 --memory-manager-mode HostMappedUnsafe {artifact}",
    "timeout_s": 60,
    "success_patterns": ["consoleInit complete", "main loop entered"],
    "crash_patterns": ["svcBreak", "Result code 0x[0-9a-f]+", "panic", "fatal exception"],
    "required": false
  }
}
```

`required=false`：emulator 失败只 warning，不阻塞 commit_candidate（开发期推荐）；
`required=true`：emulator 失败 reject candidate，必须迭代修复（接近发布时启用）。

## 当前落地状态对照

| Tier | kedo 侧 | 用户侧 |
|---|---|---|
| **T1** | `BuildTool` 注入 strict_warnings + surface warning 行；`static_check` 工具可用 | profile 里手填 `strict_warnings.cflags`（或让 LLM 生成 profile 时自填） |
| **T2** | `host_test` 工具 + ReactAgent build 成功自动调 + `mocks/libnx/` 样板 | 写项目自己的 `tests/host_mock/host_main.c` 驱动业务代码 |
| **T3** | `emulator_test` 工具 + `commit_candidate` gate | 在 192.168.1.8 装 Ryubing + dump firmware + 在 profile.emulator 填 command_template |

## 后续切片（实战暴露后再做）

- T1：profile 自动补强（首次 build 失败如果是 warning-as-error 类，Reviewer 建议加 `-Werror`）
- T2：mocks 扩充（按平台一份）— 当前只有 libnx，后续按需要加 freertos/zephyr/jni 等
- T3：emulator 截图比对（可选，用于检测 GPU/UI 真渲染回归）
