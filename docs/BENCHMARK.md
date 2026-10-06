# Loghub 2.0 公开基准：压缩比 vs 证据保留率

> 本文所有数字来自 `scripts/benchmark_loghub.py` 在 Loghub-2.0 真实日志上的实测，
> 可复现。**包括对我们不利的部分。**

## 要回答的问题

给定一份真实系统日志，在**同样的 token 预算**下，谁留住的「事件类型」最多？

行业里都在比**压缩比**（谁压得更小）。但压缩比是个会骗人的指标：
一个工具完全可以输出 50 万 token 的"压缩结果"并宣称压缩了 9 倍 ——
而 50 万 token 对 LLM 上下文来说毫无意义。所以这里同时报两个指标：

- **压缩比** = 原始日志 tokens / 输出 tokens
- **证据保留率** = 输出中仍可识别的人工标注事件类型数 / 事件类型总数

## 数据来源

- **数据集**：Loghub-2.0（[zenodo.org/record/8275861](https://zenodo.org/record/8275861)），
  ISSTA'24 "A Large-scale Evaluation for Log Parsing Techniques" 配套数据。
- **标注依据**：该数据集的 event template 是**人工标注**的（论文就是拿它当
  ground truth 评估 15 个解析器），不是任何工具的输出。所以"证据保留率"
  有真实标注依据。
- **本文覆盖**：5 / 14 个数据集，按体积从小到大选取（212,394 行 / 25.3 MB 为上限）。

| 数据集 | 行数 | 原始 tokens | 标注事件类型 |
|---|---:|---:|---:|
| Proxifier | 21,320 | 624,518 | 11 |
| Apache | 51,978 | 1,230,926 | 29 |
| Zookeeper | 74,273 | 2,564,008 | 89 |
| HealthApp | 212,394 | 5,067,779 | 156 |
| OpenStack | 207,632 | 15,299,983 | 48 |

## 对照组

| 方法 | 说明 |
|---|---|
| `tail -N` | 最朴素：日志太大，只看最后 N 行 |
| `grep` | 关键词过滤 `error\|exception\|fail\|fatal\|...` |
| **Drain3** | 学术标准日志模板抽取（ICWS'17，821★）。喂给它 Loghub 自己标注的 `Content` 字段（已剥离时间戳/主机/级别），这是 Drain 官方文档明确要求的输入形态 |
| **log-ai-compressor** | 本项目。跑两种模式：默认级别（ERROR/FAIL 排障）与全级别（摘要） |

**公平性说明**：第一版基准把带时间戳的原始行直接喂给 Drain3，导致它只拿到
63.6%（Proxifier）；按其文档要求改喂 `Content` 字段后升到 72.7%。
**这个修正让对照组变强，最终表格用的是修正后的数字。**

## 结果一：证据保留率

| 数据集 | 标注事件 | 本项目(默认) | **本项目(全级别)** | Drain3 | tail @32k | grep @32k |
|---|---:|---:|---:|---:|---:|---:|
| Proxifier | 11 | 45.5% | **100.0%** | 72.7% | 63.6% | 45.5% |
| Apache | 29 | 55.2% | **100.0%** | 93.1% | 27.6% | 34.5% |
| Zookeeper | 89 | 2.2% | **92.1%** | 76.4% | 79.8% | 9.0% |
| HealthApp | 156 | 1.9% | **87.8%** | 83.3% | 32.1% | 3.8% |
| OpenStack | 48 | 10.4% | **50.0%** | 41.7% | 20.8% | 8.3% |

**5 个数据集全部领先 Drain3。**

### 这两个数字是怎么来的（不是算法变强了）

第一轮实测我们是 **3 胜 2 负**（Zookeeper 71.9%、HealthApp 78.8%）。用
`scripts/diagnose_missed_events.py` 逐条追查丢失的事件后发现：

> **丢的不是「聚类合并」，是「根本没解析出来」。**

ZooKeeper 用的是 `<时间戳> - <级别> [<模块>] - <内容>`，但 `generic` 规则集里
`iso_level_module` 的级别前只允许 `[` 或 `(`，不接受 `-`，于是整份日志掉进无结构
兜底：message 里保留完整时间戳、module 为空、指纹噪声大、不同事件被并簇。

HealthApp 用的是 `20171223-22:15:29:606|模块|用户ID|内容`，**非 ISO 紧凑时间戳 +
管道分隔**，同样无模式匹配 —— 时间戳解析为 `None`，**整份日志的时间直方图是空的**。

补了两条规则后（`iso_dash_level_module` / `pipe_delimited_device`）：

| 数据集 | 修复前 | 修复后 | 增量 |
|---|---:|---:|---:|
| Zookeeper | 71.9% | 92.1% | **+20.2** |
| HealthApp | 78.8% | 87.8% | **+9.0** |

**结论要老实说：这不是算法进步，是补格式覆盖的洞。** 聚类本身仍是
`difflib` 编辑距离 + 级别桶回退，没有 Drain 的前缀树和模板在线精修。
在**解析正常**的日志上（Apache / ProxIFIER 那种标准格式），我们领先的原因是
保留了每簇的原始样例行，而 Drain 只吐模板 —— 不是因为我们分得更准。

另外注意 **默认级别模式下 Zookeeper 从 11.2% 降到 2.2%**：这是**变准了**而非变差。
修复前那些行靠「消息里含 error/fail 关键词」被误捞进错误集，实际上它们是
`INFO` 级的启动日志。修复后级别被正确识别为 INFO，于是被正确排除。

## 结果二：输出规模

### 必须先说清楚：上面那张表测的是 `brief_summary`

benchmark 跑的是 `brief_summary`（投喂 LLM 的精简摘要），**不是 CLI 的默认格式**。
两种格式的体积差一个数量级，混为一谈会得出错误结论。实测各格式 tokens：

| 数据集 | 原始 | brief_summary | **md（CLI 默认）** | text | json |
|---|---:|---:|---:|---:|---:|
| Proxifier | 629,847 | 710 | **42,718** | 44,414 | 5,150 |
| Apache | 1,243,920 | 562 | **47,498** | 49,946 | 41,633 |
| Zookeeper | 2,582,576 | 266 | **8,686** | 8,738 | 2,992 |
| HealthApp | 5,120,877 | 453 | **14,533** | 15,021 | 4,374 |
| OpenStack | 15,351,891 | 512 | **39,515** | 39,938 | 8,718 |

- `brief_summary`：**266–710 tokens**，跨度 1.06 倍，与日志规模无关。
- `md` / `text`（人读的完整报告）：**8,686–47,498 tokens**，跨度 5.5 倍，
  取决于有多少簇、多少实例需要展开。它仍然远小于原文（压缩 15x–388x），
  但**不是恒定**，也不该拿它去对标「几百 token」。

**这里也修掉了一个曾被写进 README 的错误表述**：早期版本说「输出恒定在
470–746 tokens」，那只对 `brief_summary` 成立。对着 md 宣传是夸大。

### 修复记录：实例行号曾把报告撑到 27 万字符

修复前，OpenStack 上 3 个簇的 md 报告是 **519 行 / 272,744 字符（≈68k tokens）**，
其中**单行 16,828 字符** —— 一个簇的 2000 个实例行号被平铺进一行。

改成摘要式呈现后：同场景 **41,723 字符（-85%）**，最长行 361 字符。
`tests/test_report_size.py` 把体积钉成硬约束。

### 与 Drain3 对比（同为 brief_summary 口径）

| 数据集 | 原始 tokens | 本项目输出 | Drain3 输出 |
|---|---:|---:|---:|
| Proxifier | 624,518 | 743 | 317 |
| Apache | 1,230,926 | 548 | 431 |
| Zookeeper | 2,564,008 | 470 | 980 |
| OpenStack | 15,299,983 | **746** | **73,569** |
| HealthApp | 5,067,779 | **499** | **566,802** |

**本项目输出恒定在 470–746 tokens（跨度 1.59 倍），与日志行数、事件类型数无关。**
Drain3 从 317 到 566,802（跨度 1789 倍）。

在最大的两个数据集上，Drain3 的"压缩结果"已经不可用：

- **HealthApp**：压缩比只有 **9x**，耗时 **578 秒**。一个 56 万 token 的压缩结果
  等于没压缩。
- **OpenStack**：压缩比 208x，输出 73,569 tokens。

原因：Drain3 的输出行数 ≈ 模板数 × 每模板的参数，模板一多参数就爆炸。
本项目的输出行数 ≈ 簇数，**且每簇只存一份样例，与出现次数无关**。

## 结果三：耗时

| 数据集 | 本项目(全级别) | Drain3 |
|---|---:|---:|
| Apache | 2.12s | 0.28s |
| Zookeeper | 4.50s | 0.76s |
| OpenStack | 27.37s | 36.30s |
| HealthApp | **12.00s** | **578.30s** |

小数据集上 Drain3 更快；**HealthApp 上慢 48 倍**，因为它的聚类在该数据集上
退化成了近乎两两比较。

## 结论（诚实版）

**1. 证据保留率 5 个数据集全部领先 Drain3，但这个领先是"补洞"补出来的，不是算法更强。**
真正的算法差距仍然存在：我们的聚类没有前缀树剪枝、没有模板在线精修。
在**解析正常**的日志上领先，是因为我们为每簇保留了一行原始样例作为可引用证据，
而 Drain 只吐模板 —— 样本行天然覆盖更多事件类型，但这不等于「分得更准」。

**2. 我们赢在"输出规模有上界"。** 无论日志是 2 万行还是 21 万行、11 种还是
156 种事件类型，输出都在 470–750 tokens。这直接决定了它能不能塞进 LLM 上下文。

**3. 这仍然是一条工程性质，不是护城河。** 给 Drain3 加一个 `--max-clusters`
十分钟就能做到。**能被十分钟实现的东西不算差异化。**

**4. 输入条件对两组并不完全对称，这是有意为之但必须说明。**
Drain3 拿到的是 Loghub 标注里的 `Content` 字段（时间戳/主机/级别已被剥离），
本项目拿的是**原始日志**，必须自己解析。真实用户就是这样用的（指着日志文件点一下），
所以这是现实世界的公平对照；但它意味着本项目的成绩里包含了「格式覆盖能力」，
而 Drain3 的成绩里不包含。**换句话说：这一轮我们赢在工程完整性，不在算法。**

**5. 默认级别模式在无级别字段的日志上很糟**（HealthApp 1.9%、Zookeeper 2.2%、
OpenStack 10.4%）。这些系统把级别写在非标准位置，工具判不准。这是当前最该修的
已知缺陷 —— 语义上没错（那些行本就不是错误），但对用户是灾难：指着日志点一下，
拿到一份几乎空的报告且没有任何解释。

## 本文不能证明什么

写清楚边界，避免过度解读：

- **本文没有验证根因判定的准确率。** Loghub-2.0 的标注是"日志模板"，
  不是"故障根因"。本文的"证据保留率"衡量的是**事件类型是否还在**，
  完全不涉及本项目的置信三档（CONFIRMED/LIKELY/INSUFFICIENT）。
  **根因判对判错，本文一个字都没证明。**
- **Drain3 的保留率有测量偏袒**：它的输出是模板而非原始行，没有 LineId 可查，
  只能与标注模板做归一化模糊匹配（阈值 0.8）。这一项对 Drain3 略偏宽松，
  但也可能误判。
- **只覆盖 5 / 14 个数据集**，且都是体积较小的。Loghub 还有 Spark / HDFS /
  Thunderbird 等 300MB+ 的数据集未测。
- **单次运行**，无重复实验、无显著性检验。

## 复现

```bash
# 1. 下载数据集（PowerShell，约 13 MB）
$u = "https://zenodo.org/api/records/8275861"
$r = Invoke-RestMethod $u
foreach ($n in "Proxifier","Apache","Zookeeper","HealthApp","OpenStack") {
  Invoke-WebRequest ($r.files | ? { $_.key -eq "$n.zip" }).links.self `
    -OutFile "data/loghub/$n.zip"
  Expand-Archive "data/loghub/$n.zip" "data/loghub/$n"
}

# 2. 装对照工具（可选，不装则跳过 Drain3）
pip install drain3

# 3. 跑
python scripts/benchmark_loghub.py
python scripts/benchmark_loghub.py --datasets Apache --budgets 500,2000,8000
```

指标口径由 `tests/test_benchmark_loghub.py` 钉死（13 个用例），
专门守住两个**改坏了不会报错**的地方：结构化 CSV 的 `LineId` 是 1-based
（整体错位一行只会让数字悄悄变好看），以及朴素基线的 token 预算封顶。

## 引用

```
Loghub-2.0
Zhihan Jiang, Jinyang Liu, Junjie Huang, Yichen Li, Yintong Huo, Jiazhen Gu,
Zhuangbin Chen, Jieming Zhu, Michael R. Lyu.
A Large-scale Evaluation for Log Parsing Techniques: How Far are We?
ISSTA '24.  DOI: 10.1145/3650212.3652123

Drain
Pinjia He, Jieming Zhu, Zibin Zheng, Michael R. Lyu.
Drain: An Online Log Parsing Approach with Fixed Depth Tree. ICWS '17.
```