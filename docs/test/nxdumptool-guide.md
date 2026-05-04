# nxdumptool 安装与使用指南

> ⏸ **状态：搁置中（2026-05-05）**  
> nxdumptool 的核心用途是 **dump firmware（NCA）给模拟器加载**。当前公开生态没有可 build 的 headless Switch emulator（详见 [p1-emulator-setup-guide.md](p1-emulator-setup-guide.md) 死局说明），所以本文描述的 firmware dump 流程**也跟着搁置**。  
>
> ★ 真机崩溃定位**不需要本工具** — 直接走 [switch-coredump-guide.md](switch-coredump-guide.md) 即可（Atmosphere 自带 creport 写到 `<SD>/atmosphere/crash_reports/`，PC 端 `addr2line` 解就行，零依赖）。  
>
> 本文继续保留：等社区出活的 headless Switch emulator 时直接启用，不用从头再调研。

---

> **目的**：从你自己的 Switch dump firmware（NCA 文件）和验证 prod.keys。  
> **场景前提**：硬破 + Atmosphere CFW + hbmenu 可用。  
> **关联**：[p1-emulator-setup-guide.md](p1-emulator-setup-guide.md) — 模拟器装机流程（已搁置）；本文是该流程的前置 dump 步骤。

---

## 0. 选哪个版本

DarkMatterCore/nxdumptool 的 [release 页面](https://github.com/DarkMatterCore/nxdumptool/releases) 现在有两条线：

| 版本 | 类型 | 推荐场景 |
|---|---|---|
| **`rewrite-prerelease`**（2023-10-15） | 重写版主开发分支 | **当前推荐** — 功能完整、UI 现代、firmware dump 模式更全 |
| `v1.1.15` (2022-02-26) | 旧稳定版 | 兼容性兜底，UI 老但稳 |

→ **直接用 `rewrite-prerelease`** 的 `nxdt_rw_poc.nro`（2.6 MB）。

---

## 1. 装到 SD 卡

### 1.1 下载

```bash
# 在 PC 上下载
curl -LO "https://github.com/DarkMatterCore/nxdumptool/releases/download/rewrite-prerelease/nxdt_rw_poc.nro"
ls -lh nxdt_rw_poc.nro    # 期望 ~2.6 MB
```

### 1.2 拷到 SD 卡

把 SD 卡插进电脑，建一个目录放：

```
<SD>/switch/nxdumptool/
└── nxdt_rw_poc.nro
```

或者直接放 `<SD>/switch/nxdt_rw_poc.nro`（hbmenu 自动扫描 `/switch/`，子目录或顶层都行）。

### 1.3 SD 卡放回 Switch

---

## 2. 启动

1. Switch 进 hbmenu（系统 → 相册 / 任意标题，根据你的 Atmosphere 配置）
2. 找到 **`nxdumptool`** 或 **`nxdt_rw_poc`** 图标启动
3. 首次启动会扫描 NAND，约 5-15 秒

> 启动失败：可能是 prod.keys 缺失或过期。先跑一次 Lockpick_RCM 拿最新 keys（见 [P1 指南](p1-emulator-setup-guide.md) 步骤 B）。

---

## 3. dump firmware（emulator 装机用）

### 3.1 进入 firmware dump 菜单

主菜单 → **`SD card / eMMC operations`**（或类似名字，不同版本略有差异） → **`Dump system firmware`** 或 **`Dump system update content`**。

### 3.2 选 dump 模式

通常有 3 个选项：

| 选项 | 输出 | 用途 |
|---|---|---|
| **`Dump system update partition (whole NCA set)`** | 几百个 `.nca` | 模拟器加载所需的完整 firmware（**emulator 用这个**）|
| `Dump system NAND CAL0` | calibration 数据 | 模拟器初始化时偶尔需要 |
| `Dump exFAT update` | exFAT 驱动 | 一般用不到 |

→ 选 **第 1 个 "system update partition"**。

### 3.3 选输出路径

通常默认 `<SD>/switch/nxdumptool/Firmware/<system_version>/`，保持默认即可。

### 3.4 等

dump 一份完整 firmware：
- SD 卡速度好：~15-30 分钟
- SD 卡慢：~60-90 分钟

中途**别按 HOME 键**，会被 Atmosphere 当成主动退出，dump 中断。

### 3.5 完成后的产物

```
<SD>/switch/nxdumptool/Firmware/<version>/
├── 010000000000080A.nca   ← 类似这样几百个文件
├── 010000000000080B.nca
├── ...
└── (约 600 个 .nca, 总计 ~1.5 GB)
```

---

## 4. dump titles / saves（开发常用）

如果你是要 dump 自己装的 homebrew 测试 saves，主菜单 → **`Title operations`**：

- **`Dump installed application`** — dump 装好的 .nsp / 游戏卡带
- **`Dump system save data`** — dump 系统级 save（不常用）
- **`Dump installed update`** — dump game update

---

## 5. dump prod.keys（用 nxdumptool 行不行？）

**不太行**。nxdumptool 的 `Dump system NAND keys` 选项只 dump 部分系统 keys，不包括完整的 `prod.keys` / `title.keys`。

**正确工具是 Lockpick_RCM**（RCM payload，不是 hbmenu 的 .nro）：

1. 用 [Kofysh/Lockpick_RCM](https://github.com/Kofysh/Lockpick_RCM/releases) 的 `Lockpick_RCM.bin`
2. 通过 Hekate 的 "Payloads" 菜单或 PC 端 TegraRcmGUI 注入
3. 选 "Dump from SysNAND" → 输出 `prod.keys` + `title.keys` 到 `<SD>/switch/`

**Tip**：很多 Atmosphere bundle（如 SXOS Replacer 类一键安装包）已经把 keys 放在 `<SD>/switch/prod.keys`。先看看：

```bash
ls <SD>/switch/prod.keys
```

有就不用跑 Lockpick_RCM 了。

---

## 6. 把 dump 拷出来的方法

### 6.1 SD 卡放电脑（最简单）

```bash
# 把 SD 卡插读卡器
mkdir -p ~/switch_dumps
cp -r /Volumes/SD/switch/nxdumptool/Firmware/<version>/ ~/switch_dumps/firmware/
cp /Volumes/SD/switch/prod.keys /Volumes/SD/switch/title.keys ~/switch_dumps/
```

### 6.2 用 nxdumptool 的 USB host 模式（不取 SD 卡）

`rewrite-prerelease` 版有 USB host 功能：用 `nxdt_host.py`（PC 端 Python 程序）通过 USB 直接抓 dump 流，不写 SD 卡。

```bash
# PC 上
unzip nxdt_host.7z
python3 nxdt_host.py -o ~/switch_dumps/

# Switch 上 nxdumptool 选 "USB host" 输出模式 → dump
# Switch USB 接 PC, 数据流式传过来
```

适合 SD 卡剩余空间不够时用。

### 6.3 ftpd-pro / nxmtp 网络拉

如果不想插 SD 卡，装 ftpd-pro homebrew 跑起来后 PC 用 sftp 拉：

```bash
sftp -P 5000 root@<SWITCH_IP>
sftp> get -r /switch/nxdumptool/Firmware/
```

---

## 7. dump 拷到 192.168.1.8 部署到 Hyjinx

> 即便 P1 当前 emulator 死局，把 firmware 留在 192.168.1.8 不算亏 — 等社区出活的 headless emulator 直接能用。

```bash
# 在你的 PC 上
scp -r ~/switch_dumps/firmware/ 192.168.1.8:/tmp/switch_firmware/
scp ~/switch_dumps/prod.keys ~/switch_dumps/title.keys 192.168.1.8:/tmp/

# 在 192.168.1.8 上 (按 Hyjinx 默认路径布置)
ssh 192.168.1.8
mkdir -p ~/.config/Hyjinx/system
mkdir -p ~/.config/Hyjinx/bis/system/Contents/registered
mv /tmp/prod.keys /tmp/title.keys ~/.config/Hyjinx/system/
mv /tmp/switch_firmware/*.nca ~/.config/Hyjinx/bis/system/Contents/registered/

# 验证
ls ~/.config/Hyjinx/system/
ls ~/.config/Hyjinx/bis/system/Contents/registered/ | wc -l    # 期望几百个
```

(Ryujinx 同结构，只是路径换 `~/.config/Ryujinx/`)

---

## 8. 时间预算

| 步骤 | 时间 |
|---|---|
| PC 端下载 nxdt_rw_poc.nro 拷 SD | 1 min |
| Switch 启动 hbmenu + nxdumptool | 1 min |
| dump system update partition | 15-30 min（SD 速度决定） |
| 拷出来 + scp 到 192.168.1.8 | 5 min |
| **总计** | **~25-40 分钟** |

加 prod.keys 也要 dump（用 Lockpick_RCM RCM payload）：

| 额外步骤 | 时间 |
|---|---|
| Switch 进 RCM + 注入 Lockpick_RCM | 2 min |
| Lockpick 跑（dump SysNAND keys） | 30-60 秒 |
| 重新进 Atmosphere | 30 秒 |

---

## 9. 常见问题

### 9.1 nxdumptool 启动黑屏

`prod.keys` 太老（你 Switch 系统升级过，新版需要新 keys）。先用 Lockpick_RCM 重新 dump 一次 keys，覆盖 `<SD>/switch/prod.keys`，然后再启 nxdumptool。

### 9.2 "Failed to parse keys" 类错

同上，keys 过期。

### 9.3 dump 中断 → 部分 .nca 文件

直接 nxdumptool 选 "Resume" 或重新跑一次会覆盖。**安全的，重做一次就行**。

### 9.4 SD 卡满

dump 一份 firmware ~1.5 GB。先清空 `<SD>/switch/nxdumptool/Firmware/旧版本/` 再 dump 新的。

### 9.5 跑 Lockpick_RCM 时 RCM 注入失败

- 用 hekate（推荐）：把 `Lockpick_RCM.bin` 放 `<SD>/bootloader/payloads/`，进 hekate → "Payloads" → 选
- 用 PC TegraRcmGUI：Switch RCM 模式 + USB 接 PC，TegraRcmGUI 选 .bin 注入

### 9.6 我已经有 prod.keys 但太老怎么办

直接覆盖。Lockpick_RCM 跑一次拿最新版，旧版本 keys 直接丢掉。新 keys 向下兼容（能用来 dump 老内容）。

---

## 10. 下次升级 firmware 时怎么办

每次 Switch 系统升级（你 Atmosphere 跟随官方系统升级时），`prod.keys` 和 `firmware/*.nca` 都要重新 dump：

```bash
# 1. 重新跑 Lockpick_RCM 拿新 prod.keys
# 2. 重新跑 nxdumptool dump 新 firmware
# 3. 把 192.168.1.8 上 ~/.config/Hyjinx/{system,bis/...} 全清空覆盖
```

否则模拟器加载新 .nro 时会因 keys/firmware 不匹配挂掉。

---

## 11. 跟 P1 emulator 死局的关系

即使 P1 当前模拟器无法 build，**firmware dump 仍然有价值**：

1. 留在 192.168.1.8 上，等社区出活的 headless emulator 直接能用
2. 可以本机装 Hyjinx GUI 版 + xvfb + xdotool 自动化（虽然复杂）
3. 你自己 PC 上装 GUI Hyjinx 测 .nro，跟实战部署到真 Switch 互补

→ **如果你愿意 dump 一次 firmware，存 192.168.1.8 上**，P1 重启时省掉这道步骤。

不愿意 dump 也没事，P0 + P2 + 真机 crash_reports（[switch-coredump-guide.md](switch-coredump-guide.md)）已经是务实兜底。

---

## 12. 工具快查

| 工具 | 用途 | 仓库 |
|---|---|---|
| **nxdumptool (rewrite)** | dump firmware / titles / saves | [DarkMatterCore/nxdumptool](https://github.com/DarkMatterCore/nxdumptool) |
| **Lockpick_RCM (Kofysh)** | dump prod.keys / title.keys | [Kofysh/Lockpick_RCM](https://github.com/Kofysh/Lockpick_RCM) |
| **Hekate** | bootloader + payload 启动器 | [CTCaer/hekate](https://github.com/CTCaer/hekate) |
| **TegraRcmGUI** | PC 端 RCM 注入工具 | [eliboa/TegraRcmGUI](https://github.com/eliboa/TegraRcmGUI) |
| **ftpd-pro** | Switch 端 FTP 服务器 | hbmenu 商店里搜或 [mtheall/ftpd](https://github.com/mtheall/ftpd) |
