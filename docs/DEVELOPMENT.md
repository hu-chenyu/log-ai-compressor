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
`ubuntu-3.12`（最常用版本）、`windows-3.12`（行尾 / shell / 路径处理与
unix 不同，且 `start.bat` 是 Windows 一等入口）。**六道门禁**：ruff →
pytest + 覆盖率 ≥90% → 对抗性评分卡 → 确定性指纹 → 自证脚本 → 构建可复现性。
自证脚本内部已扩到 **11 条**主张（v2.8.0 起新增「README 工程指标与仓库一致」，
v2.8.1 再加「README 架构树必须列全包内模块」，v2.9.0 加「接入层只经 service
触达 core/export」，v2.9.1 再把「README 写的条数」本身也纳入对账，
v2.10.0 加「**基准表逐格对账实测基线**」，v2.11.0 从只校 README 扩到**每一份副本**
（README / docs/BENCHMARK.md / docs/FAQ.md），并把「N 胜 N 平 N 负」的场次也纳入重算）。

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
