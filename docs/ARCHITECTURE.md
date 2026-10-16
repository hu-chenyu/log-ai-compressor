# 技术架构

## 技术架构

```
log_ai_compressor/
├── constants.py              # 全局常量（时间上限 / 内存有界参数 / 展示上限）
├── service.py               # 共享服务层：参数校验 + JSON 序列化 + 导出门面
├── __main__.py              # `python -m log_ai_compressor` 入口（退出码即契约）
├── rules/                   # 可插拔解析规则引擎（YAML 驱动）
│   ├── engine.py            #   规则加载/编译/占位符展开
│   └── presets/             #   generic / embedded / jenkins 三套模板
├── core/                    # 核心处理层（零 UI / 零 Web 依赖）
│   ├── models.py            #   数据模型 + 自适应时间直方图
│   ├── encoding.py          #   编码探测（BOM/严格解码验证/截断容忍）
│   ├── structured.py        #   结构化日志原生解析（JSON Lines / logfmt）
│   ├── parser.py            #   增量解析器（多行聚合：折行/堆栈/Caused-by）
│   ├── causal.py            #   Caused-by 因果链识别原语（摄入层与分析层共用）
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
