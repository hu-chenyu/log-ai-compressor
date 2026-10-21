# 开发与测试

## 开发与测试

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
`ubuntu-3.12`（最常用版本，也是覆盖率与变异记分卡的**参考矩阵**）、
`windows-3.12`（行尾 / shell / 路径处理与 unix 不同，且 `start.bat` 是
Windows 一等入口）。**十道门禁**：ruff → pytest + 覆盖率 ≥90% → 对抗性评分卡
→ 确定性指纹 → 自证脚本 → 构建可复现性 → 变异存活率记分卡 →
SAST → 依赖漏洞扫描 → 性能回归（逐条见 [`AGENTS.md` §4](AGENTS.md)）。
自证脚本内部已扩到 **11 条**主张（v2.8.0 起新增「README 工程指标与仓库一致」，
v2.8.1 再加「README 架构树必须列全包内模块」，v2.9.0 加「接入层只经 service
触达 core/export」，v2.9.1 再把「README 写的条数」本身也纳入对账，
v2.10.0 加「**基准表逐格对账实测基线**」，v2.11.0 从只校 README 扩到**每一份副本**
（README / docs/BENCHMARK.md / docs/FAQ.md），并把「N 胜 N 平 N 负」的场次也纳入重算）。

### 三道新门禁（v2.11.5 起）

它们有共同的纪律：**先证明自己会红**，否则「检查通过」只是占位。

```bash
python scripts/sast_gate.py                       # 第八道
python scripts/dep_audit_gate.py --self-check     # 第九道（首次运行要联网）
python scripts/perf_gate.py                       # 第十道
```

| 门禁 | 本地怎么验它会红 | 产物 |
| --- | --- | --- |
| SAST | `tests/test_sast_gate.py` 四条负向用例；`python scripts/sast_gate.py --print-baseline` 看当前发现 | `docs/sast_baseline.json` |
| 依赖扫描 | `--self-check` 会先喂 `urllib3==1.25.8`（实测 21 条 advisory）；扫不出来返回 **2** | — |
| 性能回归 | `tests/test_perf_gate.py::test_counters_are_not_silently_zero` | `docs/perf_baseline.json` |

**SAST 基线怎么维护**：

```bash
python scripts/sast_gate.py --print-baseline   # 生成骨架（reason 全空，门禁会拒）
```

把它抄进 `docs/sast_baseline.json` 并**逐条写 reason**。门禁会在四种情况下红：
新发现不在基线、数量对不上、**豁免过期**（代码里已不报错而基线里还留着）、
**豁免没写 reason**。能修的一律先修 —— v2.11.5 建基线时修掉了 5 处，
剩下 57 条才是真该留下的。

**性能基线怎么维护**：

```bash
python scripts/perf_gate.py --write-baseline
```

只在**算法或剪枝有意改动**之后刷新。注意它比的是**确定性工作量计数**，
同一份负载在任何 OS / Python 上必须给出同一组整数；墙钟写在
`wall_clock` 里但**明确标注不参与判定** —— 跨机器不可比，噪声只会向下，
做成红线只会收获一堆假红，然后某天被人加 `--continue-on-error` 关掉。

**为什么它们接在三条矩阵上**（而不是像变异记分卡那样只跑参考矩阵）：
变异记分卡量的是测试质量，与 OS / Python 无关；三道新门禁守的是
「这个仓库此刻的安全与性能姿态」，而那恰恰是最可能在某一条矩阵上
被新引入的问题悄悄带进来的地方。


### 基准数字的对账链

`docs/benchmark_baseline.json` 是最近一次全量实测的快照，由

```bash
python scripts/benchmark_loghub.py --budgets 32000 --write-baseline
```

生成。`prove.py` 的 `claim_benchmark_table` 拿它与**三份文档**里的十行基准表
**逐格对账**（容差 0.05pp，与判胜的容差同口径），并从 `verdict_*` 字段重算
「N 胜 N 平 N 负」的场次。少一份副本就变红 —— `_BENCH_DOCS` 白名单本身
也被 `tests/test_benchmark_table_gate.py::test_every_benchmark_copy_is_listed` 守着。

**为什么需要它**：v2.9.9 修了 `mask_text` 的两处误伤，改动本身是对的，
但它改变了聚类结果 —— HPC 76 簇 / 85.1% 变成 79 簇 / 81.1%、
Zookeeper 92.1% 变成 91.0%、Proxifier 交付口径 81.8% 变成 72.7% ——
而 README、`docs/BENCHMARK.md` 与「唯一负例只差 2 个事件」全部原地不动，
**六道门禁全绿**。与 v2.9.5 撤回的那次是同一个病：对外数字没人校。

因此：**改了任何影响聚类的代码都要重跑全量基准**。只跑子集或缺对照臂时
`--write-baseline` 会拒绝写文件 —— 残缺基线会把「这次没测」固化成
「产品就是这样」，比没有基线更糟。

### 诊断脚本

```bash
python scripts/diagnose_merge_decisions.py          # 需先 fetch_datasets
```

回答的是「有没有任何相似度量能把『该合并』和『不该合并』分开」。
结论记在 `tests/test_cluster_token_guard.py` 里，断言守着它 ——
哪天判据出现了，那些断言会失败并提示重新评估。

### 启动脚本的硬约束

`start.bat` **必须是纯 ASCII + CRLF 行尾**，测试会强制校验。原因见 `tests/test_launcher.py` 顶部注释：cmd.exe 逐字节按控制台代码页解析批处理文件，用裸 LF 或含非 ASCII 字节都会导致解析错位、每行开头被吞，程序永远起不来。所有中文提示都放在 Web 界面里，不放 bat。

---
