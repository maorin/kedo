# 长期改进方案 (post-6ad7d5f6)

> **状态**：草案，等评审。  
> **背景**：switchvideo task `6ad7d5f6` 撞上 `audrenInitialize(NULL)` 触发 `2168-0002 / x4a8` svcBreak；同一 task 还把 server 端责任甩给用户 (`python3 -m http.server 8080`)。  
> **本文档目的**：把 P0/P1/P2 三个长期改进方案细化成可评估、可分配工作量的切片。  
> **关联文档**：
> - [virtual-test-strategy.md](../virtual-test-strategy.md) — T1/T2/T3 三层设计
> - [virtual-test-cases-switchvideo.md](virtual-test-cases-switchvideo.md) — 三层测试用例

---

## 0. 当前状态盘点（写在前面）

| 改进 | 状态 | 落地位置 |
|---|---|---|
| **P0** charter forbidden_patterns 工具层硬拦截 | ✅ **已上线 (commit `ed96fe8`)** | `core/project_charter.py` + `tools/profile_guard.py` |
| **P0** reviewer deliverable_completeness 维度 | ✅ **已上线** | `core/reviewer.py` |
| **P0+** switchvideo charter 6 条实战 patterns | ✅ **已部署到 192.168.1.8** | `.kedo/project_charter.md` |
| **P1** Ryubing 装机 + T3 真模拟器启用 | ⏸ **未做** | 本文档第 2 节 |
| **P2** mock_libnx 扩展 audren / 旧 hid API stub | ⏸ **未做** | 本文档第 3 节 |
| **P0 残留打磨** | ⏸ **未做** | 本文档第 1 节 |

P0 不再是"全新方案"，是"已上线 + 三处可补的打磨"。P1/P2 是真正未落地的长期改进。

---

## 1. P0+ 残留打磨（已上线之上的小修）

### 1.1 charter forbidden_patterns 命中后的 LLM 体验

当前命中后 ToolResult.error 给的是规则文本：

```
charter:forbidden_patterns — refusing to write video_player.c:
matched forbidden pattern `audrenInitialize\s*\(\s*NULL\s*\)`
Reason: audrenInitialize 要求完整 AudioRendererConfig, 传 NULL 必触发 svcBreak 2168-0002
Either remove this pattern from your generated code, or call `propose_charter_change` ...
```

LLM 看到后的可能反应：
1. ✅ 改方案（不调 audren）
2. ⚠️ 调 `propose_charter_change` 想绕过（这才是 charter frozen 的本意 — 让用户决策）
3. ❌ 反复重写代码同样调 audren（如果这条规则 LLM 不"领会"原因）

**第三种情况怎么治？** 实测才知道。先观察一两次 task，如果 LLM 反复撞同一条 pattern 才考虑加 reject_tracker。**先不动。**

### 1.2 charter forbidden_patterns 的批量编辑工具

当前要加新的 forbidden_patterns 必须人工 ssh 上去改 `.kedo/project_charter.md`。规模化场景下不友好。

可加一条 CLI：

```bash
kedo charter add-pattern \
  --pattern '\bunsafe_call\b' \
  --reason "..." \
  --applies-to "*.c,*.cpp"
```

**ROI 评估**：当前 charter 是 freeze 状态，改动靠人审；批量编辑 CLI 不解决"领悟新 bug 模式"这个真问题。**先不做。**

### 1.3 forbidden_patterns 命中事件可观测性

当前命中只记 `logger.warning` + 返回给 LLM，**dashboard 看不到**。建议：

- 在 `state_manager.event_bus` 发一个新事件 `CHARTER_VIOLATION`，data 含 `{file_path, pattern, reason}`
- Dashboard 顶栏加一个红色徽章：`⚠ Charter violations this task: 3`

**工作量**：~30 min 改 `tools/profile_guard.py` + `dashboard/index.html`。  
**价值**：可视化 LLM 在哪些规则上反复撞，指导 charter 演进。  
**建议**：可做但不紧迫，留给下次有空时打磨。

---

## 2. P1 — Ryubing 装机 + T3 真模拟器启用 ★ 重点

> **核心论点**：T1/T2 设计上**完全救不了** `audrenInitialize(NULL)` 这一类（编译期合法 + libnx-only API + host 不可重现），只有 T3 真模拟器能把"build 通过 + reviewer 也过 + 装到 Switch 才崩"的 bug 在 commit_candidate 关卡前拦下。switchvideo `6ad7d5f6` 就是 T3 ROI 的最直接证明。

### 2.1 装机环境与依赖

**目标主机**：`192.168.1.8`（已是 kedo daemon 宿主）  
**操作系统**：Ubuntu 24.04（已知）  
**预估安装时间**：3-4 小时（含 firmware dump 准备）  
**预估单次 emulator run**：40-90 秒  
**重启 daemon 必要性**：装完不需要重启 daemon，profile.emulator 启用即可

#### 2.1.1 系统依赖

```bash
# Microsoft 官方 .NET 8 仓库
wget https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb
sudo dpkg -i packages-microsoft-prod.deb
sudo apt update

# 装 .NET 8 SDK + xvfb (headless display) + 编译用 git/build-essential
sudo apt install -y dotnet-sdk-8.0 xvfb git build-essential

# 验证
dotnet --version    # 期望 8.x
which xvfb-run      # 期望 /usr/bin/xvfb-run
```

#### 2.1.2 Ryubing 源码 build

```bash
git clone https://github.com/Ryubing/Ryujinx ~/Ryubing
cd ~/Ryubing
# DISABLE_UPDATER 关掉自动更新检查 (服务端 daemon 模式不需要)
dotnet build -c Release -p:ExtraDefineConstants=DISABLE_UPDATER

# 验证 headless 入口编译产物
ls ~/Ryubing/src/Ryujinx.Headless.SDL2/bin/Release/net8.0/Ryujinx.Headless.SDL2
# 期望: 此可执行文件存在
```

**预估**：dotnet build 约 8-15 分钟首次编译。

### 2.2 Firmware 准备（用户侧 — kedo 不能也不该自动做）

> ⚠️ **法律 + 安全**：必须从你**自己拥有的**任天堂 Switch 主机 dump，不能从网上下载。下载到的 firmware 几乎都带病毒或盗版。Ryubing/Ryujinx 项目本身不分发 firmware，这是合法边界。

#### 2.2.1 Switch 端准备

1. 你自己的 Switch 进入 RCM 模式
2. 用 [Lockpick_RCM](https://github.com/shchmue/Lockpick_RCM) payload 启动
3. dump `prod.keys` + `title.keys` 到 SD 卡
4. 用 [nxdumptool](https://github.com/DarkMatterCore/nxdumptool) dump 当前系统的 firmware（系统设置 → 主机信息可看版本号，记下来）
5. 把这些文件 SCP 到 `192.168.1.8`

#### 2.2.2 Ryubing 端目录布置

```bash
# Ryubing 默认配置路径
mkdir -p ~/.config/Ryujinx/system
mkdir -p ~/.config/Ryujinx/bis/system/Contents/registered

# 拷贝密钥
cp prod.keys title.keys ~/.config/Ryujinx/system/

# 拷贝 firmware (一堆 .nca 文件)
cp /path/to/dumped/firmware/*.nca ~/.config/Ryujinx/bis/system/Contents/registered/
```

#### 2.2.3 装机自检：跑一个已知能工作的 .nro

```bash
# 用 hello-world.nro 之类的简单 homebrew (任意 .nro 都行) 验证启动链路
xvfb-run -a -s "-screen 0 1280x720x24" \
  ~/Ryubing/src/Ryujinx.Headless.SDL2/bin/Release/net8.0/Ryujinx.Headless.SDL2 \
  --memory-manager-mode HostMappedUnsafe \
  --graphics-backend OpenGl \
  /path/to/hello-world.nro 2>&1 | tee /tmp/ryubing_test.log &
EMULATOR_PID=$!
sleep 30
kill $EMULATOR_PID

# 检查 log 没有 fatal 错
grep -E "panic|svcBreak|Fatal exception|Result code 0x[0-9a-f]+" /tmp/ryubing_test.log
# 期望: 无输出 (没有 panic)
```

**关键参数说明**：

| flag | 用途 | 备注 |
|---|---|---|
| `xvfb-run -a -s "-screen 0 1280x720x24"` | headless 虚拟显示 | `-a` 自动选 display；尺寸够 Switch 分辨率 |
| `--memory-manager-mode HostMappedUnsafe` | 性能模式 | 服务端跑可接受 unsafe |
| `--graphics-backend OpenGl` | 用 OpenGL 不用 Vulkan | **xvfb + Vulkan 在 Linux 经常黑屏**（已知坑） |
| `--no-input` | 禁掉输入设备 | 服务端没控制器 |

### 2.3 kedo 侧集成（已基本就绪）

**关键事实**：T3 工具在 Phase C 已经全部落地（commit `bf4539a`），只需要给 switchvideo 的 profile.emulator 配上正确参数。当前 switchvideo profile.emulator **未启用**。

#### 2.3.1 switchvideo 的 profile.emulator 推荐配置

把以下 JSON 段并入 `/home/maojj/project/switchvideo/.kedo/project_profile.json`：

```json
"emulator": {
  "enabled": true,
  "command_template": "xvfb-run -a -s '-screen 0 1280x720x24' /home/maojj/Ryubing/src/Ryujinx.Headless.SDL2/bin/Release/net8.0/Ryujinx.Headless.SDL2 --memory-manager-mode HostMappedUnsafe --graphics-backend OpenGl --no-input {artifact}",
  "timeout_s": 60,
  "success_patterns": [
    "consoleInit",
    "Application loaded",
    "PROGRAM CODE START"
  ],
  "crash_patterns": [
    "panic",
    "svcBreak",
    "Result code 0x[0-9a-f]+",
    "Fatal exception",
    "ABI breakpoint"
  ],
  "required": false
}
```

**`required: false` 的含义**（重要）：

- 开发阶段推荐 false：emulator 失败只 warning，不阻塞 commit_candidate（容忍 false positive、加速迭代）
- 接近发布时切到 true：emulator 失败必拒，强保证装到真机不崩
- false 时 emulator 仍跑，结果会进 candidate 元信息 + dashboard，给人审看

#### 2.3.2 commit_candidate 阶段的工作流

T3 已在 commit_candidate 工具内集成（Phase C 时落地）：

```
Producer 调 commit_candidate
  ↓
Reviewer 评审（5 维度）
  ↓ approve
Emulator gate (T3) 跑一次
  ├─ profile.emulator.enabled=false → skip
  ├─ binary 缺失 + required=false → skip + warn
  ├─ 命中 success_patterns → pass
  └─ 命中 crash_patterns → fail
  ↓ pass (或 required=false 时容忍)
VersionManager.create_candidate
  ↓
candidate 元信息含 {emulator_command, emulator_returncode, emulator_crashes}
```

### 2.4 验证测试（装完后跑）

#### 验证 #1：当前 working tree（带着 audren bug）应该被 emulator 拦下

```bash
# 用现有 build/switchvideo.nro (含 audrenInitialize NULL bug)
curl -X POST http://127.0.0.1:8000/api/tools/emulator_test \
  -H "Content-Type: application/json" \
  -d '{"project_path": "/home/maojj/project/switchvideo"}'
```

期望：返回 `success: false`, error 含 `crash detected: svcBreak` 或 `Result code 0x...`，data.crash_hits 至少 1 条。

#### 验证 #2：手动修复 audren bug 后 emulator 应该过

```bash
# 编辑 src/video_player.c, 把 audrenInitialize(NULL) 注释掉
# rebuild
# 重跑 emulator_test
```

期望：`success: true`, success_hits 命中至少 1 条 (e.g. "Application loaded")。

#### 验证 #3：commit_candidate 链路集成

新建一个简单 task 让 LLM 跑通到 commit_candidate，dashboard candidate 卡片应该显示 emulator 元信息。

### 2.5 性能预算

每次 emulator run 大概：

| 阶段 | 耗时 |
|---|---|
| Ryubing 启动（含 firmware load） | 5-15 s |
| .nro 加载 | 2-5 s |
| 跑测试逻辑（appletMainLoop 几十次） | 20-40 s |
| 退出 + xvfb 清理 | 2-5 s |
| **总计** | **~30-65 s** |

ReactAgent 主循环**绝对不应该**每 turn 跑 emulator。当前设计：只在 `commit_candidate` 关卡跑一次。这一次 60s 是可接受的（commit_candidate 本来就是"task 末端"动作）。

### 2.6 已知坑

| 坑 | 缓解 |
|---|---|
| Vulkan + xvfb 黑屏 | 用 `--graphics-backend OpenGl` |
| audio 在 xvfb 下无声 | 不影响 .nro 跑得起来；audio service 仍 init |
| libcurl/网络在 Ryubing 行为不一致 | switchvideo HTTP 测试可能误报，先放 `required: false` |
| 启动时间不稳定（5-30s 浮动） | timeout 给到 60s+ 不要给 30s |
| firmware 升级时所有缓存失效 | dump firmware 后版本固定，长期维护负担小 |
| Ryubing 主分叉变更 | 锁 `git checkout <commit>` 不跟随 master |

### 2.7 风险 + Rollback

**风险评估**：

- **法律风险**：firmware 必须用户自己 dump（已强调）
- **维护风险**：Ryubing 是社区分叉，可能不稳定。**对策**：装的时候记下 commit hash，出问题就 git reset 到稳定版本
- **磁盘占用**：firmware ~1.5 GB + Ryubing build artifacts ~500 MB ≈ 2 GB。192.168.1.8 应该宽裕

**Rollback 路径**：

- profile.emulator.enabled = false → T3 完全 short-circuit，回到 Phase C 之前行为
- 不需要回滚代码，只动 profile

### 2.8 P1 工作量分解

| 步骤 | 时间 |
|---|---|
| Ubuntu 装 .NET 8 + xvfb | 15 min |
| clone + build Ryubing | 20 min |
| 用户自己 Switch dump firmware（**用户侧**） | 1-2 hr |
| 拷贝 firmware/keys 到 192.168.1.8 + 装机自检 | 30 min |
| 给 switchvideo profile.emulator 填 config | 5 min |
| 验证 #1 / #2 / #3 | 30 min |
| 实战观察 1-2 个 task 的 emulator 行为，调 timeout / patterns | 1 hr |
| **总计（kedo 侧）** | **~3 hr** |
| **+ 用户 firmware dump** | **~1-2 hr** |

### 2.9 ROI

**直接收益**：
- ✅ `audrenInitialize(NULL)` 类 svcBreak 100% 拦下
- ✅ `console/framebuffer 资源冲突`类（switchvideo 4 连漂移真因）100% 拦下
- ✅ 各类 platform-specific 运行时 bug（GPU shader / audio service init / sm 服务权限）100% 拦下
- ✅ commit_candidate 关卡 deliverable 真实"装机能跑"，不是"build 过就 commit"

**机会成本**：
- 60 秒/次 emulator 加在 commit_candidate 阶段——可接受
- 一次性 ~3 小时 setup（kedo 侧）+ 用户 1-2 小时 firmware dump

**何时该装**：
- 马上 — 因为 switchvideo 还在试错阶段，每个 commit 都怕崩
- 或者等下一次又触发 svcBreak 类 bug 时再装（被动）

**建议**：**马上装**。每次又踩一个 svcBreak 都浪费一次实战观察机会。

---

## 3. P2 — mock_libnx 扩展 ★ 中优先级

> **核心论点**：T2 host_test + ASAN 不能直接救 `audrenInitialize(NULL)`（libnx-only API），但**可以通过扩展 mock 让 host_test 复现这个错误模式**。让 host 测试期就能发现"传 NULL 给 audren"，比真机迭代快 10x。

### 3.1 当前 mock 覆盖盘点

```
tools/mocks/libnx/include/switch.h     ~80 line  public header (stub-friendly subset)
tools/mocks/libnx/src/mock_libnx.c     ~85 line  console / appletMainLoop / pad / svc / socket / romfs
```

**已覆盖**（够 hello-world Switch homebrew 在 host 编译运行）：
- `consoleInit / consoleClear / consoleUpdate / consoleExit`
- `appletMainLoop` （带 iteration cap 防死循环）
- `padConfigureInput / padInitializeAny / padUpdate / padGetButtons*`
- `svcSleepThread`
- `socketInitializeDefault / socketExit`
- `romfsInit / romfsExit`

**未覆盖**（switchvideo 用到的但 mock 缺失）：
- `audrenInitialize / audrenExit / ...` （audio renderer 全套）
- `audoutInitialize / audoutExit / ...` （audio out 旧 API）
- `padInitializeDefault` （charter 推荐的，**与 padInitializeAny 是不同函数**）
- `consoleDebugInit` （switchvideo `6ad7d5f6` 用到）
- `framebufferCreate / framebufferMakeLinear / framebufferBegin / framebufferEnd`
- `nwindowGetDefault`
- `hidScanInput / hidKeysDown`（**deprecated, 应该 abort 而不是 stub**）
- `hosversionGet`（已有但路径不对）

### 3.2 扩展策略：三类 stub

#### 类型 A — 立即 abort 的 deprecated/broken 模式

让"已知 bug 模式"在 host 上立刻挂，给 LLM 强反馈。这是 P2 的**核心价值**。

```c
/* mock_libnx_strict.c — 已知错误模式立即 abort */

Result audrenInitialize(const AudioRendererConfig *config) {
    if (!config) {
        fprintf(stderr,
            "[mock_libnx] FATAL: audrenInitialize(NULL) — config required.\n"
            "On real Switch this triggers svcBreak 2168-0002 (x4a8).\n"
            "Use a populated AudioRendererConfig, or per charter use SDL2 audio.\n");
        abort();   /* ASAN/host 立即挂, file:line 给 LLM */
    }
    /* config 非 NULL 时正常 stub: 检查关键字段 */
    if (config->output_rate == 0) {
        fprintf(stderr, "[mock_libnx] FATAL: AudioRendererConfig.output_rate=0\n");
        abort();
    }
    return 0;
}

void hidScanInput(void) {
    fprintf(stderr,
        "[mock_libnx] FATAL: hidScanInput is deprecated.\n"
        "Charter 要求 PadState API: padInitializeDefault / padUpdate / padGetButtonsDown.\n");
    abort();
}

uint64_t hidKeysDown(uint32_t controller) {
    (void)controller;
    fprintf(stderr,
        "[mock_libnx] FATAL: hidKeysDown is deprecated.\n"
        "Charter 要求 padGetButtonsDown(PadState*).\n");
    abort();
}
```

**预期效果**：LLM 在 task 期间 host_test 就 abort，看到清晰的 stderr 反馈（含 file:line via ASAN frames），不用等装到 Switch 才暴露。

#### 类型 B — 正常 stub（让流程跑通）

补齐 switchvideo 用到的非错误 API：

```c
/* mock_libnx_audio.c */
Result audoutInitialize(void) { return 0; }
void   audoutExit(void) {}
Result audoutStartAudioOut(void) { return 0; }
void   audoutStopAudioOut(void) {}

/* mock_libnx_video.c */
typedef struct { int placeholder; } NWindow;
typedef struct { int placeholder; } Framebuffer;
NWindow *nwindowGetDefault(void) { static NWindow w = {0}; return &w; }
Result framebufferCreate(Framebuffer *fb, NWindow *win, uint32_t w, uint32_t h, uint32_t fmt, uint32_t buffers) {
    (void)fb; (void)win; (void)w; (void)h; (void)fmt; (void)buffers;
    return 0;
}
Result framebufferMakeLinear(Framebuffer *fb) { (void)fb; return 0; }
uint8_t *framebufferBegin(Framebuffer *fb, uint32_t *out_stride) {
    (void)fb;
    static uint8_t fake[1280*720*4];   /* 假帧缓冲 */
    if (out_stride) *out_stride = 1280*4;
    return fake;
}
void framebufferEnd(Framebuffer *fb) { (void)fb; }

/* mock_libnx_input.c — padInitializeDefault: 与 padInitializeAny 行为一样 */
void padInitializeDefault(PadState *pad) { padInitializeAny(pad); }

/* consoleDebugInit: 静默 stub */
typedef enum { debugDevice_SVC = 1 } DebugDevice;
void consoleDebugInit(DebugDevice dev) { (void)dev; }
```

#### 类型 C — 启动顺序检查

让 mock 知道哪个 API 必须在哪个之前调，违反就 abort。这能拦住 charter 里"网络初始化前必须 socketInitializeDefault" 类的顺序错误。

```c
/* mock_libnx_strict.c (续) — 全局状态追踪 init 顺序 */

static bool g_socket_initialized = false;
static bool g_warned_avformat_before_socket = false;

Result socketInitializeDefault(void) {
    g_socket_initialized = true;
    return 0;
}

/* 注: avformat_network_init 是 FFmpeg 的, 不是 libnx 的, 没法直接拦.
   但可以提供一个包装宏让 host_test 编译期替换:
*/
#define avformat_network_init() do { \
    if (!g_socket_initialized) { \
        fprintf(stderr, "[mock_libnx] WARN: avformat_network_init called before " \
                "socketInitializeDefault — charter requires socket init first.\n"); \
    } \
    avformat_network_init_orig(); \
} while (0)
```

这条比较 hack，是否做要看实战。**先不做。**

### 3.3 文件组织

```
tools/mocks/libnx/
├── README.md                       (已有)
├── include/
│   └── switch.h                    (扩展: 加新 API 声明)
└── src/
    ├── mock_libnx.c                (已有: console/applet/pad/svc/socket/romfs)
    ├── mock_libnx_audio.c          (新: audoutInitialize 等正常 stub)
    ├── mock_libnx_video.c          (新: nwindow/framebuffer 正常 stub)
    └── mock_libnx_strict.c         (新: audrenInitialize NULL / hidScanInput / hidKeysDown abort)
```

### 3.4 host_test 集成

profile.host_test.build_command 引用全部 mock 源文件：

```json
"host_test": {
  "enabled": true,
  "mock_dir": "tests/host_mock",
  "build_command": "cc -fsanitize=address,undefined -g -O0 -I. -I../../tools/mocks/libnx/include nfs_client.c host_main.c ../../tools/mocks/libnx/src/mock_libnx.c ../../tools/mocks/libnx/src/mock_libnx_audio.c ../../tools/mocks/libnx/src/mock_libnx_video.c ../../tools/mocks/libnx/src/mock_libnx_strict.c -o host_test 2>&1",
  "run_command": "./host_test",
  "expected_exit_code": 0,
  "timeout_s": 30,
  "auto_run_after_build": true
}
```

### 3.5 验证测试

#### 验证 #A1：audrenInitialize(NULL) 在 host_test 上 abort

```c
/* tests/host_mock/host_main.c */
#include <switch.h>
int main(void) {
    audrenInitialize(NULL);   /* 期望: mock abort + 输出 FATAL */
    return 0;
}
```

期望：
- host_test 工具返回 success=False
- output 含 `[mock_libnx] FATAL: audrenInitialize(NULL)`
- ASAN frames 含 `host_main.c:N` (具体行号)

#### 验证 #A2：hidScanInput abort

类似 #A1，调 `hidScanInput()` → abort。

#### 验证 #A3：正常路径不 abort

```c
int main(void) {
    AudioRendererConfig cfg = { .output_rate = 48000 };
    audrenInitialize(&cfg);   /* 期望: 通过 */
    audrenExit();
    return 0;
}
```

期望：host_test success，无 abort。

### 3.6 P2 工作量分解

| 步骤 | 时间 |
|---|---|
| 扩 `include/switch.h` 加新声明（audren/audout/nwindow/framebuffer/hidKeys等） | 30 min |
| 写 `src/mock_libnx_audio.c`（类型 B 正常 stub） | 30 min |
| 写 `src/mock_libnx_video.c`（类型 B 正常 stub） | 30 min |
| 写 `src/mock_libnx_strict.c`（类型 A abort 模式） | 45 min |
| 更新 `tools/mocks/libnx/README.md` 覆盖矩阵 + 用法 | 15 min |
| 写 host_main 验证 case A1/A2/A3 | 30 min |
| 在本机跑通 + 部署到 192.168.1.8 | 15 min |
| **总计** | **~3 小时** |

### 3.7 ROI

**直接收益**：
- ✅ `audrenInitialize(NULL)` 类在 host_test 阶段就 abort，无需等 emulator/真机
- ✅ `hidScanInput / hidKeysDown` deprecated API 在 host 上 abort
- ✅ host_test build 命令兼容更多 switchvideo 子模块（不只 nfs_client，还能加 video_player 部分）
- ✅ 反馈速度：abort 是 < 1s，emulator 是 60s

**局限**：
- ❌ 只能拦"已知错误模式"，新型 bug 需要再加 strict rule
- ❌ 不能替代 T3（GPU/真渲染/真机服务交互这些 mock 不出来）
- ❌ host_test 必须真在 mock 路径上跑过这条代码 — 如果业务代码没被 host_main 驱动调到，bug 抓不到

**结论**：**P2 是 P1 的补充，不是替代**。P1 装了 Ryubing 后，commit 关卡前一定能拦下；P2 的价值是"早期反馈"，让 LLM 在 ReAct 主循环内就发现 bug，不用等 commit。

---

## 4. P0/P1/P2 综合对比

| 项 | 落地状态 | 工作量 | 反馈速度 | 拦截范围 | 推荐顺序 |
|---|---|---|---|---|---|
| P0 charter forbidden_patterns | ✅ 已上线 | — | < 1s | "已知错误代码模式"（grep 类） | 已完成 |
| P0 reviewer deliverable_completeness | ✅ 已上线 | — | LLM 评审耗时 | "deliverable 不完整"（语义类） | 已完成 |
| **P1 Ryubing T3 emulator gate** | ⏸ 待装 | ~3h kedo + ~1-2h 用户 firmware | ~60s/run | **运行时 svcBreak / GPU / 服务初始化错误** | **🔥 立即做** |
| P2 mock_libnx 扩 audren/hid abort | ⏸ 待做 | ~3h | < 1s | "已知 libnx API 误用模式" | 推荐做但可后置 |

### 关键判断

1. **P0 已经覆盖"代码长这样就拦"的所有静态可拦 bug**——只要新 bug 模式被发现，往 charter 加一条 forbidden_pattern 就行
2. **P1 是"运行时层"的最后一道闸**——`audrenInitialize(NULL)` 这种**编译期合法但平台运行时挂**的 bug 没有 P1 任何静态分析都救不了
3. **P2 是 P1 的 fast-feedback 补丁**——抓"已知错误模式"在 host 阶段，但抓不到未知模式

### 失败模式覆盖矩阵

| Bug 类型 | 历史 task | T1 | T2 | T2+P2 | T3 (P1) |
|---|---|---|---|---|---|
| 未声明函数 / 签名漂移 | b943c9d0 | ✅ | ✅ | ✅ | ✅ |
| 未初始化变量 | 多次 | ✅ | ✅ | ✅ | ✅ |
| null deref / heap overflow | switchvideo 多次 | ❌ | ✅ | ✅ | ✅ |
| `audrenInitialize(NULL)` | 6ad7d5f6 | ❌ | ❌ | ✅ (abort) | ✅ |
| `hidScanInput` deprecated | (历史) | ❌ | ❌ | ✅ (abort) | ✅ |
| GPU shader / 真渲染崩 | (未来) | ❌ | ❌ | ❌ | ✅ |
| Switch service init 错误顺序 | (未来) | ❌ | ❌ | 部分 | ✅ |
| Charter 已知违规代码模式 | 6ad7d5f6 | **P0 ✅** | — | — | — |

→ **P1 是覆盖范围最广的**。

---

## 5. 推荐实施顺序

### 选项 A — 立即做 P1（推荐）

**理由**：

1. switchvideo 当前还在 audio/video 试错阶段，每次试错都怕又一次 2168-0002 类 svcBreak。装上 Ryubing 后这一类全免疫
2. P0 已经覆盖了"静态可识别"的 bug 模式，剩下的运行时 bug 只有 P1 能拦
3. P2 是优化反馈速度的，P1 是覆盖根本盲区的——先解决覆盖问题
4. P1 一次投入 ~3h（kedo 侧）+ 1-2h（用户 firmware）后**永久受益**

**节奏**：

```
Day 1 上午：用户自己 Switch dump firmware (1-2h)
Day 1 下午：装 Ryubing + 自检 + switchvideo profile.emulator 配置 + 验证 #1/#2/#3 (3h)
Day 1 晚上：实战跑一个 task 看 emulator 行为，按需调 timeout/patterns
Day 2+：观察 1-2 周, 收集 patterns 调优
```

### 选项 B — 先做 P2 等 firmware 准备好再做 P1

**理由**：firmware dump 涉及用户自己 Switch 操作，可能等不及。P2 不依赖 firmware，可以先做。

**节奏**：

```
Day 1：P2 mock_libnx 扩展 + 验证 (3h)
Day 1+：用户找时间 dump firmware
firmware 就绪后：补 P1
```

### 选项 C — 先观察实战 1 周再决定

**理由**：P0 刚上线，charter forbidden_patterns 在实战中表现如何还没数据。观察 1 周看 LLM 实际撞 pattern 的频率，决定 P2 优先级。

**节奏**：

```
Week 1：什么也不做，观察 P0 在 switchvideo 上的拦截率
Week 1 复盘：决定 P1 vs P2 优先级
```

---

## 6. 风险评估总结

| 风险 | P1 | P2 |
|---|---|---|
| 实施失败 | 中（firmware dump 不顺） | 低 |
| 维护负担 | 低（Ryubing 装好基本不用改） | 中（新 libnx API 出现要扩 mock） |
| 误报率 | 中（Ryubing ≠ 真机 100%） | 低 |
| Rollback 简单度 | ✅ 改 profile 就 short-circuit | ✅ 删 strict rules 即可 |
| 法律风险 | 中（firmware 用户自己 dump） | 无 |
| 性能开销 | 60s/commit | < 1s/host_test |

---

## 7. 评审请求

请就以下问题给反馈：

- [ ] **顺序建议**：选 A / B / C 哪个？或者你有别的建议？
- [ ] **P1 firmware**：你的 Switch 现在能 dump 吗？还是需要先准备硬件 / 工具？
- [ ] **P2 strict abort 行为**：用 `abort()`（立即挂）还是 `_Exit(1)`（不打 core dump 但给 ASAN 看）？我倾向 abort
- [ ] **P2 strict 规则范围**：除了 `audrenInitialize(NULL)` / `hidScanInput` / `hidKeysDown`，你想加哪些？
- [ ] **P0+ 残留打磨**：1.3 的 dashboard 徽章值得做吗？还是放进等闲心思
- [ ] **整体推荐**：先做 P1 还是先做 P2？要不要并行（拆给你 + 我）？

确认 / 修改后我开干。

---

## 附：当前 P0 实战观察基线

在做 P1/P2 之前，给 switchvideo 跑下面这条**最小验证**确认 P0 已经在 charter 上有效：

```bash
ssh 192.168.1.8
cd /home/maojj/project/switchvideo

# 测试 1: 故意写一行 audrenInitialize(NULL) 触发 ProfileGuard 拦截
# (用 file_write 工具，不走 LLM)
curl -X POST http://127.0.0.1:8000/api/file/write \
  -d '{"path": "src/test_violation.c", "content": "void test(void) { audrenInitialize(NULL); }"}'
# 期望: HTTP 4xx/200 with success=false, error 含 "charter:forbidden_patterns"
```

如果 P0 在 switchvideo 上已经能拦——P1/P2 无论先做哪个都基于一个稳定基线。

