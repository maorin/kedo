# Switch 应用崩溃信息获取指南

> **目的**：当 .nro 在 Switch 真机上崩溃（如 `Error 2168-0002`、黑屏、闪退）时，怎么把现场信息取出来供 kedo 或人工分析。  
> **场景前提**：你的 Switch 已硬破，跑 Atmosphere CFW，能进 hbmenu。  
> **关联**：[p1-emulator-setup-guide.md](p1-emulator-setup-guide.md) — 模拟器跑（已搁置）；本文是真机崩溃定位的实操替代。

---

## 0. 选哪种方法

| 场景 | 推荐方法 |
|---|---|
| **正在调试期间，需要实时 stdout** | 方法 A：nxlink + 监听 stderr（最快） |
| **崩溃后已经退出，想看堆栈** | 方法 B：Atmosphere `crash_reports/` 离线读 |
| **黑屏 + 错误码全屏（fatal）** | 方法 C：Atmosphere `fatal_errors/` 离线读 |
| **想看 svcOutputDebugString 输出** | 方法 A，否则丢失 |

实战推荐：**A 主用 + B 兜底**。

---

## 1. 方法 A：nxlink 实时 stdout / stderr

最方便的方式 — Switch 用 `consoleDebugInit(debugDevice_SVC)` 或 `printf` / `fprintf(stderr, ...)` 时，输出通过 **`nxlink`** 协议传到 PC。

### 1.1 PC 端启动监听

```bash
# nxlink 是 devkitPro 的标准工具
$DEVKITPRO/tools/bin/nxlink -s -a <SWITCH_IP>

# -s = server 模式, 监听 28280 端口
# -a <IP> = Switch IP（在 Switch 设置 → 网络里看）
```

监听后会等待 Switch 端连接、然后实时打印 stdout/stderr。

### 1.2 部署 + 自动收集

```bash
# 一行 — 部署 + 监听
$DEVKITPRO/tools/bin/nxlink -a 192.168.1.100 \
    /path/to/build/switchvideo.nro \
    | tee /tmp/nro_run.log
```

**Switch 端必须满足**：
- `hbloader` 已经在跑（任何用 hbmenu 启动的 .nro 都自动开 hbloader）
- 路由器允许 28280/tcp 双向

### 1.3 在源码里把 stdout 送到 nxlink

很多 .nro 默认 stdout 不走 nxlink；需要在 main 开头：

```c
// 把 stdout/stderr 重定向到 svcOutputDebugString → nxlink 接收
extern "C" void __libnx_initheap(void);
void userAppInit(void) {
    socketInitializeDefault();
    nxlinkStdio();      // ← 这一行: 让 stdout/stderr 走 nxlink
}
void userAppExit(void) {
    socketExit();
}
```

或更简洁版（不依赖 socket，**调试期推荐**）：

```c
#include <switch.h>
int main(void) {
    consoleDebugInit(debugDevice_SVC);   // 输出走 svcOutputDebugString
    fprintf(stderr, "Hello from Switch\n");
    // ...你的代码
}
```

`debugDevice_SVC` 模式下，stderr 自动通过 `svcOutputDebugString` 调用，被 Atmosphere stratosphere 转发——可被 nxlink server 抓到，**或** Atmosphere 的 `crash_reports` 也会收录最近的 svc debug strings。

### 1.4 实战例子（你的 switchvideo task 6ad7d5f6 audrenInitialize NULL 应该会输出）

```
[main] framebufferCreate failed: 0x0
[vp] avformat_network_init OK
[vp] audrenInitialize failed: 0x...    ← 这条本应输出但 audrenInitialize NULL 直接 svcBreak
                                       不会先打印任何东西
```

→ 实际上 `audrenInitialize(NULL)` 触发的是 **assert/svcBreak**，不会先 fprintf。这种情况靠 nxlink 看不到，要走方法 B / C。

---

## 2. 方法 B：Atmosphere `crash_reports/`

**程序异常退出（segfault / unhandled exception / 主动 abort）后**，Atmosphere 的 **creport sysmodule** 自动捕捉并写崩溃报告到 SD 卡。

### 2.1 文件位置

```
<SD>/atmosphere/crash_reports/
├── <YYYYMMDDHHMMSS>_<title_id>.log    ← 人可读文本日志
└── dumps/
    └── <title_id>_<process_id>_thread<N>.dmp    ← 二进制堆栈快照
```

`title_id`：homebrew .nro 通常是 `0100000000010000`（hbloader 的固定 ID），多个 .nro 共享。看 `process_name` 区分。

### 2.2 .log 文件长什么样

```
Atmosphère Crash Report (v1.7.x):
Result:                          0x000000XX (xxxx-xxxx)
Process Info:
    Process Name:                switchvideo
    Title ID:                    0100000000010000
    Process ID:                  0x000000000000007F
    Process Flags:               0x00000033
    Userland Process:            yes

Exception Info:
    Type:                        Bad-svcBreak
    Address:                     0x000000000xxxxxxx (in switchvideo)
    BreakReason:                 Assert
    Memory Address:              0x000000000xxxxxxx (in switchvideo)
    Memory Size:                 0x0000000000000010

CPU Registers:
    X0:                          0x...
    X1:                          0x...
    ... [X0-X28]
    PC:                          0x000000000xxxxxxx (in switchvideo + 0xXXXX)
    LR:                          0x000000000xxxxxxx (in switchvideo + 0xXXXX)
    SP:                          0x...
    ...

Stack:
    0x000000000xxxxxxx (in switchvideo + 0xXXXX) <- 当前帧
    0x000000000xxxxxxx (in switchvideo + 0xXXXX) <- 上一帧
    ...

Module Info:
    switchvideo (0x...)
        Build ID:                <hex>
```

### 2.3 怎么把 PC 当前帧映射回源码行号

**关键步骤** — 拿 `PC` / 栈帧的 `+0xXXXX` 偏移配合 `.elf`（带 debug 符号的版本）：

```bash
# 把 build/switchvideo.elf 拷到 PC
# 用 devkitPro 的 aarch64-none-elf-addr2line
$DEVKITPRO/devkitA64/bin/aarch64-none-elf-addr2line \
    -e build/switchvideo.elf \
    -f -C -i \
    0xXXXX     # ← crash report 里 "in switchvideo + 0xXXXX" 的那个偏移
```

输出：

```
audrenInitialize_inline
/home/maojj/project/switchvideo/src/video_player.c:92
```

→ 直接定位到出问题的源码行。

### 2.4 取出 crash report 的两种方式

#### 方式 1 — SD 卡放电脑

```bash
# 把 SD 卡插到电脑读卡器
ls <SD>/atmosphere/crash_reports/
cp -r <SD>/atmosphere/crash_reports/ ~/switch_crashes/
```

#### 方式 2 — Switch 上用 ftpd 或 nxmtp 远程拉

如果你装了 ftpd-pro 之类的 homebrew，可以用 sftp 直接从 PC 拉：

```bash
sftp -P 5000 root@192.168.1.100   # ftpd-pro 默认 5000 端口
sftp> get -r /atmosphere/crash_reports
```

或者用 `nxmtp` 把 Switch 当 USB MTP 设备挂到 PC。

### 2.5 实战识别 audrenInitialize NULL

如果你跑了带 audren bug 的 switchvideo.nro，crash_reports 里会有：

```
Result:           0x4a8 (2168-0002)   ← 跟 Switch 屏幕显示的错误码对应
Type:             Bad-svcBreak
BreakReason:      Assert
PC:               <某地址> (in switchvideo + 0xXXXX)
```

`addr2line` 上面那个 0xXXXX 偏移 → 对应 `video_player.c:92` 的 `audrenInitialize(NULL)` 调用。

---

## 3. 方法 C：Atmosphere `fatal_errors/`

跟 crash_reports 类似但触发条件不同：**fatal 是系统级错误**（不可恢复 / svcBreak with fatal flag），全屏黑底显示错误码（你的 2168-0002 就是这种）。

### 3.1 文件位置

```
<SD>/atmosphere/fatal_errors/
└── report_<YYYYMMDDHHMMSS>.bin   ← 二进制
```

### 3.2 用 BootMii / 第三方工具解码

`.bin` 文件不是文本。需要工具：

- **switch-fatal-decoder** （社区）— 把 .bin 转成可读文本
- 或手动十六进制解读：前 0x10 字节是 magic，后面是 result code + register dump

实操推荐：**fatal_errors 同步看 crash_reports**——很多 fatal 也会在 crash_reports 里有对应记录（fatal handler 触发后 creport 也会跑），后者更易读。

### 3.3 全屏错误码本身的解读

Switch 黑屏显示的 `2168-0002 (0x4a8)`：

- 格式：`<module>-<description>` (Result code hex)
- `2168` = module 0x68 (libnx fatal)
- `0002` = description 0x02
- `0x4a8` = 完整 raw result code = `(description << 9) | module = (2 << 9) | 0x68 = 0x4a8`

**常见 module/description 对照**：

| 错误码 | 含义 |
|---|---|
| `2168-0002` (0x4a8) | libnx fatal — 通常是 svcBreak from libnx assert（如 `audrenInitialize(NULL)`）|
| `2168-0000` | libnx fatal — generic |
| `2002-XXXX` | FS (文件系统) 相关 |
| `2110-0XXX` | nvServices 相关（GPU / 显示） |
| `2210-0XXX` | sm 服务名称相关（权限 / 服务找不到） |

完整对照表：[switchbrew.org/wiki/Error_codes](https://switchbrew.org/wiki/Error_codes)

---

## 4. 给 kedo 集成的建议（未来）

T3 真模拟器路径搁置后，可以让 kedo 在 deploy step 后自动拉 crash_reports：

```yaml
# profile.deploy.command 之后加 post_deploy.command:
post_deploy:
  command: |
    sleep 30  # 等 Switch 跑完
    sftp -b - root@$SWITCH_IP:/atmosphere/crash_reports << EOF
      lcd /tmp/switch_crashes_${TASK_ID}/
      get -r .
    EOF
    # 然后 grep 最新的 .log + addr2line
    LATEST=$(ls -t /tmp/switch_crashes_${TASK_ID}/*.log | head -1)
    grep -E "Result:|in switchvideo \+" "$LATEST"
```

这样 task 跑完自动收集崩溃报告 → ReactAgent 看到 stderr 含 svcBreak 类信息就能继续迭代修复。这是 P1 (Ryubing) 死局后的实用替代。

---

## 5. 工具清单

| 工具 | 用途 | 装在哪 |
|---|---|---|
| `nxlink` | PC 端 stdout 接收 + .nro 部署 | `$DEVKITPRO/tools/bin/nxlink`（已有） |
| `aarch64-none-elf-addr2line` | crash 偏移 → 源码行号 | `$DEVKITPRO/devkitA64/bin/aarch64-none-elf-addr2line`（已有） |
| `aarch64-none-elf-objdump` | 反汇编 .elf 看具体指令 | `$DEVKITPRO/devkitA64/bin/aarch64-none-elf-objdump`（已有） |
| `ftpd-pro.nro` | Switch FTP server | 装到 SD `/switch/` |
| `nxmtp.nro` | Switch USB MTP | 装到 SD `/switch/`（可选） |

---

## 6. 你的 switchvideo `6ad7d5f6` 案例实操

立刻可以做的：

```bash
# 1. SD 卡插电脑, 看 crash_reports
ls <SD>/atmosphere/crash_reports/

# 2. 找最新一个 .log
LATEST=$(ls -t <SD>/atmosphere/crash_reports/*.log | head -1)
cat "$LATEST" | head -50

# 3. 提取 PC 偏移 + addr2line
PC_OFFSET=$(grep "PC:" "$LATEST" | grep -oE "\+ 0x[0-9a-f]+" | head -1 | sed 's/+ //')
$DEVKITPRO/devkitA64/bin/aarch64-none-elf-addr2line \
    -e build/switchvideo.elf -f -C -i $PC_OFFSET
# 期望: video_player.c:92 audren_output_init / audrenInitialize 之类
```

如果这条路径能跑通，你就**不依赖任何模拟器**也能拿到 .nro 崩溃的精确位置。
