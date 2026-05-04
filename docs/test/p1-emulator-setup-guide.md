# P1 — Switch 模拟器 (Hyjinx) 装机指南

> **状态**：kedo 侧装机进行中。需要你 dump firmware 才能完整跑通。  
> **替代说明**：原文档说 "Ryubing"，但 Ryubing/Ryujinx 全镜像 2025 年被任天堂法律打掉。改用 **Hyjinx**（[hyjinx-emu/Hyjinx](https://github.com/hyjinx-emu/Hyjinx)，gdkchan 关联，Ryujinx 直接技术继承），用法基本一致。

---

## 1. kedo 侧已完成（不需要你做）

- [x] 装 .NET 8 / .NET 9 SDK 到 `~/.dotnet/`（无 sudo）
- [x] clone Hyjinx 到 `~/Hyjinx/`
- [x] dotnet build Hyjinx.Headless.SDL2（后台跑中）

build 完成后 binary 在：

```
~/Hyjinx/src/Hyjinx.Headless.SDL2/bin/Release/net9.0/Hyjinx.Headless.SDL2
```

---

## 2. 你侧要做的（按顺序）

### 步骤 A — 装 xvfb（一行命令，需 sudo）

```bash
ssh 192.168.1.8
sudo apt install -y xvfb
which xvfb-run    # 期望 /usr/bin/xvfb-run
```

为什么需要：emulator 是 GUI 程序，服务端无显示器，要 xvfb 起虚拟 X server。

### 步骤 B — 用你自己的 Switch dump firmware（**必须你自己 dump，不能下载**）

⚠️ **法律 + 安全**：firmware 必须从你拥有的物理 Switch 主机 dump。网上下载到的几乎都是带病毒或盗版，且法律上不可行。Hyjinx/Ryujinx 项目本身不分发 firmware，这是合法边界。

#### B.1 Switch 端

1. 你自己的 Switch 进入 RCM 模式（按住 Vol+ + Home + Power）
2. 用 [Lockpick_RCM](https://github.com/shchmue/Lockpick_RCM) payload 启动（用 TegraRcmGUI 注入）
3. 选 "Dump from SysNAND" → 拿到 `prod.keys` + `title.keys`
4. 用 [nxdumptool](https://github.com/DarkMatterCore/nxdumptool) homebrew 启动，dump firmware
   - 输出在 SD 卡：`switch/nxdumptool/Firmware/<version>/`
   - 一堆 `.nca` 文件（约 600 个 ~1.5GB）
5. 记下你 Switch 的系统版本号（设置 → 主机 → 系统）

#### B.2 把文件传到 192.168.1.8

```bash
# 把 SD 卡里的文件拷到本机, 然后 scp 过去
scp prod.keys title.keys 192.168.1.8:/tmp/
scp -r firmware_<version>/ 192.168.1.8:/tmp/firmware/
```

#### B.3 在 192.168.1.8 上布置到 Hyjinx 配置目录

```bash
ssh 192.168.1.8

# Hyjinx 默认配置路径
mkdir -p ~/.config/Hyjinx/system
mkdir -p ~/.config/Hyjinx/bis/system/Contents/registered

# 拷贝密钥
mv /tmp/prod.keys /tmp/title.keys ~/.config/Hyjinx/system/

# 拷贝 firmware
mv /tmp/firmware/*.nca ~/.config/Hyjinx/bis/system/Contents/registered/

# 验证
ls ~/.config/Hyjinx/system/
ls ~/.config/Hyjinx/bis/system/Contents/registered/ | wc -l   # 应该几百个
```

### 步骤 C — 装机自检（任意 .nro 验证启动链路）

用 switchvideo 当前编译产物自检（即便它会因 audren bug 崩，启动链路本身能起来就证明 emulator 装好）：

```bash
ssh 192.168.1.8
export PATH=$HOME/.dotnet:$PATH
cd /home/maojj/project/switchvideo

xvfb-run -a -s "-screen 0 1280x720x24" \
  ~/Hyjinx/src/Hyjinx.Headless.SDL2/bin/Release/net9.0/Hyjinx.Headless.SDL2 \
  --memory-manager-mode HostMappedUnsafe \
  --graphics-backend OpenGl \
  --no-input \
  build/switchvideo.nro 2>&1 | tee /tmp/hyjinx_test.log &
EMU_PID=$!
sleep 30
kill $EMU_PID 2>/dev/null

# 期望: log 里有 svcBreak / Result code 0x... → 证明启动到了 audren NULL 那段
grep -E "svcBreak|Result code|panic|consoleInit|Application loaded" /tmp/hyjinx_test.log | head -10
```

**期望看到**：
- ✅ 含 `Application loaded` / `consoleInit` 类启动成功标志
- ✅ 含 `svcBreak` / `Result code 0x...` 类 audren NULL 触发的崩溃（**这是好结果**——证明 emulator 真的能跑你的 .nro 并暴露真实 bug）

如果两类都没看到，可能是 firmware 缺失或 keys 不匹配，看 stderr 排查。

---

## 3. 启用 T3 (commit 你的 profile.emulator)

装机自检通过后，编辑 switchvideo profile：

```bash
ssh 192.168.1.8
vim /home/maojj/project/switchvideo/.kedo/project_profile.json
```

在文件里加这一段：

```json
"emulator": {
  "enabled": true,
  "command_template": "xvfb-run -a -s '-screen 0 1280x720x24' /home/maojj/Hyjinx/src/Hyjinx.Headless.SDL2/bin/Release/net9.0/Hyjinx.Headless.SDL2 --memory-manager-mode HostMappedUnsafe --graphics-backend OpenGl --no-input {artifact}",
  "timeout_s": 60,
  "success_patterns": [
    "Application loaded",
    "consoleInit",
    "main loop"
  ],
  "crash_patterns": [
    "svcBreak",
    "Result code 0x[0-9a-f]+",
    "panic",
    "Fatal exception",
    "ABI breakpoint",
    "UNHANDLED EXCEPTION"
  ],
  "required": false
}
```

**关键参数**：

| 字段 | 值 | 说明 |
|---|---|---|
| `enabled` | `true` | 启用 T3 emulator gate |
| `required` | `false` | 开发期推荐：emulator 失败仅 warn 不阻塞 commit；接近发布时切 true |
| `timeout_s` | `60` | Hyjinx 启动 5-15s + 跑 ~30s + 退出 ~5s ≈ 50s，给 60 留缓冲 |
| `success_patterns` | `["Application loaded", "consoleInit", "main loop"]` | 任一命中即视为启动到目标状态 |
| `crash_patterns` | `["svcBreak", ...]` | 任一命中即 fail |

### 没有 firmware 时的占位配置

如果你想先把 enabled=false 的占位配置 commit 进 profile，等 firmware 准备好再切 true：

```json
"emulator": {
  "enabled": false,
  "command_template": "xvfb-run -a -s '-screen 0 1280x720x24' /home/maojj/Hyjinx/src/Hyjinx.Headless.SDL2/bin/Release/net9.0/Hyjinx.Headless.SDL2 --memory-manager-mode HostMappedUnsafe --graphics-backend OpenGl --no-input {artifact}",
  "timeout_s": 60,
  "success_patterns": ["Application loaded", "consoleInit"],
  "crash_patterns": ["svcBreak", "Result code 0x[0-9a-f]+", "panic", "Fatal exception"],
  "required": false,
  "_status": "disabled until firmware dumped to ~/.config/Hyjinx/"
}
```

---

## 4. 验证 kedo 集成

启用后跑一次 kedo task 触发 commit_candidate，看 dashboard candidate 卡片是否显示 emulator 元信息：

```bash
ssh 192.168.1.8
kedo
kedo ❯ 给 switchvideo 加一行注释，build 一下，commit 候选版本
```

期望 dashboard candidate 卡片显示：
- `emulator_command`: `xvfb-run -a ... build/switchvideo.nro`
- `emulator_returncode`: 0 / 非 0
- `emulator_crashes`: 命中的 crash_patterns 列表（如有）

如果 enabled=true 且 required=false，emulator 失败时 candidate 仍创建但 panel 显示警告。

---

## 5. 常见坑

| 现象 | 原因 | 修 |
|---|---|---|
| `Hyjinx.Headless.SDL2 not found` | build 没完成 | `tail -f /tmp/hyjinx-build.log` 等 |
| 启动时报 "Missing keys" | prod.keys 路径错 | 确认在 `~/.config/Hyjinx/system/prod.keys` |
| 黑屏 / Vulkan failed | xvfb + Vulkan 不兼容 | 确认 `--graphics-backend OpenGl` |
| 启动后 hang 不退 | .nro 写了 `appletMainLoop` 死循环 | 设 `timeout_s` 足够，超时是预期 |
| 装机自检无输出 | 真的没 firmware | 重新做 B.3 |

---

## 6. 现状速查（本指南最后更新时）

| 项 | 状态 |
|---|---|
| .NET 8 SDK 装到 `~/.dotnet/sdk/8.0.412` | ✅ |
| .NET 9 SDK 装到 `~/.dotnet/sdk/9.0.100` | ⏳ 后台装 |
| Hyjinx clone 到 `~/Hyjinx/` | ✅ |
| Hyjinx headless build | ⏳ 等 .NET 9 装完后启动 |
| xvfb（系统包） | ❌ **需要你 sudo apt install** |
| Switch firmware | ❌ **需要你自己 dump** |
| switchvideo profile.emulator | ⏸ 等 build + firmware 后启用 |

---

## 7. 工作量预估（你侧）

| 任务 | 时间 |
|---|---|
| `sudo apt install xvfb` | 30 秒 |
| Switch RCM + Lockpick_RCM dump keys | 5 分钟 |
| Switch nxdumptool dump firmware | 30-60 分钟（取决于 SD 卡速度） |
| scp 文件到 192.168.1.8 + 布置到 `~/.config/Hyjinx/` | 5 分钟 |
| 装机自检（步骤 C） | 5 分钟 |
| 修 profile.emulator + 验证 commit_candidate 链路 | 10 分钟 |
| **总计** | **~1 小时**（不算 .nca dump 等待） |

---

## 8. 确认装机完成的清单

跑完所有步骤后：

```bash
ssh 192.168.1.8

# 1. emulator binary 存在
ls ~/Hyjinx/src/Hyjinx.Headless.SDL2/bin/Release/net9.0/Hyjinx.Headless.SDL2
# 期望: 文件存在

# 2. xvfb 可用
which xvfb-run
# 期望: /usr/bin/xvfb-run

# 3. firmware + keys 就位
ls ~/.config/Hyjinx/system/prod.keys
ls ~/.config/Hyjinx/bis/system/Contents/registered/ | wc -l
# 期望: prod.keys 存在; 几百个 .nca 文件

# 4. 装机自检 log 含真实启动标志
grep -E "Application loaded|consoleInit" /tmp/hyjinx_test.log
# 期望: 至少 1 行命中

# 5. profile.emulator 配好且 enabled=true
jq '.emulator' /home/maojj/project/switchvideo/.kedo/project_profile.json
# 期望: enabled=true, command_template 含 Hyjinx 路径
```

5 项都过 → T3 就绪，下一个 commit_candidate 自动调 emulator gate。

---

## 9. Rollback

T3 出问题时，最快回滚：

```bash
# profile.emulator.enabled = false → T3 完全 short-circuit
# 不需动代码, kedo 看到 enabled=false 直接 skip
```

不影响其它工具行为。
