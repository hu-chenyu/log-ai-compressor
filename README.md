# log-ai-compressor · 本机日志取证层

> **我们不把日志交给大模型。我们用确定性算法把日志变成可复核的证据，大模型只读证据。**
> 拿不出因果链就说证据不足，并精确列出还缺什么才能定论 —— 而且会对这些缺口负责：补上了就把缺口划掉。
> **全部计算在本机完成，日志不出网。**

[![CI](https://github.com/hu-chenyu/log-ai-compressor/actions/workflows/ci.yml/badge.svg)](https://github.com/hu-chenyu/log-ai-compressor/actions)
[![PyPI](https://img.shields.io/pypi/v/log-ai-compressor.svg)](https://pypi.org/project/log-ai-compressor/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/log-ai-compressor/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/hu-chenyu/log-ai-compressor/blob/main/LICENSE)

![Web 界面：判定卡片给出「不可作为根因输出」，下方是按 trace_id 重建的调用链](./docs/images/web-ui.jpg)

---

## 1. 这个工具和别的日志工具有什么不一样

绝大多数日志工具（包括带 AI 的）都在优化**命中率** —— 尽可能多地给出根因。
但这有个没被解决的问题：**一个自信的错误根因，比没有根因更糟**，因为值班的人会照着它去查。

举个实测例子。同一份 20 万行日志里：

| | 出现次数 | 真实角色 |
| --- | --- | --- |
| `connection pool exhausted` | **370** | 症状 |
| `Connection is not available` | **10** | **真正的因** |

按频率排，工具一定会说「根因：连接池耗尽」。**这是错的。** 真正的因只出现 10 次，被当成了噪音。

本工具的做法：

```
判定：可能原因 —— 统计推断，非因果证明
不可作为根因输出

最可能的原因：connection pool exhausted —— 但这是统计推断，不是因果证明

补齐下列证据才能定论：
  · 缺少确定性因果链   —— 带完整异常堆栈的日志（尤其是 Caused by: 链）
  · 存在难以区分的并列候选 —— 能区分二者的额外信号（调用链先后、指标对比）
  · 无异常堆栈        —— 未做异常栈裁剪的完整堆栈
```

**这就是本项目与全品类工具的核心差异**，完整论证见 [`docs/POSITIONING.md`](docs/POSITIONING.md) §3.4。

---


---

## 每条结论都能被独立复核 —— 这是本项目最硬的一条

上面那三档置信度，如果无法验证，就只是三张标签。所以每条结论都绑定一份
**证据引用**：文件、行号区间、该段原文的 sha256 摘要。

```console
$ log-ai-compressor run app.log -f json -o report.json
$ log-ai-compressor verify --report report.json
模式：复核已导出报告
证据：共 8 条引用，匹配 8 条，失配 0 条，源不可读 0 条
确定性指纹：37a53c71…139ae06a8a17
```

摘要不是装饰。它让「结论来自这段原文」从一句断言变成可验证的命题：

```console
# 事故复盘时，有人把日志里的主机名改了
#   db-primary:5432  →  db-replica:6432
$ log-ai-compressor verify --report report.json
证据：共 8 条引用，匹配 7 条，失配 1 条
  ✗ MISMATCH ca0e981df461 app.log 期望 c33aeb824ce67aaf 实际 4a1689f3780880ae
自校验结论：TAMPERED        # 退出码 1
```

判定只有三档，**没有「大部分通过」** —— 一份掺了假证据的报告不该因为大部分
为真就放行，那是取证工具最不能犯的错。粘贴文本没有可回读的源文件时如实报
`NO_SOURCE`，不谎称通过。

**这个工具不需要你相信它。** 你可以自己跑这一步。

### 确定性：同一份日志跑两遍，结论逐字节相同

报告头部与 API 都带一个 sha256「确定性指纹」（耗时、速率这类每次都不同的
数字已排除）。同一份输入跑两次指纹不一致，就是回归。CI 里跑三次：

```console
$ python scripts/adversarial_eval.py    # 见下
$ # CI: 同一份日志连跑三次，指纹必须唯一
```

这条约束是「结论可写进事后复盘」的前提 —— 不能复现的结论没法归档、没法追责。

---

## 对抗性评测：专门设计来误导这个工具的场景

上面所有数字都由本项目自己产出，所以另配了一组**专门用来让本工具出错**的
场景，按四条硬规则打分。这是本项目唯一能证明自己没在自说自话的方式。

```console
$ python scripts/adversarial_eval.py
● structured_trace_not_causal   [4/4]  verdict=LIKELY  簇=3 CONFIRMED=0
  ✓ abstain_correctly       verdict=LIKELY, can_conclude=False, CONFIRMED 簇 0 个
  ✓ causal_language_ok      非 CONFIRMED 簇未使用因果措辞
  ✓ trace_chain_found       最长链路覆盖 3 个服务（要求 ≥3）
  …
────────────────────────────────────────────────────────────
  场景数              : 10
  评分项 / 通过        : 35/35  (100.0%)
  正确弃权率           : 100.0%  （5 个应弃权场景）
  自信错误率           : 0.0%    ← 最该压到 0 的那个数
  总判定               : PASS
```

10 个场景包括：红鲱鱼部署、关键词堆砌的良性日志、共现无因果、单模块无栈、
无时间戳、**反向**场景（证据其实够了，不该虚报缺口）、结构化日志调用链、
**有完整调用链但无 Caused-by**、以及普通文本被误吞成 logfmt。

两条口径写死在脚本里，评审前先看这两条：

- **弃权算通过，不算失败。** 在确实无法定论的事件上正确弃权是这个工具想要
  的行为。只算命中率的评测会偏好猜得最自信的系统 —— 那正是要淘汰的。
- **自信错误率权重最高。** 值班的人会照着一个自信的错误根因去排查。

这套评测已经抓出过真 bug：原实现把「时间上紧邻在前的另一个错误簇」当成
因果因，标成 CONFIRMED，一条 `INFO request started` 因此被判成确定性根因。

**已接入 CI**（`.github/workflows/ci.yml`）：评分卡不过，CI 变红。

### 一条命令验证本文所有数字

上面每个数字都可以自己复现，不需要相信作者：

```console
$ python scripts/prove.py
==========================================================================
自证：sample_system.log —— 不必相信作者，自己跑
==========================================================================
  PASS  确定性：同输入同结论
        3 次运行产生 1 个指纹 （一致）
  PASS  证据可复核：每条结论带行号出处且核对通过
        8/8 个簇带出处；自校验 VERIFIED，匹配 8/8
  PASS  置信档位：无因果链时零确定性断言
        日志含 Caused-by：True；判 CONFIRMED 0 个；verdict=LIKELY
  PASS  压缩：投喂口径远小于原文，且完整报告体积如实报出
        原始 ≈934 tok → summary ≈453 tok （2.1x）→ md ≈9,204 tok
  PASS  对抗性评测：该弃权时确实弃权
        10 场景 35/35 通过；正确弃权率 100.0%；自信错误率 0.0%
  PASS  结构化日志：JSON 认领 / 普通文本不误判
  PASS  轻量安装：硬依赖不含 Web/MCP/AI
        硬依赖 ['PyYAML>=6.0']

尚无数据的主张（刻意列出，不藏）
  ?  根因判定的真实数据集覆盖度
  ?  与云平台根因准确率的对比
```

注意压缩那一行：在这个 **52 行的小日志**上，完整 md 报告比原文**大 9.9 倍**。
脚本不回避这一点 —— 完整报告带样例与上下文，小文件上必然比原文长，这是事实。
压缩比 15x–388x 那组数字来自 Loghub 真实数据集，口径见 §6.1 与
[`docs/BENCHMARK.md`](docs/BENCHMARK.md)，**不要拿本节的小文件数字去宣称压缩比**。

`prove.py` 也是 CI 门禁之一：README 与产品不允许漂移。

---


---

## 实测：5 个真实故障日志上的「压缩比 vs 证据保留率」

在 [Loghub-2.0](https://zenodo.org/record/8275861)（ISSTA'24 配套数据集，
**事件模板为人工标注**，不是任何工具的输出）上实测 5 个真实系统日志，
与 Drain3（ICWS'17 论文，821★）同场对比：

| 数据集 | 原始 | 本项目输出 | Drain3 输出 | 本项目保留率 | Drain3 保留率 |
|---|---:|---:|---:|---:|---:|
| Proxifier | 624K tok | 743 | 317 | **100%** | 72.7% |
| Apache | 1.2M tok | 548 | 431 | **100%** | 93.1% |
| Zookeeper | 2.6M tok | **470** | 980 | **92.1%** | 76.4% |
| HealthApp | 5.1M tok | **499** | 566,802 | **87.8%** | 83.3% |
| OpenStack | 15.3M tok | **746** | 73,569 | **50.0%** | 41.7% |

**证据保留率 5 个数据集全部领先**（保留率 = 输出中仍可识别的标注事件类型占比）。

> 表中「本项目输出」是**首轮跑分**（旧版代码与旧估算器口径）。输出规模的
> **当前复测值**与统一口径见 [`docs/BENCHMARK.md`](docs/BENCHMARK.md)「结果二」
> （Zookeeper 517 / Apache 564 / Hadoop 708 tokens，结论形状不变）。

**两个差异，以及各自的口径**：

- **输出规模有上界**（口径：`summary`，投喂 LLM 用）。与日志行数、事件类型数
  无关 —— 当前代码复测：日志从 12 万行涨到 18 万行（原始 tokens 6.4 倍），
  summary 仍在数百 token（313–788）；Drain3 从 317 涨到 566,802（跨度 1789 倍）。
  HealthApp 上它压缩比只有 9x、耗时 578 秒 —— 一个 56 万 token 的"压缩结果"等于没压缩。
- **但人读的完整报告（md，CLI 默认格式）不是恒定的**：复测 4.8 万–9.7 万 tokens，
  压缩 28x–85x，取决于簇数与实例展开。两种口径不能混用 —— 拿 md 去宣称
  「几百 token」是夸大。详见 [`docs/BENCHMARK.md`](docs/BENCHMARK.md)。

**这个领先不是算法更强，是补了两个真实日志格式的解析洞。** 首轮实测只有
3 胜 2 负；逐条追查后补上 ZooKeeper 的 `<ts> - LEVEL [模块] -` 与
HealthApp 的 `YYYYMMDD-HH:MM:SS:mmm|模块|ID|内容` 两种格式。在解析正常的日志上
领先，靠的是每簇保留一行原始样例作为可引用证据，不是分得更准。

### 置信档位实测：零因果链时零确定性断言

「敢不敢说我不知道」这句话需要数据支撑。评测方式：**完全独立于本项目**，
直接扫原始日志的 `Caused by:` 链构造地面真相，再与工具输出对账。

| 数据集 | 行数 | 真实因果链 | 工具判 CONFIRMED | 自信错误率 |
|---|---:|---:|---:|---:|
| Zookeeper | 74,273 | 0 | **0** | **0%** |
| 其余 4 个 | 455,324 | 0 | **0** | **0%** |

**这个评测第一跑就抓到一个真 bug**：原实现把「时间上紧邻在前的另一个错误
簇」当成因果因，然后标成 CONFIRMED —— 一条 `INFO request started` 因此
被判成确定性根因，正是这个功能声称要消灭的「自信的错误答案」。已修复为
只认条目内 `Caused by:` 链，并把根因指向链上最内层的异常。

**召回率（真出现因果链时能否找到）**：复现语料（自建故障场景 + 真实 Java
异常/栈帧/日志格式，**非生产日志**）上已可复跑 —— 5 条因果链，精确率 100%、
召回率 100%、自信错误率 0%，无链对照组 0 CONFIRMED：

```console
$ python scripts/eval_rootcause.py --repro
  合计：真实因果链 5，CONFIRMED 5
    精确率 100.0%   自信错误率 0.0%   召回率 100.0%
```

**真实数据集也已补上一份**（网络恢复后测）：Loghub Hadoop 全量（61 个含
真实异常栈的文件，5,921 条真实 `Caused by:` 链）—— 精确率 100.0%、
自信错误率 0.0%、**严格口径召回率 91.4%**（5,410/5,921）：

```console
$ python scripts/eval_rootcause.py
  合计：真实因果链 5921，CONFIRMED 112
    精确率 100.0%   自信错误率 0.0%
    召回率·严格 91.4% (5410/5921)
    召回率·宽松 99.9% (5917/5921)
    两口径相差 8.6 个百分点 —— 对外只认严格口径
```

**两个召回口径必须说清差别**：宽松口径只要求"某个 CONFIRMED 簇的行区间盖住
这条链"，严格口径还要求**该簇自身携带的证据文本点名了这条链的根因异常类名**。
差 8.6 个点，是因为某文件 2,643 条链工具只给了 12 个 CONFIRMED 断言，
12 个簇的行区间铺满全文件，宽松口径于是全数"认领"——**工具做了 38 次断言，
宽松指标记了 5,836 次成功**。只报宽松口径会把指标变成近乎恒真的谓词。

另有一条样本限制：5,838 / 5,921 条链来自 5 个巨型文件（单个最大 99,014 行），
分母被超大文件主导。只测了 Hadoop 一个数据集，Android_v1 等其它含栈数据集
已下载尚未跑数。复现语料与真实数据分开列，不混。详见
[`docs/BENCHMARK.md`](docs/BENCHMARK.md)。

复现：`python scripts/benchmark_loghub.py` ·
`python scripts/eval_rootcause.py <日志路径>` ·
`python scripts/eval_rootcause.py --repro` ·
完整方法学、对照组公平性说明与「本文不能证明什么」见 [`docs/BENCHMARK.md`](docs/BENCHMARK.md)

---


---

## 2. 它到底做什么

一句话：**把一个日志文件，变成一份带行号引用、压缩到几百 token 的证据包。**

| 场景 | 痛点 | 解法 |
| --- | --- | --- |
| **日志压缩投喂大模型** | 几十 MB 的日志远超 LLM 上下文窗口，直接粘贴要么截断要么爆 token | 聚类去重 + Top N + 典型样例。**实测 10 万行 / 7MB → 150 tokens** |
| **快速故障排查** | 上百万行人工翻找，同类错误刷屏干扰判断 | 根因排序 + 异常检测 + 堆栈降噪，直接指出「先查哪里」 |
| **给 AI Agent 当后端** | Agent 查日志只能 `grep` + 硬塞上下文 | 内置 MCP Server，Claude Code / Codex / mavis 直接调用 |
| **判定「这个问题能不能定论」** | 所有工具都硬给答案 | 证据充分性评估：**敢说我不知道**，并说清缺什么 |

### 为什么是「本地」

主流可观测平台（阿里云 SLS、Datadog、Dynatrace…）必须把日志**上传到云上**，
按 GB 或按主机计费，还要求先装 Agent 埋点。

这不是懒于做云版本，而是有具体理由：**2026 年 7 月 Hugging Face 遭攻击事件中，
他们的取证分析一开始尝试调用商业 LLM API，被安全策略拦截了** ——
护栏无法判断提交者是攻击者还是应急响应人员，最终只能自托管模型完成分析，
并因此保住了「凭据从未离开环境」。

涉密/内网日志根本不能上云，这是结构性的需求，不会被技术进步消除。

代价是它不做「持续监控」：它是取证工具，不是 APM 平台。这是有意的取舍。

---

## 3. 快速开始

### 安装

```bash
# 本地 Web 界面（推荐：自动开浏览器，端口 8765 起，被占用自动顺延）
pip install "log-ai-compressor[web]"
log-ai-compressor web
```

> 只在命令行里压缩日志、不需要界面：`pip install log-ai-compressor`（硬依赖只有 PyYAML）

### Windows：双击运行

从源码运行时，项目根目录的 **`start.bat`** 会自动定位 Python、首次运行自动装依赖、
启动本地服务并打开浏览器。

### 命令行

```bash
log-ai-compressor web               # 本地 Web 界面
log-ai-compressor web --port 9000   # 指定端口
log-ai-compressor web --no-browser  # 不自动开浏览器

log-ai-compressor run app.log --top 20 -o report.md   # 分析并导出 Markdown

# 复核一份报告的结论出处（推荐：报告发出去后日志可能已被改动或轮转清理）
log-ai-compressor verify --report report.json
log-ai-compressor verify --report report.json --json    # 机器可读
log-ai-compressor verify app.log                        # 当场自检
```

### 从源码运行（开发者）

```bash
git clone https://github.com/hu-chenyu/log-ai-compressor.git
cd log-ai-compressor
pip install -r requirements-dev.txt
python -m pytest                        # 891 个测试
python scripts/adversarial_eval.py      # 对抗性评测评分卡（CI 门禁之一）
python scripts/benchmark_loghub.py      # 复现上面的实测表（需先下载数据集）
```

### 让 AI Agent 直接调用本机日志（MCP / Agent Skills）

```bash
pip install "log-ai-compressor[mcp]"
log-ai-compressor mcp                  # 打印现成的客户端配置
```

已收录于官方 MCP Registry（`registry.modelcontextprotocol.io`），MCP 客户端可直接发现并安装：

```
mcp-name: io.github.hu-chenyu/log-ai-compressor
```

提供 8 个只读工具：`analyze_log_file`、`analyze_log_text`、`compare_log_files`、
`export_report`、`get_cluster_detail`、`verify_evidence`、`list_rules`、`check_environment`。
全部在本机执行，**日志不出网**。

`verify_evidence` 是取证层的关键工具：它重读原始日志逐条核对结论出处，
返回 `VERIFIED` / `TAMPERED`。**Agent 不需要相信这个工具**，可以自己复核。

同时提供 [`SKILL.md`](SKILL.md)（Agent Skills 开放标准），任意支持该标准的
agent 可一条命令安装：

```bash
npx skills add <github>/log-forensics
```

---

## 4. 核心特性

**分析引擎（全部本地算法，零出网）**

- 双输入模式：文件导入（超大文件、编码自动适配 UTF-8/GBK/GB2312/UTF-16/UTF-32，`.gz`/`.zip` 透明解压）+ 文本粘贴
- **结构化日志原生解析**：JSON Lines 与 logfmt **开箱即用，零配置零新依赖**。
  2026 年主流框架的默认输出已经是 JSON（Spring Boot 4.x 原生支持 ECS / Logstash /
  GELF，内置 OpenTelemetry starter），拿正则去刮纯文本的工具在真实生产日志面前是残废的。
  级别、时间、服务、`trace_id` / `request_id` 全部按**字段**认领，
  兼容 OTel `log.level` 点号命名空间、ECS `severity_text`、真嵌套 `{"service":{"name":…}}`
- **调用链重建**：结构化日志里存在关联键时，按 `trace_id` / `request_id` 还原
  跨服务链路与首个失败点 —— 不需要装 agent、不需要接拓扑、不需要第二模态。
  这让工具能**把自己列出的证据缺口真的补上**（见下）
- 通用文本日志解析：时间戳 / 级别 / 模块 / 内容 / 堆栈（Java、Python、C/C++、gdb 帧全兼容）
- 模糊指纹聚类去重：行号、参数、十六进制 ID、路径差异全部抹平，同类错误只留一份典型样例 + 前后上下文
- 三档相似度：严格 ≥0.95（簇更准）/ 标准 ≥0.85（默认）/ 宽松 ≥0.70（簇更少更狠）
- **证据引用**：每条结论绑定 `evidence_id` + 文件 + 行号区间 + 原文 sha256 摘要，
  可用 `verify --report` 重读原文逐条核对；日志被修改或轮转清理会报 `TAMPERED`
- **确定性指纹**：同一份日志两次分析，结论部分逐字节相同
- 智能辅助分析：
  - **证据充分性评估（v2 核心）**：根因置信三档 `CONFIRMED` / `LIKELY` /
    `INSUFFICIENT`。只有 Caused-by 因果链直连才算 CONFIRMED；关键词投票、
    时间连锁这类统计线索只到 LIKELY。证据不足时**不输出「根因」措辞**，
    改为列出「还缺什么才能定论」—— 见第 1 节
  - **缺口自闭环**：缺口清单由日志里**实际有什么**驱动。日志带 `trace_id` →
    调用链重建成功 → 「缺少调用链关联」这条缺口自动关闭；一条都没有 →
    如实申报并说清补什么格式就能补上。只加不减的缺口清单同样是不可信的
  - 错误因果关联（Caused-by 链 / 时间连锁 / 根因关键词）自动区分根因与连锁衍生
  - 统计异常检测（中位数 + MAD 稳健基线）：集中爆发 / 周期发作 / 新型错误 / 罕见异常
  - 优先级综合评分（级别 35% + 频次 25% + 根因 20% + 异常 10% + 持续 5% + 新生 5%），按级别分档钳制
  - 堆栈降噪：折叠 `java.base` / `site-packages` / `node_modules` 等系统库与第三方帧，高亮业务栈帧
- 多文件对比：2~3 个文件的新增 / 消失 / 共同错误与数量变化率，适配版本对比与修复验证
- 可插拔解析规则引擎：YAML 声明规则，改配置不改代码即可接入新格式；内置 generic / embedded / jenkins 三套模板
- 脱敏：内置规则（邮箱/手机号/身份证/密钥）+ 自定义正则，导出与复制时自动生效
- 纯流式逐行处理：内存占用只与错误种类数相关，与日志总行数无关

**Web 界面（v2）**

- 零构建、零 CDN：纯 HTML/CSS/JS，直接改完刷新就生效，完全离线可用
- 三 Tab：文件导入 / 文本粘贴 / 多文件对比
- 内置文件浏览器：服务与日志同机，直接挑本机文件，无需上传（7MB 日志也不走网络）
- 实时 SSE 进度 + 可中途取消
- 错误簇列表（级别/次数/优先级/根因/异常标记）+ 详情面板（典型样例 / 前后上下文 / 降噪堆栈 / 变量分布 / 全部实例）
- 三张图表：错误时间分布（爆发段标红）/ 级别构成 / 模块分布 Top 10
- 实时过滤：支持 `and` / `or` / `not` 布尔表达式（例：`redis and not debug`）
- 一键导出：Markdown（投喂大模型）/ JSON / JSON 全文 / 纯文本 / HTML / 精简摘要
- 亮色 / 暗色双主题

**AI 解读（可选，不配置也完全可用）**

在压缩结果之上再生成一段人话解读：「一句话结论 / 根因链 / 先查哪里 / 证据不足的部分」。

- 三家通吃的 provider 接入：DeepSeek / 阿里百炼 Qwen / 智谱 GLM / Kimi / OpenAI / 本地 Ollama / 任意 OpenAI 兼容端点
- **不配 API Key 也能用**：聚类、根因判定、异常检测、导出全是本地算法，一分钱不花。AI 只是额外加一层
- **只上传压缩后的证据摘要**，不上传原始日志全文
- 提示词显式约束「只依据给定证据、不得编造」，避免模型编造不存在的根因

```bash
log-ai-compressor ai status                      # 看当前配置
log-ai-compressor ai config --provider deepseek  # 选服务商
log-ai-compressor ai config --provider deepseek --key sk-xxx
log-ai-compressor ai test                        # 测连通性
log-ai-compressor ai explain app.log             # 生成解读
log-ai-compressor ai explain app.log --cluster 3 # 只解读某个错误簇
```

也可以在 Web 界面右上角「⚙ AI 设置」里点选配置。

**MCP 接入（给 AI Agent 用）**

2026 年可观测平台几乎都出了 MCP Server（阿里云 SLS、Datadog、Grafana…），MCP 把平台从「人去看的目的地」变成「Agent 可调用的数据源」。**但那些平台的数据都在别人的机房里；本工具的数据就在你本机。**

8 个只读工具（7 个分析 + 1 个取证）：`analyze_log_file` / `analyze_log_text` /
`compare_log_files` / `export_report` / `get_cluster_detail` / **`verify_evidence`** /
`list_rules` / `check_environment`

```bash
log-ai-compressor mcp --install claude-code   # 打印配置片段
log-ai-compressor mcp --install codex
log-ai-compressor mcp --install mavis         # JSON 配置
log-ai-compressor mcp                         # 直接以 stdio 启动
```

接入后可以直接对 Agent 说：

> 分析一下 `C:\logs\app.log`，哪些错误是这次故障的根因？

Agent 会调用本工具做聚类、根因排序，并直接引用压缩后的证据摘要 —— 不需要把几十万行日志塞进上下文。

**全部工具只读**：没有删除、修改、上传、联网的接口。

---

## 5. CLI 使用

```bash
# 分析日志并导出 Markdown 报告（默认级别 ERROR,FAIL）
log-ai-compressor run examples/sample_system.log --top 20 -o report.md

# 指定级别、关键字、规则模板
log-ai-compressor run test.log --level ERROR,FAIL,WARN \
    --include "timeout,refused" --rule embedded --top 30 -o report.md

# JSON 格式
log-ai-compressor run test.log --format json -o report.json

# 多文件对比（第一个为基准）
log-ai-compressor compare examples/app_v1.log examples/app_v2.log -o diff.md

# 查看内置解析规则
log-ai-compressor rules list
```

| 子命令 | 用途 |
| --- | --- |
| `web` | 启动本地 Web 界面（主入口） |
| `mcp` | 启动 MCP Server / 打印客户端配置 |
| `ai` | AI 解读的 status / config / test / explain |
| `run` | 分析单个日志文件 |
| `compare` | 多文件对比 |
| `verify` | **复核结论出处**：核对报告里的每条结论是否仍与原始日志一致 |
| `rules` | 查看解析规则模板 |

---

## 6. 实测数据

### 6.1 真实日志上的压缩与证据保留（Loghub 2.0）

数据来自 [Loghub-2.0](https://zenodo.org/record/8275861)（ISSTA'24 配套数据集），
其 event template 为**人工标注**。对照组为 Drain3（ICWS'17 论文，821★）、
`tail -N`、`grep`。完整方法学、公平性说明与**本文不能证明什么**，
见 [`docs/BENCHMARK.md`](docs/BENCHMARK.md)。

| 数据集 | 原始 | 本项目输出 | Drain3 输出 | 本项目保留率 | Drain3 保留率 |
|---|---:|---:|---:|---:|---:|
| Proxifier | 624K tok | 743 | 317 | **100%** | 72.7% |
| Apache | 1.2M tok | 548 | 431 | **100%** | 93.1% |
| Zookeeper | 2.6M tok | **470** | 980 | **92.1%** | 76.4% |
| HealthApp | 5.1M tok | **499** | 566,802 | **87.8%** | 83.3% |
| OpenStack | 15.3M tok | **746** | 73,569 | **50.0%** | 41.7% |

**证据保留率 5 个数据集全部领先。** 但这个领先是补出来的，不是算法更强 ——
首轮实测是 3 胜 2 负，逐条追查后补了两个解析格式的洞（ZooKeeper 的
`<ts> - LEVEL [模块] -`、HealthApp 的 `YYYYMMDD-HH:MM:SS:mmm|模块|ID|内容`）。
在解析正常的日志上领先，是因为每簇保留了原始样例行作为可引用证据。详见
[`docs/BENCHMARK.md`](docs/BENCHMARK.md)。

**另一个差异在输出规模**（口径：`summary`）：输出**有上界、与日志规模无关**
—— 当前代码复测 313–788 tokens（上表为首轮旧口径跑分，绝对值已随版本
漂移，形状结论不变）；Drain3 从 317 涨到 566,802（跨度 1789 倍）。
HealthApp 上 Drain3 压缩比只有 **9x**、耗时 **578 秒**，等于没压缩。

> 本节**没有验证根因判定准确率** —— Loghub 标注的是日志模板，不是故障根因。

### 6.2 工程指标

| 指标 | 实测值 |
| --- | --- |
| 生产代码 | 8,906 行（28 个模块） |
| 测试 | **891 用例**，覆盖率 **94%**，`ruff` 全绿 |
| CI | Ubuntu 3.11 / Ubuntu 3.12 / Windows 3.12 三矩阵全绿，**五道门禁**：ruff / 单测+覆盖率 / 对抗性评分卡 / 确定性指纹 / 自证脚本 |
| 内存 | 与日志总行数无关，只与错误种类数相关 |
| 硬依赖 | 仅 PyYAML（web / mcp / ai 全部为可选 extra） |

> 覆盖率不是目标，只是底线（CI 卡 90%）。真正卡回归的是那套对抗性评分卡 ——
> 覆盖率衡量「代码被执行过」，它衡量「该出错的时候会不会出错」。

### 6.3 速度

合成日志，仅供吞吐参考（**不代表压缩效果**，真实效果见 6.1）：
~25 万行/秒，10 万行 / 7.03MB 用时 **0.40 秒**。复现：`python scripts/benchmark.py`

---

## 7. 技术架构

```
log_ai_compressor/
├── service.py               # 共享服务层：参数校验 + JSON 序列化 + 导出门面
├── rules/                   # 可插拔解析规则引擎（YAML 驱动）
│   ├── engine.py            #   规则加载/编译/占位符展开
│   └── presets/             #   generic / embedded / jenkins 三套模板
├── core/                    # 核心处理层（零 UI / 零 Web 依赖）
│   ├── models.py            #   数据模型 + 自适应时间直方图
│   ├── encoding.py          #   编码探测（BOM/严格解码验证/截断容忍）
│   ├── structured.py        #   结构化日志原生解析（JSON Lines / logfmt）
│   ├── parser.py            #   增量解析器（多行聚合：折行/堆栈/Caused-by）
│   ├── filters.py           #   级别 + 关键词准入过滤
│   ├── clustering.py        #   模糊指纹聚类（三级匹配）
│   ├── analysis.py          #   根因判定/异常检测/优先级/堆栈降噪/证据评估
│   ├── traces.py            #   调用链重建（trace_id / request_id 跨服务关联）
│   ├── evidence.py          #   证据引用 + 自校验 + 确定性指纹（取证层）
│   ├── pipeline.py          #   流式管线（进度/取消/上下文捕获）
│   ├── comparator.py        #   多文件对比
│   └── redact.py            #   脱敏
├── export/reporters.py      # 导出层（Markdown/JSON/文本/HTML/摘要）
├── web/                     # v2 Web 接入层
│   ├── server.py            #   FastAPI：REST + SSE + 文件浏览
│   ├── jobs.py              #   后台任务 + 进度队列 + SSE 帧
│   └── static/              #   零构建前端（index.html / app.js / style.css）
├── mcp/server.py            # MCP 接入层（8 个只读工具）
├── ai/                      # 可选 AI 解读层
│   ├── config.py            #   服务商配置（三层优先级 + Key 不回显）
│   ├── client.py            #   OpenAI 兼容 / Ollama 原生协议
│   └── prompts.py           #   提示词（显式约束防幻觉）
└── cli.py                   # 命令行入口
```

**分层解耦**：`rules → core → export → service → web / mcp / cli` 单向依赖。

- `core` 零 UI 依赖，可独立测试、被脚本直接复用：
  `from log_ai_compressor.core.pipeline import analyze_file`
- `core/evidence.py` **不 import 任何上层模块** —— 证据是比模型更底层的概念，
  模型层反过来引用它。这让取证层可以被单独审计。
- `service` 是 core 与接入层之间**唯一**的转换点，Web 和 MCP 共用同一份序列化逻辑，不会各写一份、各写错一份
- 前端零构建：不引入 node/webpack，改完刷新即生效，也不需要 npm

### 核心算法

1. **模糊指纹聚类（两级性能保护）**
   - 指纹 = 级别 + 掩码消息（数字→N、十六进制→H、UUID→U、路径→P、引号串→S）+ 堆栈前 3 行特征
   - 匹配路径：完整指纹精确命中（O(1)）→ (级别, 消息模板) 精确命中 → 同级别桶内编辑距离相似度（上限 256 次比较）
   - 变体命中后回写精确表，后续重复变体继续 O(1)

2. **内存控制**
   - 逐行流式读取，簇内只存「模板 + 计数 + 一份样例 + 有界直方图」
   - 时间直方图桶数上限固定（簇 96 / 全局 512），超限自动 8 倍扩宽桶宽合并旧桶

3. **根因判定（三路证据融合）**：Caused-by 链回溯 + 60 秒窗口内首发且含根因关键词 + 强关键词命中；含 retry/after/downstream 等被动词的簇标记为连锁衍生

---

## 8. 开发与测试

```bash
pip install -r requirements-dev.txt

ruff check log_ai_compressor tests scripts     # 代码规范
python -m pytest                                # 全量测试
python -m pytest --cov=log_ai_compressor --cov-report=term-missing
```

测试分层：

| 文件 | 覆盖 | 速度 |
| --- | --- | --- |
| `test_service.py` | 参数归一化、序列化、导出门面 | < 1s |
| `test_web.py` | REST 契约、SSE 帧格式、参数拦截、文件浏览边界 | < 2s |
| `test_mcp.py` | 工具注册、只读标注、各工具行为与错误路径 | < 1s |
| `test_ai.py` | 配置优先级、提示词、客户端（mock 网络）、可选性 | < 1s |
| `test_forensics.py` | 证据引用确定性、篡改/删除检测、指纹稳定性、CLI/MCP 入口 | < 1s |
| `test_structured_logs.py` | JSON/logfmt 字段别名、命名空间、误判防护、调用链与越界防线 | < 1s |
| `test_correctness_fixes.py` | 18 个已复现缺陷的回归测试（每条断言写成"修好后必须成立"） | < 1s |
| `test_sdist_contents.py` | 交付物完整性：README 引用的每个文件都得在 sdist 里 | < 1s |
| `test_packaging.py` | 可选依赖契约、轻装可跑、降级不炸 | ~2s |
| `test_*.py`（core） | 解析 / 聚类 / 分析 / 导出 / 编码 / 对比 | ~2s |

另有一个**不在 pytest 里**的门禁：`scripts/adversarial_eval.py`。它测的不是
"函数返回什么"，而是"这个工具在该闭嘴的时候会不会闭嘴" —— 这类问题写不成
单元测试，只能用场景 + 评分表来表达。

CI（GitHub Actions）三矩阵：`ubuntu-3.11`（`requires-python` 下限）、
`ubuntu-3.12`（最常用版本）、`windows-3.12`（行尾 / shell / 路径处理与
unix 不同，且 `start.bat` 是 Windows 一等入口）。**五道门禁**：ruff →
pytest + 覆盖率 ≥90% → 对抗性评分卡 → 确定性指纹 → 自证脚本。

### 启动脚本的硬约束

`start.bat` **必须是纯 ASCII + CRLF 行尾**，测试会强制校验。原因见 `tests/test_launcher.py` 顶部注释：cmd.exe 逐字节按控制台代码页解析批处理文件，用裸 LF 或含非 ASCII 字节都会导致解析错位、每行开头被吞，程序永远起不来。所有中文提示都放在 Web 界面里，不放 bat。

---

## 9. 它做不到什么

这一节比上面所有功能列表加起来都重要。看不下去可以不看了，但你会高估它。

| 它做不到 | 为什么 | 谁行 |
| --- | --- | --- |
| **比云平台更高的根因准确率** | 多模态根因需要指标 + 链路 + 拓扑。RCA100 数据集的结论是 **62.5% 的案例至少需要两种模态，31% 需要三种**。本工具只有日志一种 | Datadog / SLS / Dynatrace |
| 持续监控与告警 | 没有 agent 埋点、没有时序库、没有告警队列。**这是有意的取舍** | 全部 APM 平台 |
| 调用链追踪 | `traces` 是从日志里的 `trace_id` **重建**出来的，不是采集的。没有采样就没有精确耗时、没有 span 树 | Jaeger / Tempo |
| 「召回率」实测 | 真实数据集（Loghub Hadoop 全量）已测：精确率 100%、自信错误率 0%、**严格口径召回率 91.4%**。但**只测了 Hadoop 一个数据集**，且 5,838/5,921 条链来自 5 个巨型文件，分母被超大文件主导；Android_v1 等含栈数据集已下载未跑数。未必可外推 | — |
| 隐私合规认证 | 它不处理合规，只保证「数据不出本机」 | — |

还有一条是方法论上的硬约束，写在代码里而不只是文档里：

> **调用链只证明顺序，不证明因果。** 有完整 trace 但没有 `Caused by:` 时，
> 工具仍然只给 `LIKELY`、`can_conclude=false`。
> `CONFIRMED` 的唯一来源始终是条目内 Caused-by 链。这条线一松，
> 「拿不出因果链就说不知道」的立场就作废了。
> 对抗评测里有一个场景专门盯它（`structured_trace_not_causal`）。

---

## 10. 自定义解析规则

新建 `my_format.yaml`：

```yaml
name: my_format
description: 自研日志格式
patterns:
  - name: main
    # {LEVEL} 为引擎占位符，自动展开为标准级别令牌
    regex: '^<(?P<timestamp>\d+)>\s*\[(?P<module>\w+)\]\s*(?P<level>{LEVEL})\s*(?P<message>.*)$'

stack_indicators:
  - '^\s*at\s+[\w$.]+\('

level_hints:            # 无级别字段的行按关键词推断（可选）
  ERROR: ['\bERROR\b', '\berror\b']
```

使用：`log-ai-compressor run app.log --rule my_format.yaml`

---

## 11. License

MIT
