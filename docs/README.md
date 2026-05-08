# kedo 文档索引

> 这里按"读这个顺序"组织 kedo 全部文档。最后更新 2026-05-08。
>
> 命名约定：
> - **状态 reference** = "现在代码是什么样的" 实施说明（看代码也能反推出来，但写下来省时间）
> - **deep-dive** = "为什么这么做、还有哪些 gap、下一步候选" 设计讨论
> - **how-to** = 操作步骤（装环境、跑测试、抓崩溃等）

---

## 🚀 起步 / 入门

刚来项目先看这几篇，按顺序：

| 文件 | 内容 | 行数 |
|---|---|---|
| [architecture.md](architecture.md) | kedo 是什么、ReactAgent 架构、15→26 工具一览 | ~180 |
| [roadmap.md](roadmap.md) | 现状 + 下一步 + 长期方向；workstream 总览表 | ~180 |
| [reference.md](reference.md) | Dashboard 用法 / CLI 命令 / 常用 endpoint | ~120 |
| [llm-providers.md](llm-providers.md) | 支持的 LLM 提供商、配置、quirk 速查 | ~90 |
| [changelog.md](changelog.md) | 改进历程（G1-G6 实战修复等） | ~30 |

---

## 🏗 架构与设计落地（"现在代码是这样"）

实施 reference，而不是设计讨论。设计动因看 deep-dives/。

| 文件 | 内容 |
|---|---|
| [dual-agent-architecture.md](dual-agent-architecture.md) | 方案 C / Actor-Critic（Producer + Reviewer）落地状态。✅ 2026-04-25 完成 |
| [virtual-test-strategy.md](virtual-test-strategy.md) | T1/T2/T3 三层虚拟测试。T1/T2 ✅、T3 模拟器搁置→真机 coredump 替代 |
| [deep-dives/browser-bridge-design.md](deep-dives/browser-bridge-design.md) | Browser Bridge 完整设计 + M1-M5 路线（M1+M2 ✅） |

---

## 🔍 深度讨论（"为什么这么做"）

`deep-dives/` 子目录有自己的 [完整索引](deep-dives/README.md)。10 篇文档，分两类：

**核心问题答疑**（context anxiety / self-eval drift / planning instability / tool fragility / long-horizon memory / hallucinated execution）

**架构演进设计**（multi-agent / agent-workflow-hybrid / browser-bridge / kedo-as-skill）

→ 直接看 [deep-dives/README.md](deep-dives/README.md)

---

## 📖 开发指南（how-to）

`test/` 下放的是装环境 + 跑测试 + 抓崩溃的实操步骤。注意几篇是**搁置状态**（替代方案见每篇顶部）。

### 测试

| 文件 | 状态 | 用途 |
|---|---|---|
| [test/virtual-test-cases-switchvideo.md](test/virtual-test-cases-switchvideo.md) | 活跃 | T1/T2/T3 在 switchvideo 真实项目上的测试用例集 |
| [test/long-term-plan-after-6ad7d5f6.md](test/long-term-plan-after-6ad7d5f6.md) | 进行中 | post-6ad7d5f6 长期改进方案（P0/P1/P2 切片） |

### 真机调试

| 文件 | 状态 | 用途 |
|---|---|---|
| [test/switch-coredump-guide.md](test/switch-coredump-guide.md) | ✅ 推荐 | 真机崩溃定位（Atmosphere creport + addr2line）— 实战首选 |

### 模拟器路径（搁置）

| 文件 | 状态 | 用途 |
|---|---|---|
| [test/p1-emulator-setup-guide.md](test/p1-emulator-setup-guide.md) | ⏸ 搁置 | Hyjinx 模拟器装机；公开生态无 headless 模拟器导致死局 |
| [test/nxdumptool-guide.md](test/nxdumptool-guide.md) | ⏸ 搁置 | firmware dump（给模拟器用）— 同上理由跟着搁置 |

> **替代决策**（2026-05-05）：T3 模拟器路径搁置，改用 [test/switch-coredump-guide.md](test/switch-coredump-guide.md) 真机崩溃信息抓取 + addr2line 解析。详见 `roadmap.md` "Virtual Test" workstream。

---

## 📝 示例

| 文件 | 用途 |
|---|---|
| [examples/switchvideo_charter.md](examples/switchvideo_charter.md) | 一份真实 frozen charter 示例（switchvideo 项目，post-drift incident） |

---

## 常见导航场景

**"我是新贡献者，从零开始"**
→ architecture → roadmap → reference → 任选一篇 deep-dive

**"想了解 kedo 为什么用 ReactAgent 不用 X"**
→ deep-dives/agent-workflow-hybrid.md + deep-dives/multi-agent-architecture.md

**"想跟 kedo 集成（外部调用）"**
→ deep-dives/kedo-as-skill-and-skill-host.md（探讨稿，未实施）+ roadmap "Skill 暴露" workstream

**"调试 Switch homebrew 崩了"**
→ test/switch-coredump-guide.md（直接走真机，别绕模拟器）

**"想给 kedo 加新 LLM 提供商"**
→ llm-providers.md → 看 `api/server.py` create_llm_client 现有实现

**"agent 行为有 bug，怀疑 prompt 或工具"**
→ deep-dives/tool-fragility.md + deep-dives/hallucinated-execution.md

---

## 维护说明

- 新增文档：放对应分类目录（test / examples / deep-dives），在本 README 加一行
- 文件改名 / 移位：grep 全仓 + memory + dashboard 看是否有外链，先修引用再 rename
- 文档过时：在文档顶部加 banner 说明替代方案（参考 nxdumptool-guide.md / p1-emulator-setup-guide.md 模板），不要直接删
- 状态变化（active ↔ 搁置 ↔ 完成）：本 README 同步标
