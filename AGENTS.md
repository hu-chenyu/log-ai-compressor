# AGENTS.md

给 AI agent 与自动化工具的**工作契约**。人类贡献者看 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

这个文件描述三件事：**这个仓库是什么形状的**、**哪些约定是不许绕过的**、
**改之前必须先跑通哪几道门禁**。

---

## 1. 这是什么

`log-ai-compressor`：把一个日志文件变成**可复核的证据包**。

三条产品承诺，其余一切都是实现细节：

| 承诺 | 落到哪 |
| --- | --- |
| 每条结论带原文行号区间 + 那段原文的 sha256，用户可脱离本工具自校验 | `core/evidence.py`、`verify` 入口 |
| 拿不出 `Caused by` 因果链就给 `LIKELY`，并列出**还缺什么**才能定论 | `core/analysis.py`（`assess_evidence`） |
| 同一份日志跑两遍，结论指纹逐字节相同 | `core/evidence.py::result_fingerprint`，CI 第 4 道门禁 |

**不做的事**：不上传日志、不出网、不调外部模型 API 做判断（AI 是可选的
「解读」extra，只读已经算好的证据，不参与判定）。

---

## 2. 四条不可协商

### 2.1 不许造数字

任何对外数字（README / docs / CHANGELOG / PR 描述）必须来自**可复现的脚本输出**，
并写明复现命令。`scripts/prove.py` 会把 README 里的主张逐条重算一遍，对不上就红。

- 禁止把「大概」「应该差不多」写成实测值。
- 禁止只改措辞来掩盖缺陷。发现问题只有两条路：**修**，或**如实降级并写进文档**。
  「把断言改弱让测试变绿」不在选项里。

### 2.2 门禁必须可失效

**一个从未曾变红过的门禁，等于没有门禁。**

每加一条门禁或断言，同步加一条「故意做坏 → 断言必须变红」的测试
（`tests/test_prove_claims_gate.py` 与 `tests/test_gates_and_masking.py` 是范式）。
判据很简单：把被守的那行代码改坏，看这条测试会不会失败。不会，就是装饰。

### 2.3 不许对着基准调参

`docs/BENCHMARK.md` 的数字是对照 Drain3 / LogsLop 的**独立评测**。
不许为了让某个数据集赢而去改阈值、改掩码规则、改 `top_n` 默认值。

调参调出来的领先不是领先。**对着评测集改出来的规则会把评测集本身也改坏**，
所以每次「修好了某数据集」的提交，都必须全量重跑 10 个数据集并给出
「哪些变了、为什么」。

### 2.4 错了就撤回

这个仓库撤回过多次**数字**与**修法**（逐条清单见 [`CHANGELOG.md`](CHANGELOG.md)
里标着「撤回」的条目）。

**降级不是修复，措辞不是补丁。** 修不好就说明修不好。注意区分两类撤回 ——
「数字错了」容易识别，「**指标变好但产品变坏了**」才是容易漏的那一类：
v2.9.8 的字面词元守卫把 HPC 覆盖率从 85.1% 抬到 93.2%，却让报告从 76 个簇
炸成 2,690 个、交付口径从 23.0% 崩到 8.1%。看到覆盖率涨了就宣布胜利，
是本项目最容易犯的错。

撤回的记录**不要删**——历史引用刻意不改写：当时的判断在当时是对的，
把它抹掉等于篡改历史。新版本在 CHANGELOG 里追加「此前发布的 X 数字已撤回，
原因如下」，让读者能看到纠正过程。

---

## 3. 分层与唯一门面

```
core/       解析 / 聚类 / 分析 / 证据 / 编码 / 对比      零第三方依赖
rules/      YAML 规则集加载与匹配                        零第三方依赖
export/     md / html / json / text / summary 导出       零第三方依赖
   ↑ 三层互不越界，且硬依赖只有 PyYAML
service.py  唯一门面：Web / MCP / CLI 共用
   ↑
cli.py  web/  mcp/  ai/     ← 四个接入层**只能经 service** 触达 core/export
```

这条契约由 `scripts/prove.py::claim_layering` 用 AST 核验：静态扫
`log_ai_compressor/**/*.py`，逐个判定每个 import 的层级，越层即红。

> **踩过的坑**：这条门禁最早写成 `for pkg in (pkg/"ai", pkg/"cli", ...)`，
> 而 `cli` 是**文件**不是目录，`rglob` 静默返回空 → 扫了 0 个文件却报
> 「4 个接入层 AST 核验通过」。修好之后它立刻变红，点名 `cli.py` 里 4 处
> 真实越层 import。**一个报「检查通过」但其实什么都没扫的门禁，比没有门禁更糟** ——
> 没有门禁你知道那里没检查。

加新模块时先确认它属于哪一层。新增接入层必须同步更新 `prove.py` 的
`_CLAIM_FNS` 注册表，否则它不在核验范围内。

---

## 4. CI 的六道门禁

改代码前要知道它们各自在守什么 —— **不要通过删断言、跳过测试、放宽阈值让 CI 变绿。**

| # | 门禁 | 守的是什么 |
| --- | --- | --- |
| 1 | `ruff check` | 代码规范 |
| 2 | `python scripts/adversarial_eval.py` | 「该闭嘴的时候会不会闭嘴」。测的不是函数返回什么，而是场景下会不会乱给 `CONFIRMED` |
| 3 | 确定性指纹 | 同一份日志连跑三次，指纹必须逐字节相同 |
| 4 | `python scripts/prove.py` | README 里每个数字与主张，重新算一遍 |
| 5 | 构建 + 可复现性 | 同一份源码能否重建出**逐字节相同**的产物 |
| 6 | `pytest --cov-fail-under=90` | 单测 + 覆盖率地板 |

矩阵是 `ubuntu-3.11` / `ubuntu-3.12` / `windows-3.12` 三条。
`3.11` 是 `pyproject.toml` 的下限（改矩阵必须同步改那边），
`windows` 必须在真机上跑（行尾 / shell / 路径处理与 unix 不同，
且 `start.bat` 是 Windows 一等入口）。

覆盖率对账只在 `ubuntu-3.12` 上做：**覆盖率是平台相关的量**，
README 里写的那个数就是 ubuntu-3.12 的实测值。地板（90%）三矩阵全卡。

### 基准数字有一条独立的对账链

`prove.py` 除了校工程指标，还逐格校 **README 的十行基准表**——
对照 `docs/benchmark_baseline.json`（`benchmark_loghub.py --write-baseline`
生成的实测快照），容差 0.05pp。

这条门禁是**事故驱动的**：v2.9.9 修了 `mask_text` 的两处误伤，改动本身是对的，
但它改变了聚类结果，于是 HPC 76 簇 / 85.1% 变成 79 簇 / 81.1%、Zookeeper
92.1% 变成 91.0%、Proxifier 交付口径 81.8% 变成 72.7% —— **README、
docs/BENCHMARK.md、「唯一负例只差 2 个事件」全部原地不动，而全部门禁绿着。**

所以：**改了任何影响聚类的代码**，哪怕你觉得「只是掩码规则更准确」，
也要重跑全量基准、回填文档、跑 `prove.py`。`--write-baseline` 在只跑子集
或缺对照臂时会**拒绝写文件** —— 残缺基线比没有基线更糟，它会把「这次没测」
固化成「产品就是这样」。

---

## 4b. 确定性的边界

「同一份日志跑两遍指纹相同」这条承诺，守的是**同一输入 → 同一输出**。
它的边界要写清楚，否则会被当成比实际更强的承诺：

| 层 | 状态 |
| --- | --- |
| 同一份日志、同一份代码、跑任意次 | 相同。CI 第 3 道门禁在守 |
| 同一批条目、**换排列** | v2.10.0 前**不相同** —— `_find_similar` 只比级别桶里最近的 256 个簇，早期簇永远比不到。已修，且钉成回归测试 |
| 打乱整个日志文件的行序 | **仍然不同**，且这是**设计如此**，不是缺陷 |

最后一行要说清楚为什么：打乱行序会破坏折行/堆栈的**邻接聚合**，解析出的
条目多重集合本身就不是同一个；「错误首次出现时间」「Caused-by 邻接」
这些结论也天然与行序有关。要让整份报告对行序不变，只能改成批量重聚类，
而本工具是**流式**的（Web 界面边扫边出结果），那条路会推翻整个产品形态。

已知且接受的残余：**贪心在线聚类对到达顺序敏感** —— 一条边界条目并进哪个簇，
取决于当时已经存在哪些簇。这是 Drain3 也有的性质，不是本项目独有的缺陷。

---

## 5. 常用命令

```bash
pip install -r requirements-dev.txt

ruff check log_ai_compressor tests scripts   # 规范
python -m pytest -q                          # 全量测试
python scripts/prove.py                      # 逐条重算 README 的主张
python scripts/adversarial_eval.py           # 对抗性评分卡

# 诊断：有没有任何相似度量能把「该合并」和「不该合并」分开
python scripts/diagnose_merge_decisions.py

# 基准（需要先 python scripts/fetch_datasets.py --all，约 178 MB）
python scripts/benchmark_loghub.py --no-logslop --no-drain3 --budgets 32000
python scripts/benchmark_loghub.py --budgets 32000 --write-baseline   # 顺带刷新基线
```

**全量 10 数据集基准很慢。** 单是 Hadoop 一个数据集，`run_one` 就要对
18 万行做 4 次完整分析（实测单次约 194 秒）。迭代时用 `--datasets HPC`
之类的子集，判胜负用 `--budgets 32000`。

**跑基准时不要并发跑别的东西。** 分析是纯 CPU 密集的，并发跑测试会把
两边的墙钟时间一起拉长好几倍，最后谁都测不准「这次改了多久」。

---

## 6. 改动纪律

### 6.1 提交

[Conventional Commits](https://www.conventionalcommits.org/)。
scope 用真实子系统：`core` / `clustering` / `analysis` / `export` / `ci` / `bench` / `docs`。

subject 写**为什么**，不写「改了什么」——「什么」git 已经知道了：

```
fix(bench): 撤回 v2.9.4 的全部基准数字 —— 指标在数用户没收到的东西
fix(gates): 四处「测量环节静默失能」全部修掉，并钉成回归测试
fix(report): 「变更关联」吃掉 Mac 全篇 33%，且它根本不是变更
```

在 `main` 上不要直接提交，先开分支。

### 6.2 改了对外数字

顺序固定：**改代码 → 重跑全量基准 → 回填文档 → 提交**。
不要先写数字再找证据，也不要只改一两个数据集就发版。

### 6.3 加测试

每条降级分支都要配一条「正常路径没被写坏」的对照。
覆盖率 96% 的形状会骗人 —— 未覆盖的几乎全是「出问题时怎么降级」，
而降级写成「恒返回空」测试照样是绿的。

### 6.4 Windows 与行尾

- `.bat` **必须纯 ASCII + CRLF**（`tests/test_launcher.py` 强制校验）。
  cmd.exe 逐字节按控制台代码页解析批处理，裸 LF 或非 ASCII 字节都会导致解析错位。
- 其余源码与文档按 `.gitattributes` 统一 LF。改完用**二进制读**复核行尾：
  `Path.read_text()` 会走通用换行把 CRLF 归一成 LF，自检会得到一个百分之百确定的假结论。

---

## 7. 给 agent 的额外提醒

- **不要自己 grep 整个日志文件。** 几十万行会吃掉你几乎全部上下文；
  本工具的 MCP 工具 0.4 秒就能给出排好序的清单。
- **改了聚类/掩码就先重跑基准。** 这类改动「看起来只是更准确了」，
  但它会改变所有对外数字，而历史已经证明没人会自动记得重跑
  （v2.9.9 就这样让 README 的基准表烂了一个版本）。
- **三档置信不要混用**：`INSUFFICIENT` 时读 `gaps`（缺什么证据），
  **不要自己猜一个**。一个自信的错误根因比没有答案更糟。
- **相关性不是因果**：`cooccurring` / `related_clusters` / `traces` 永远不能表述成「导致了」。
- **引用结论必须带行号出处**，否则等于让用户无法验证。
- 拿不准某条结论档位时，**弃权（`INSUFFICIENT`）算通过**。
  只算命中率的评测会偏好猜得最自信的系统。

---

## 8. 相关文档

| 文档 | 内容 |
| --- | --- |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 人类贡献流程、分支与发版 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 模块职责与数据流 |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | 开发与测试速查 |
| [`docs/BENCHMARK.md`](docs/BENCHMARK.md) | 评测口径与实测数据 |
| [`docs/POSITIONING.md`](docs/POSITIONING.md) | 能力边界与不适用范围 |
| [`CHANGELOG.md`](CHANGELOG.md) | 每个版本改了什么、撤回过什么 |
| [`SKILL.md`](SKILL.md) | 作为 Agent Skill 的调用说明 |

> 本文**刻意不写会被 CI 对账的数字**（行数、测试数、覆盖率百分比）。
> 那些写在 [`README.md`](README.md) 里，由 `scripts/prove.py` 逐条重算。
> 一份没人复现过的数字等于营销 —— 包括本文档里的。
