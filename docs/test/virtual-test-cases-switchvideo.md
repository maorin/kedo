# 三层虚拟测试 · switchvideo 实战测试方案

> **状态**：草案，等用户评审。  
> **目的**：把 T1/T2/T3 三层在 switchvideo 这个真实 Switch homebrew 项目上跑一遍，回答"虚拟测试到底能不能拦住实战 bug"。  
> **被测对象**：kedo 三层虚拟测试（`tools/build_tool.py` warning surface / `tools/host_test.py` / `tools/emulator_test.py` + `commit_candidate` gate）。  
> **不是**：switchvideo 业务的功能验收（那是另外的事）。

## 0. 项目现状速查

```
/home/maojj/project/switchvideo/
├── CMakeLists.txt     20 行（已 LLM 生成，引用 main.c 而非 main.cpp）
├── Makefile           3968 行（实际 build 走的是 Makefile，CMakeLists 没用）
├── npdm.json          
├── src/
│   ├── main.cpp       359 行（C++ 入口，调 libnx + 4 个 C 模块）
│   ├── input_handler.{c,h}  47 行 + impl，PadState / HidNpadButton 包装
│   ├── nfs_client.{c,h}     64 行 + impl，纯 POSIX 不依赖 libnx ★ T2 样板
│   ├── ui_renderer.{c,h}    89 行 + impl，console + framebuffer 绘制
│   └── video_player.{c,h}   102 行 + impl，FFmpeg / libcurl 封装
├── .kedo/project_profile.json   profile 已生成，type=switch_homebrew
└── build/                       Make 输出 .nro / .elf
```

**注意**：`CMakeLists.txt` 是历史 LLM 生成的，引用的源文件名错（`main.c` 而非 `main.cpp`），但实际 build 走的是 Makefile，不影响。三层测试都用 Makefile/profile.build.command 路径，CMakeLists 暂忽略。

---

## 1. 测试目标

| 层 | 关键问题 |
|---|---|
| **T1** | 把 strict_warnings 打开后，能否在 build 阶段拦住"声明缺失/签名漂移/未初始化"类 bug，且 LLM 能从 surface 的 warning 行里读懂错在哪？ |
| **T2** | host_test + ASAN 能否抓到 null deref / heap overflow / use-after-free？mock_libnx 够不够支撑 switchvideo 子模块（至少 nfs_client）在 host 编译运行？ |
| **T3** | emulator_test + commit_candidate gate 的 5 种状态机分支（disabled / missing-binary 两种 / 模拟 success / 模拟 crash）跑通；Ryubing 真机验证留待 firmware dump 后做。 |

---

## 2. 准备工作

### 2.1 备份现有 profile + 源码

```bash
ssh 192.168.1.8
cd /home/maojj/project/switchvideo
cp .kedo/project_profile.json /tmp/profile.bak.json
git stash --keep-index --include-untracked   # 保护测试中临时引入的 bug code
```

> 每个测试用例跑完都 `git checkout -- src/` 恢复源码，避免"测试 bug"留进真实代码。

### 2.2 建议测试期 daemon 配置

```bash
# 切到 mock LLM,避免每个 case 都烧 LLM token (T1/T2 主要看工具行为, 不需要 LLM 决策)
cat > ~/.kedo/test-config.yaml <<'EOF'
llm_provider: "mock"
reviewer_provider: "none"
EOF

KEDO_HOME=~/.kedo-test KEDO_PORT=8001 \
  kedo-server start /home/maojj/project/switchvideo
# 用 8001 跑测试,8000 留给日常工作
```

测试期通过 `curl -X POST http://127.0.0.1:8001/api/...` 直接打工具端点，不走 LLM。

---

## 3. T1 测试用例（编译期严格化）

### Case T1-A：未声明函数被调 → 严格模式拦下

**动机**：switchvideo 历史 task `b943c9d0` 卡的就是"main.cpp 调了某模块没声明的函数，warning 但 build 过；只在 link 阶段才挂"。T1 应该在 compile 阶段就 fail。

**注入 bug**：在 `src/input_handler.c` 末尾加：

```c
/* 故意调一个未声明的函数 */
void __t1_a_test(void) {
    input_unknown_helper();   /* 未声明 → -Wimplicit-function-declaration */
}
```

**改 profile.strict_warnings**：

```json
"strict_warnings": {
  "enabled": true,
  "cflags": ["-Wall", "-Wextra", "-Wimplicit-function-declaration", "-Werror"],
  "cxxflags": ["-Wall", "-Wextra", "-Werror"],
  "extra_env": {}
}
```

**操作**：

```bash
curl -X POST http://127.0.0.1:8001/api/tools/build \
  -d '{"tool":"build","args":{"project_path":"/home/maojj/project/switchvideo"}}'
```

> 如果还没暴露 tools/build 端点，等价方式：直接起一个 mock task `kedo build`，看 BuildTool 调用返回值。

**预期**：
- ToolResult.success = `False`
- error 字段含 `error: implicit declaration of function 'input_unknown_helper'`（或 `-Werror=implicit-function-declaration` 触发的字样）
- data.warnings 至少 1 条，data.warning_count ≥ 1
- data.strict_env 含 `["CFLAGS", "CXXFLAGS"]`

**通过标准**：error 里出现"implicit declaration"或"-Werror"字样，且 surface 出来的行包含 `input_handler.c:<line>`。

**回收**：`git checkout -- src/input_handler.c`，profile.strict_warnings.enabled 改回 false。

---

### Case T1-B：签名漂移 → header 与 impl 不一致拦下

**动机**：LLM 在 50+ turn 任务里改了 .c 文件签名但忘改 .h，编译期 warning 通过、运行时挂。

**注入 bug**：把 `src/input_handler.c` 的 `input_init` 改为：

```c
void input_init(int dummy_arg) {   /* 原 void → int */
    (void)dummy_arg;
    if (g_initialized) return;
    /* ... */
}
```

`input_handler.h` 不动（仍是 `void input_init(void);`）。

**profile.strict_warnings**：同 T1-A（用 `-Werror`）。

**预期**：
- ToolResult.success = `False`
- error 含 `conflicting types for 'input_init'` 或 `incompatible types`
- 不带 strict 时 (enabled=false) 这个 bug **会通过 build**（仅 warning）→ 用作对照

**通过标准**：strict 开启 fail，关闭 pass。两次对比都跑一遍。

---

### Case T1-C：未初始化变量

**动机**：`-Wuninitialized -Wmaybe-uninitialized` 类。这种 bug 在 ASAN/UBSAN 下也能抓，但 T1 在 build 阶段就该挡。

**注入 bug**：在 `src/video_player.c` 任意函数顶部加：

```c
int x;
if (some_runtime_cond) x = 1;
printf("%d\n", x);   /* maybe-uninitialized */
```

**profile.strict_warnings**：

```json
"cflags": ["-Wall", "-Wextra", "-Wmaybe-uninitialized", "-Werror"]
```

**预期**：error 含 `'x' may be used uninitialized`；surface 出来的 warning 列表里能定位到 `video_player.c:<line>`。

**注意**：`-Wmaybe-uninitialized` 在某些 gcc 版本默认就在 -Wall 里，看实际是否触发。

---

### T1 通过门槛

| 项 | 标准 |
|---|---|
| 必须过 | T1-A、T1-B 两个用例都按预期 fail |
| 加分 | T1-C 触发 maybe-uninitialized |
| surface 检查 | 失败时 ToolResult.error 字段包含 file:line:col 格式行 |
| 兼容回退 | profile.strict_warnings.enabled=false 时 BuildTool 行为不变（已有冒烟，但 switchvideo 下也跑一次确认） |

---

## 4. T2 测试用例（host_test mock + ASAN）

> **样板模块选 `nfs_client.c`**：纯 POSIX 不依赖 libnx，最容易在 host 编通；其它模块（input/ui/video）依赖 libnx，需要 mock_libnx 进一步扩展，留作 follow-up。

### 4.1 准备 mock_dir

```bash
cd /home/maojj/project/switchvideo
mkdir -p tests/host_mock
cd tests/host_mock
# nfs_client.c 是纯 POSIX，直接 symlink 复用
ln -sf ../../src/nfs_client.c .
ln -sf ../../src/nfs_client.h .
```

写一个驱动 `tests/host_mock/host_main.c`：

```c
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "nfs_client.h"

static void test_init_idempotent(void) {
    assert(nfs_client_init() >= 0);
    assert(nfs_client_is_initialized());
    nfs_client_deinit();
}

static void test_list_nonexistent_dir(void) {
    nfs_client_init();
    nfs_entry_t entries[10];
    int n = nfs_client_list_dir("/__definitely_not_exists__", entries, 10);
    /* 期望返回负数或 0；不要 crash */
    printf("list_dir on nonexistent: n=%d\n", n);
    nfs_client_deinit();
}

int main(void) {
    test_init_idempotent();
    test_list_nonexistent_dir();
    printf("host_test: nfs_client basic OK\n");
    return 0;
}
```

### 4.2 改 profile.host_test

```json
"host_test": {
  "enabled": true,
  "mock_dir": "tests/host_mock",
  "build_command": "cc -fsanitize=address,undefined -g -O0 -I. nfs_client.c host_main.c -o host_test 2>&1",
  "run_command": "./host_test",
  "expected_exit_code": 0,
  "timeout_s": 30,
  "auto_run_after_build": true
}
```

---

### Case T2-A：Null 传给 strncpy → ASAN 抓 null deref

**注入 bug**：临时改 `src/nfs_client.c` 的 `nfs_client_list_dir`：

```c
int nfs_client_list_dir(const char *path, nfs_entry_t *entries, int max_entries) {
    /* 故意忽略 entries 是否为 NULL */
    strncpy(entries[0].name, path, 255);   // 当 entries=NULL 时 null deref
    /* ... 原逻辑保留 */
}
```

`host_main.c` 加一行 `nfs_client_list_dir("/", NULL, 10);`。

**预期**：
- host_test 工具返回 success=False
- data.fatal=True，sanitizer_summary 含 `AddressSanitizer: SEGV` 或 `null pointer dereference`
- sanitizer_frames 含 `nfs_client.c:<line>`
- error 字段把第一帧贴出来

**通过标准**：tool 返回 fail + 报告里能看到 `nfs_client.c` 文件名 + 行号。

---

### Case T2-B：Heap buffer overflow

**注入 bug**：在 `nfs_client_get_root` 里：

```c
const char* nfs_client_get_root(void) {
    char *buf = malloc(8);
    strcpy(buf, "/very/long/path/that/overflows");  // overflow
    return buf;
}
```

`host_main.c` 调一次 `nfs_client_get_root()`。

**预期**：ASAN 报 `heap-buffer-overflow`，frame 指到 `nfs_client.c`。

---

### Case T2-C：mock_libnx 编译验证（非 bug 注入）

**目的**：先验证现有 mock 够支撑 input_handler 编过 host，不验证 bug。

**操作**：把 input_handler.c 也加进 host_mock：

```bash
cd /home/maojj/project/switchvideo/tests/host_mock
ln -sf ../../src/input_handler.c .
ln -sf ../../src/input_handler.h .
# 引入 mock_libnx
cp -r /home/maojj/project/kedo/tools/mocks/libnx ./mock_libnx
```

profile.host_test.build_command 改：
```
cc -fsanitize=address,undefined -g -O0 -I. -Imock_libnx/include \
   nfs_client.c input_handler.c mock_libnx/src/mock_libnx.c host_main.c \
   -o host_test 2>&1
```

`host_main.c` 加 `input_init(); input_poll(); input_deinit();`。

**预期**：
- 能编过 → mock_libnx 至少覆盖 padConfigureInput / padInitializeDefault / padUpdate / padGetButtons*
- 编不过 → 列出缺失的符号，作为 follow-up 扩 mock 的 todo

**通过标准**：要么编过，要么明确列出缺哪些 API（不算 fail，是设计输出）。

---

### T2 通过门槛

| 项 | 标准 |
|---|---|
| 必须过 | T2-A、T2-B 都被 ASAN 抓 + 报告里能定位 file:line |
| 必须过 | host_test 工具的"未启用"路径在 enabled=false 时秒返回 success+skipped |
| 加分 | T2-C 列出 mock 覆盖率 / 缺失 API 清单 |
| 自动调 | 在 ReactAgent 里跑一个会过 build 的 task，看 build 通过后 host_test 是否被自动调 + 结果是否回灌进 messages（看 dashboard 事件流）|

---

## 5. T3 测试用例（emulator_test + commit_candidate gate）

> **Ryubing 真机验证不在本轮**（需要 firmware dump，已记入 `docs/virtual-test-strategy.md` 的"Ryubing 安装步骤"）。本轮验证工具状态机 + commit_candidate gate 接线。

### Case T3-A：disabled → skip

profile.emulator：
```json
"emulator": { "enabled": false }
```

跑 commit_candidate（mock 一个候选），预期：
- emulator_meta.emulator_skipped = true
- commit_candidate 正常创建候选

---

### Case T3-B：enabled + binary 缺失 + required=false → warn 不阻塞

profile.emulator：
```json
{
  "enabled": true,
  "command_template": "no-such-emu --headless {artifact}",
  "required": false
}
```

跑 commit_candidate，预期：
- emulator_meta.emulator_skipped = true  (reason="binary_missing")
- commit_candidate 仍然创建候选成功
- candidate.data 含 emulator_command 字段（用于审计）

---

### Case T3-C：enabled + binary 缺失 + required=true → 拒

同 T3-B 但 `required: true`。

预期：
- commit_candidate 返回 ToolResult(success=False)
- error 含 "emulator gate REJECTED" + "binary not found"
- candidate **没有**被创建（VersionManager.create_candidate 没被调）

---

### Case T3-D：echo 模拟成功

```json
{
  "enabled": true,
  "command_template": "echo 'main loop entered for {artifact}'",
  "success_patterns": ["main loop entered"],
  "crash_patterns": ["svcBreak"],
  "timeout_s": 5,
  "required": false
}
```

预期：emulator_test pass，commit_candidate 正常创建，candidate.data.emulator_returncode=0。

---

### Case T3-E：echo 模拟崩溃 + required=true → 拒

```json
{
  "command_template": "echo 'svcBreak Result code 0x2168'",
  "crash_patterns": ["svcBreak", "Result code 0x[0-9a-f]+"],
  "required": true
}
```

预期：commit_candidate 拒，data.emulator_crashes 至少 1 条匹配 svcBreak 的行。

---

### T3 通过门槛

| 项 | 标准 |
|---|---|
| 必须过 | T3-A 到 T3-E 全部按预期路径走（5 个状态机分支） |
| 检查 | candidate 创建后 dashboard panel 显示 emulator 信息（emulator_command + emulator_returncode）|
| 加分 | Ryubing 装好后做 T3-F：真跑 build/NfsVideoPlayer.nro，看 success_patterns / crash_patterns 在真模拟器输出里是否准确 |

---

## 6. 综合验证：build → host_test → emulator_test 一条龙

把 profile 的 T1 + T2 + T3 三层全开：

```json
{
  "strict_warnings": { "enabled": true, "cflags": ["-Wall", "-Wextra"] },
  "host_test":      { "enabled": true, "auto_run_after_build": true, ... },
  "emulator":       { "enabled": true, "required": false, "command_template": "echo ok" }
}
```

跑一个 mock task 让 ReactAgent 走 build → 自动 host_test → commit_candidate → 自动 emulator gate。

**通过标准**：dashboard 事件流里能看到完整链路：

```
STEP_STARTED build
STEP_COMPLETED build (warning_count=N)
STEP_STARTED host_test_auto
STEP_COMPLETED host_test_auto
... (LLM 决定 commit)
TOOL_CALL commit_candidate
... emulator gate evaluated
CANDIDATE_CREATED v1 (with emulator_meta)
```

---

## 7. 评分卡

| 层 | 必须过 | 加分项 | 我的评估 |
|---|---|---|---|
| T1 | T1-A, T1-B | T1-C | ☐ |
| T2 | T2-A, T2-B, host_test 自动调 | T2-C mock 覆盖率盘点 | ☐ |
| T3 | T3-A 到 T3-E (5 状态机分支) | T3-F Ryubing 真机 | ☐ |
| 综合 | 一条龙跑通 + dashboard 能可视化 | — | ☐ |

**判定**：
- **绿灯**：必须过的 case 全过 → 三层 kedo-侧实现可信，可以正式启用
- **黄灯**：T1/T2 必须过 + T3 状态机分支至少 4 个过 → 启用 T1+T2，T3 等真模拟器装好再开
- **红灯**：T1 或 T2 任何一个必须过 case 失败 → 暂不启用，先修

---

## 8. 时间预算

| 阶段 | 预估 |
|---|---|
| 准备（备份 profile / 起 test daemon） | 10 min |
| T1 三个 case（含 git checkout 回收） | 30 min |
| T2 准备 mock_dir + 三个 case | 60 min |
| T3 五个状态机 case | 30 min |
| 综合一条龙 | 20 min |
| 总计 | **~2.5 小时** |

---

## 9. 已知风险

1. **switchvideo 当前 build 用 Makefile 不是 CMakeLists**：T1 的 `-Werror` 注入靠 CFLAGS env 追加，需要 Makefile 实际尊重 CFLAGS（多数 Makefile 都尊重，但需验证）。如果 Makefile 硬编码 CFLAGS 不合并环境变量，T1 测试会"看似启用但没生效"。**建议**：T1-A 跑前先 `make -p | grep CFLAGS` 看 Makefile 是否 append 了 `$(CFLAGS)`。
2. **mock_libnx 当前覆盖范围窄**：`padInitializeDefault` 不在 mock 里（mock 里只有 `padInitializeAny`）。T2-C 大概率会暴露至少 1-2 个缺失符号，需要在测试期间扩 mock 或 rename 别名。
3. **mock LLM 可能过简单**：T2/T3 的"自动调" / "auto_run_after_build" 路径需要 ReactAgent 真跑一轮，mock LLM 可能不会调 build → 没机会触发 auto host_test。这种时候改用 curl 直接打 `/api/tools/...` 端点替代。
4. **`/api/tools/...` 是否已暴露**：方案里假设 kedo 有这种端点；如果没有，得通过创建 task + 走 LLM 路径来触发工具。需要确认。

---

## 10. 评审请求

- [ ] 测试目标分层（T1/T2/T3 各管什么 bug 类）合理吗？
- [ ] 注入的 bug code 是否可以代表 switchvideo 实战会遇到的真 bug？
- [ ] 通过门槛是否合理（必须过 vs 加分）？
- [ ] 是否要补 case：例如 T2 在 video_player.c 上做 use-after-free 测试（FFmpeg context 释放后访问的真实场景）？
- [ ] 时间预算可接受吗？要不要砍掉某些 case 先做 MVP 验证？
- [ ] 第 9 节里风险 1（Makefile + CFLAGS）是不是 blocker？要不要先在 switchvideo 的 Makefile 里确认 CFLAGS 入口可用？

---

## 11. 实战里程碑：switchvideo 基本播放功能跑通（2026-05-07）

> **状态**：✅ 完成 — kedo 端到端跑出，Switch 真机能播放 192.168.1.8 NFS 共享存储里的 .mp4。
> **价值**：第一个非 hello-world 级别的端到端实战收益，T3 模拟器搁置后的"真机 coredump 抓取"路径首次实战验证。

### 11.1 任务时间线（5/4 → 5/7，12 个 task）

从原始需求到稳定播放，全部 task 用 kedo 跑：

| 时间 | task_id | 描述（节选） | 状态 |
|---|---|---|---|
| 5/4 | `6ad7d5f6` | 原始需求："hello world → 连 NFS 192.168.1.8:/NFS 播视频" | ✅ |
| 5/6 | `35f711ab` | "启动错误，switch 上开了 ftp 192.168.1.145:5000，看一下 log" | ✅ |
| 5/6 | `a0d7ec26` | "帮我编译成 nro" | ✅ |
| 5/7 | `ea8fa95f` | "还是启动错误，switch 上开了 ftpd，直接下载 log" | ✅ |
| 5/7 | `de1083dd` | 用户给本地路径 `01733317250_*.log`，告知"这个文件不对" | ✅ |
| 5/7 | `b7957f7a` | "你是不是应拿 `01733324689_*.log`"（用户指认正确日志） | ❌ Tool: fetch_crash_report 卡 2% |
| 5/7 | `93378547` | "写 scripts/video_server.py（charter.external_services 声明了但没实现）：HTTP server 8080 端口" | ✅ |
| 5/7 | `83ae92dd` | 真视频测试："播放时 GET /...REBD-1025.mp4 HTTP/1.1 206" | ✅ |
| 5/7 | `4a891b6b` | "NFS 播放器 UI 优化，字都是反的" | ✅ |
| 5/7 | `79ec885b` | "switch 端播放没反应，python 这块是 206 回的" | ✅ |

最终结果：Switch 真机从 192.168.1.8 上的 HTTP 桥接 server (`scripts/video_server.py`) 拉 `.mp4` 流式播放，UI 正常。

### 11.2 fetch_crash_report 路径首次实战（5/6 → 5/7）

`35f711ab` / `ea8fa95f` 这两次"启动错误，开了 ftpd"是 dashboard 真机 coredump widget 的实战首秀：

**走通的部分**：
- LLM 看到"启动错误 + Switch 端 ftpd 已开"就主动调 `fetch_crash_report`
- Dashboard 弹橙色 widget，用户填 IP/端口（实战是 192.168.1.145:5000）+ 勾选记住
- `/atmosphere/crash_reports/*.log` 拉到本地（文件名形如 `01733317250_010000000000100d.log`）
- addr2line 解析 → LLM 拿到 `function @ file:line` 直接定位

**实战暴露的坑（task `b7957f7a` 失败）**：
- 用户主动追问"你是不是应拿 `01733324689_*.log`"。说明 **LLM 选最新 log 的策略错了**：字典序最大不一定是"用户当前关心的那次崩溃"。Switch 多次 crash 留多个 log 时，LLM 可能挑了过期的。
- 改进方向：(a) 工具不要 silent 选最新——改成列出候选 + 读各 log 头部时间戳/result_code，让 LLM/用户主动挑；(b) 工具默认只下载，让 LLM 下一步显式调 `read_file` 选。
- 实战里 Switch IP 是 `192.168.1.145`（不是 mock 用的 `192.168.1.100`），路径默认 `/atmosphere/crash_reports/` 和 ftpd-pro 默认导出一致，没遇到连接坑。

### 11.3 关键决策点

1. **HTTP 桥接替 NFS（沿用 4/12 决定）**：libnfs 在 devkitPro Switch portlibs 不可用 → 走 HTTP。这次 server 端 (`scripts/video_server.py`) 是 kedo 自己写的（task `93378547`），含 Range 206 partial content 支持。
2. **Charter.external_services 提前声明**：charter 里早就声明了 "external_services: scripts/video_server.py"，所以 LLM 在播放报错时知道服务端缺什么，主动写 server 而不是去改 .nro 端。这印证 charter forbidden_patterns + external_services 双向约束 (commit `ed96fe8` / `07e4a13`) 在实战里的价值。
3. **UI 镜像翻字 (`4a891b6b`)**：framebuffer 直绘方向位错了，kedo 一次改对。说明纯渲染逻辑 LLM 修起来比运行时 bug 容易得多。

### 11.4 对 T1/T2/T3 假设的实战验证

| 假设 | 实战验证 |
|---|---|
| **T1 编译期 -Werror 拦低级 bug** | ⚠ 未独立验证 — 实战里 build 失败多是"包/库不存在"或"profile 命令路径错"，不是编译警告级。strict_warnings 仍待专门跑 case T1-A 验证 |
| **T2 host_test + ASAN 抓内存 bug** | ❌ 未跑 — 实战崩点都需要真机才能复现 |
| **T3 模拟器路径替代（fetch_crash_report）** | ✅ 落地并实战 — 多次 task 走通 widget→ack→FTP→addr2line 全链路。**坑**：log 选哪份的策略需改进（见 11.2） |
| **Charter forbidden_patterns + external_services** | ✅ 实战收益明显 — charter 引导 LLM 在播放故障时去写 server 而非改 .nro |

### 11.5 下一轮要补的 case

- [ ] `fetch_crash_report` 多 log 选择策略：列出+读 metadata 后挑，不再 silent 选最新（task `b7957f7a` 暴露）
- [ ] 跑一次 case T1-A（未声明函数）确认 -Werror 在 switchvideo Makefile 路径下能生效
- [ ] 真视频播放的"卡顿/206 半截"类 bug 能否用 host_test 模拟 HTTP server 复现 — T2 在 switchvideo 上的潜在落地点

