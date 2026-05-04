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

## 覆盖范围（最小可跑集合）

| API 类别 | 状态 | 行为 |
|---|---|---|
| `consoleInit/consoleClear/consoleUpdate/consoleExit` | stub | 转写到 stdout |
| `appletMainLoop` | stub | 第 N 次调用返回 false（防死循环），N 默认 1000 可改 |
| `padInitializeAny / padUpdate / padGetButtons*` | stub | 返回模拟键值（默认无按键，可通过 `mock_libnx_set_buttons` 注入） |
| `svcSleepThread` | stub | usleep |
| `socketInitializeDefault / socketExit` | stub | 返回 0 |
| `Result` codes | macros | 都是 0 |

**未覆盖**：GPU/audio/HID 高级特性、文件系统、IPC。这些需要项目级 mock 自行扩展。

## 不要做的事

- 不要尝试 mock 整个 libnx — 太大且失真。只 mock 业务用到的 API。
- 不要在 mock 里塞复杂逻辑 — mock 的作用是"让 host 链接通"，业务逻辑跑自身代码用 ASAN 抓 bug。
- mock 不能替代真模拟器（T3）。GPU/svcBreak 等平台特定行为只有 T3 抓得到。
