# mock_libnx — Switch homebrew 的 host 测试桩

供 kedo T2 (host_test) 用：把 libnx + libnx-portlibs 的常用 API 桩成 host 可编译/运行的版本，
让业务代码能在 Linux/Mac 上用 ASAN/UBSAN 跑出基础内存 bug。

## 怎么用

复制（或 symlink）`include/` 和 `src/` 到你的项目 `tests/host_mock/` 下，写一个 `host_main.c`
驱动业务逻辑，然后在 profile.host_test 里：

```json
{
  "host_test": {
    "enabled": true,
    "mock_dir": "tests/host_mock",
    "build_command": "gcc -fsanitize=address,undefined -Iinclude -I../../source -g -O0 src/*.c host_main.c ../../source/*.c -o host_test 2>&1",
    "run_command": "./host_test",
    "expected_exit_code": 0,
    "timeout_s": 30,
    "auto_run_after_build": true
  }
}
```

## 覆盖范围

> **设计原则**: 三类 stub
> - **A 类 abort**: 已知错误模式（`audrenInitialize(NULL)` / `hidScanInput`）host 上立即 `abort()`+诊断, 不等真机才暴露
> - **B 类 WARN**: charter 软偏好的 (audout 旧 API), 仅 `stderr` 提示, 流程能继续
> - **C 类 stub**: 正常 API, 返回合理值让业务流程跑通

### `mock_libnx.c` (基础 — hello-world Switch homebrew)

| API 类别 | 类型 | 行为 |
|---|---|---|
| `consoleInit/consoleClear/consoleUpdate/consoleExit` | C | 转写到 stdout |
| `appletMainLoop` | C | 第 N 次调用返回 false（防死循环），N 默认 1000 可改 |
| `padConfigureInput / padInitializeAny / padUpdate / padGetButtons*` | C | 返回模拟键值（默认无按键，可通过 `mock_libnx_set_buttons` 注入） |
| `svcSleepThread` | C | usleep |
| `socketInitializeDefault / socketExit` | C | 返回 0 |
| `romfsInit / romfsExit` | C | 返回 0 |
| `hosversionGet` | C | 返回 fake "11.0.0" |

### `mock_libnx_audio.c` (P2 — switchvideo `6ad7d5f6` 实战驱动)

| API | 类型 | 行为 |
|---|---|---|
| `audrenInitialize(NULL)` | **A** | `abort` + 输出 svcBreak 2168-0002 诊断 |
| `audrenInitialize(cfg with output_rate=0/non-32k/non-48k)` | **A** | `abort` + 提示合法值 |
| `audrenInitialize(cfg with num_voices=0)` | **A** | `abort` |
| `audrenInitialize(valid cfg)` | C | 返回 0（host 不真启 audio） |
| `audrenExit` | C | 计数递减 |
| `audoutInitialize / audoutExit` | **B** | WARN 提示 charter 偏好 SDL2，返回 0 |
| `audoutStartAudioOut / audoutStopAudioOut` | C | 返回 0 |

### `mock_libnx_video.c` (P2 — nwindow / framebuffer)

| API | 类型 | 行为 |
|---|---|---|
| `nwindowGetDefault` | C | 返回静态 NWindow 指针 |
| `framebufferCreate(fb=NULL/win=NULL)` | **A** | `abort` |
| `framebufferCreate` (在 console 已 init 的 default win 上) | **B** | WARN（历史 4 连漂移真因），返回 0 |
| `framebufferCreate(valid)` | C | calloc 出 1280×720×4 假帧缓冲 |
| `framebufferMakeLinear` | C | 返回 0 |
| `framebufferBegin(uninit fb)` | **A** | `abort` |
| `framebufferBegin(valid)` | C | 返回假帧缓冲指针 + stride |
| `framebufferEnd` | C | noop |
| `framebufferClose` | C | free 假帧缓冲 |
| `consoleDebugInit(SVC)` | C | noop（不抢 nwindow） |
| `padInitializeDefault` | C | 同 `padInitializeAny` |

### `mock_libnx_strict.c` (P2 — 已废弃 API 强 abort)

| API | 类型 | 行为 |
|---|---|---|
| `hidScanInput` | **A** | `abort` + 提示用 PadState API |
| `hidKeysDown / hidKeysHeld / hidKeysUp` | **A** | `abort` + 提示用 `padGetButtons*` |

### 未覆盖（按需扩展）

GPU shader / fsdev / fs / 高级 HID（陀螺仪/加速度计/震动）/ IPC / sm 服务交互
/ amssphinx / nim / set / friends ... 这些需要项目级 mock 自行扩展。

## 验证

```bash
bash scripts/smoke/mock_libnx_smoke.sh
```

10 个测试 case：A1-A6 audio 类边界，A7-A8 framebuffer 流程 + 越界，A9 audout WARN，A10 padInitializeDefault。

## 不要做的事

- 不要尝试 mock 整个 libnx — 太大且失真。只 mock 业务用到的 API。
- 不要在 mock 里塞复杂逻辑 — mock 的作用是"让 host 链接通"，业务逻辑跑自身代码用 ASAN 抓 bug。
- mock 不能替代真模拟器（T3）。GPU/svcBreak 等平台特定行为只有 T3 抓得到。
