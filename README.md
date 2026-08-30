# log-ai-compressor · 日志AI压缩器

> 全场景通用日志分析前置处理器 —— 把海量日志压缩成一份 AI 可读的排查报告

[![CI](https://github.com/yourname/log-ai-compressor/actions/workflows/ci.yml/badge.svg)](../../actions)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](./pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](./pyproject.toml)

---

## 1. 项目定位

**log-ai-compressor** 是一个独立开源的 Python GUI 工具，定位为「全场景通用日志分析前置处理器」，面向开发、测试、运维等全技术研发岗位。

两大核心场景：

| 场景 | 痛点 | 解法 |
| --- | --- | --- |
| **日志压缩投喂大模型** | 几十 MB 的日志远超 LLM 上下文窗口，直接粘贴要么截断要么爆 token | 聚类去重 + Top N + 典型样例，产出结构化 Markdown 报告，压缩比通常 50~500 倍 |
| **快速故障排查** | 上百万行日志人工翻找错误，同类错误重复出现干扰判断 | 根因定位 + 优先级排序 + 堆栈降噪 + 时间趋势，直接指出「先查哪里」 |

## 2. 解决的痛点

- 大日志文件（GB 级 / 亿级行）无法直接投喂 AI，人工 grep 排查效率低
- 同一错误重复刷屏（重试风暴），真正不同的错误被淹没
- 堆栈里全是框架/系统库帧，根因帧难找
- 多版本 / 修复前后的日志差异靠肉眼 diff
- 中文日志编码混乱（UTF-8/GBK/GB2312）打开乱码

## 3. 核心特性

**业务核心**
- 双输入模式：文件导入（支持超大文件、编码自动适配 UTF-8/GBK/GB2312/UTF-16）+ 文本粘贴（快速排查）
- 通用日志解析：时间戳 / 级别 / 模块 / 内容 / 堆栈（Java、Python、C/C++、gdb 帧全兼容）
- 模糊指纹聚类去重：行号、参数、十六进制 ID、路径差异全部抹平，同类错误只留一份典型样例 + 前后 5 行上下文
- 智能辅助分析：
  - 错误因果关联（Caused-by 链 / 时间连锁 / 根因关键词）自动区分根因与连锁衍生
  - 统计异常检测（中位数 + MAD 稳健基线，识别集中爆发 / 罕见异常）
  - 优先级综合评分（级别 40% + 频次 30% + 根因 20% + 异常 10%），FATAL 自动置顶
  - 堆栈降噪：折叠 `java.base` / `site-packages` / `node_modules` 等系统库与第三方帧，高亮业务栈帧
- 多文件对比：2~3 个日志的新增 / 消失 / 共同错误与数量变化率，适配版本对比与修复验证
- 可视化面板：错误时间趋势（含爆发点标注）、级别占比饼图、模块分布柱状图，点击图表联动错误列表
- 一键导出：Markdown（LLM 优化格式）/ JSON / 纯文本，一键复制摘要直接投喂 AI

**架构亮点**
- 可插拔解析规则引擎：YAML 声明规则，改配置不改代码即可接入新格式；内置 generic / embedded / jenkins 三套模板
- GUI + CLI 双模式：GUI 日常排查，CLI（`log-ai-compressor run ...`）嵌入脚本与流水线
- 纯流式逐行处理：内存占用只与错误种类数相关，与日志总行数无关
- 配置持久化：常用参数自动保存恢复
- 完整工程化配套：单元/集成/边界测试，GitHub Actions CI，覆盖率门槛 90%

## 4. 快速开始

### 安装

```bash
git clone https://github.com/yourname/log-ai-compressor.git
cd log-ai-compressor
pip install -r requirements.txt
```

> 可选：`pip install tkinterdnd2` 启用 GUI 文件拖拽导入（不装则自动退化为点击选择）

### 启动 GUI

```bash
python run_gui.py            # 或
python -m log_ai_compressor gui
```

### CLI 使用

```bash
# 分析日志并导出 Markdown 报告（默认级别 ERROR,FAIL）
python -m log_ai_compressor run examples/sample_system.log --top 20 -o report.md

# 指定级别、关键字、规则模板
python -m log_ai_compressor run test.log --level ERROR,FAIL,WARN \
    --include "timeout,refused" --rule embedded --top 30 -o report.md

# JSON / 纯文本格式
python -m log_ai_compressor run test.log --format json -o report.json

# 多文件对比（第一个为基准）
python -m log_ai_compressor compare examples/app_v1.log examples/app_v2.log -o diff.md

# 查看内置解析规则
python -m log_ai_compressor rules list
```

pip 安装后可直接使用 `log-ai-compressor` 命令（等价于 `python -m log_ai_compressor`）。

### 开箱演示

仓库自带示例，克隆即可跑通：

```bash
python -m log_ai_compressor run examples/sample_system.log -o my_report.md
```

| 示例文件 | 说明 |
| --- | --- |
| `examples/sample_system.log` | 通用应用日志（Java/Python 堆栈、重复错误、错误爆发、FATAL） |
| `examples/sample_embedded.log` | 嵌入式/UT 日志（`--rule embedded`） |
| `examples/sample_gbk.log` | GBK 编码中文日志（编码自动探测演示） |
| `examples/app_v1.log` / `app_v2.log` | 版本对比演示对 |
| `examples/example_report.md` | 上述日志的标准输出报告 |

## 5. 输出报告结构（为投喂大模型优化）

```markdown
# 日志AI压缩报告：app.log
> 处理 1,200,000 行 | 耗时 12.4s | 9.7 万行/秒 | 规则 generic
**初步定位根因**：connection refused to db-primary:5432
## 一、概览统计          —— 全局认知（行数/错误数/种类/时间范围/级别分布）
## 二、Top 20 错误清单    —— 表格化去重全集（优先级/次数/模块/根因/异常）
## 三、典型样例详情       —— 每错误一份：元信息 + 前后上下文 + 降噪堆栈
```

「一键复制摘要」产出更精简的纯文本版本，适合直接粘贴给 AI 助手。

## 6. 技术架构

```
log_ai_compressor/
├── rules/                    # 可插拔解析规则引擎（YAML 驱动）
│   ├── engine.py             #   规则加载/编译/占位符展开/{LEVEL} 统一级别口径
│   └── presets/              #   generic / embedded / jenkins 三套模板
├── core/                     # 核心处理层（与 UI 完全分离）
│   ├── models.py             #   数据模型 + 自适应时间直方图（内存 O(桶数)）
│   ├── encoding.py           #   编码探测（BOM/严格解码验证/截断容忍）
│   ├── parser.py             #   增量解析器（多行聚合：折行/堆栈/Caused-by）
│   ├── filters.py            #   级别 + 关键字准入过滤
│   ├── clustering.py         #   模糊指纹聚类（三级匹配：精确/消息模板/编辑距离）
│   ├── analysis.py           #   根因判定/异常检测/优先级/堆栈降噪
│   ├── pipeline.py           #   流式管线（进度/取消/上下文捕获）
│   └── comparator.py         #   多文件对比
├── export/reporters.py       # 导出层（Markdown/JSON/文本/摘要/对比报告）
├── gui/                      # GUI 层（CustomTkinter）
│   ├── app.py                #   主窗口：三 Tab + 线程化任务调度
│   ├── charts.py             #   Matplotlib 三联图表 + 点击联动
│   └── config_store.py       #   配置持久化
└── cli.py                    # CLI 入口（run/compare/rules/gui）
```

**分层解耦**：`rules → core → export → gui/cli` 单向依赖。核心层零 UI 依赖，可独立测试、被脚本复用（`from log_ai_compressor.core.pipeline import analyze_file`）。

### 核心算法

1. **模糊指纹聚类（两级性能保护）**
   - 指纹 = 级别 + 掩码消息（数字→N、十六进制→H、UUID→U、路径→P、引号串→S）+ 堆栈前 3 行特征
   - 匹配路径：完整指纹精确命中（O(1)，覆盖绝大多数）→ (级别, 消息模板) 精确命中（堆栈差异合并）→ 同级别桶内编辑距离相似度（上限 256 次比较）
   - 变体命中后回写精确表，后续重复变体继续 O(1)

2. **内存控制**
   - 逐行流式读取，簇内只存「模板 + 计数 + 一份样例 + 有界直方图」
   - 时间直方图桶数上限固定（簇 96 / 全局 512），超限自动 8 倍扩宽桶宽合并旧桶

3. **根因判定（三路证据融合）**：Caused-by 链回溯 + 60 秒窗口内首发且含根因关键词 + 强关键词命中（≥3）；含 retry/after/downstream 等被动词的簇标记为连锁衍生

## 7. 性能数据

| 指标 | 实测值（Python 3.9 / 普通办公机 / 单核） | 目标 |
| --- | --- | --- |
| 处理速度 | 5.3 万行/秒（含 20% 错误密度的混合负载）；INFO 为主的常规日志可达 10 万行/秒 | ≥ 3 万行/秒 ✅ |
| 峰值内存 | 3.7 MB（100 万行 / 57MB 日志） | ≤ 120MB ✅ |
| 内存随行数增长 | 无关（仅与错误种类数相关） | ✅ |
| 智能分析开销 | 0.1%（仅错误簇参与，与总行数解耦） | ≤ 15% ✅ |
| 几千行小日志 | < 0.1 秒 | 秒级 ✅ |

复现基准：`python scripts/benchmark.py`（生成 100 万行合成日志并计时测内存）。

## 8. 自定义解析规则

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

## 9. 开发与测试

```bash
pip install -r requirements-dev.txt

ruff check log_ai_compressor tests       # 代码规范检查
python -m pytest                          # 全量测试（168+ 用例）
python -m pytest --cov=log_ai_compressor --cov-fail-under=90   # 覆盖率门槛
```

CI（GitHub Actions）：矩阵（Ubuntu/Windows × Python 3.9/3.12）自动执行规范检查、测试与覆盖率统计。

## 10. License

MIT
