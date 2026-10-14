# Changelog

本文件记录**用户可见**的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

项目的门禁与测试全绿才允许发版：ruff / pytest+覆盖率 / 对抗性评分卡 /
确定性指纹 / 自证脚本（`python scripts/prove.py`）。任何一条不过就不发。

---

## [2.1.0] - 2026-10-14

取证层能力的补完与两处一致性修复。新增能力 + 修复，无破坏性变更。

### 新增 · 证据自校验进入浏览器

- `POST /api/verify`：取内存中的 job.result，复用 service 层同一套
  `verify_refs`（**不另写一份校验逻辑** —— 两份实现迟早漂移）
- Web 界面判定卡片下方新增「复核证据出处」按钮与三态结果框。
  取证层的主张「每条结论都能被独立复核」此前**只在命令行成立** ——
  在浏览器里看到证据出处却无法当场验证，那它就只是一句 README 的话
- 6 条接口测试：未改动通过 / **改动后报 TAMPERED** / 粘贴文本如实报
  无源可读 / 未知 job 404 / 缺字段 404 / 部分失配时 ok=False

### 新增 · 结构化字段别名扩到六个生态

原表约 20 个键，实测中大量真实形态落在表外。现按实测形态扩表（保留
大小写折叠，`TraceId` → `traceid` 命中）：

| 生态 | 覆盖字段 |
| --- | --- |
| OpenTelemetry Logs | severity_text / body / time_unix_nano / trace_id |
| Elastic ECS | log.level / service.name / trace.id / event.original |
| Grafana Loki | level / msg / traceID |
| Serilog / zap | Timestamp / Level / Message / TraceId / CorrelationId |
| winston / pino | level / msg / service / req_id |
| structlog | event / logger / level |
| B3 传播头 | X-B3-TraceId / X-B3-SpanId |

加别名有反向风险（泛键可能顶掉真正的 message），故补边界测试：
`{"message":"...","name":"some-container"}` 的 message 必须仍是真消息。

### 修复 · 证据摘要精度两条出口不一致

同一份 `EvidenceRef` 走两条出口精度不同：Web API 给 16 位短摘要、
JSON 导出给完整 64 位，字段名还都叫 `digest`。后果是第三方拿 Web API
的数据跑 `verify` **必然失配**，而症状是「哈希对不上」，极难反推到
根因是序列化层截断 —— 取证工具的证据摘要不一致，等于证据不可信。

- `EvidenceRef.as_dict()` 一律给完整 sha256，截断只留在人读展示路径
- 新增 4 条测试钉死「所有出口摘要精度一致」这条契约

### 修复 · CI 门禁在非 UTF-8 控制台下崩溃

Windows runner 控制台是 cp1252，`adversarial_eval.py` **评分 35/35
全通过**却在 print() 中文表格时抛 UnicodeEncodeError，整个 step 变红。

这条比 bug 本身更值得修：症状是「CI 红了」而真相是「功能是好的」。
一个会因为控制台编码变红、无需改动就自己变绿的门禁，会**训练所有人
忽略红色** —— 比没有门禁更糟。

- 两个门禁脚本启动时把 stdout/stderr 切 UTF-8 + errors=replace
- 新增不变量组 I7（4 条），其中两条**真的用 cp1252 子进程跑一遍门禁**
  （断言源码里有某行字只是在检查拼写，只有真跑才算数）

### 顺带

- 补 `py.typed`（PEP 561）：全项目写了完整类型注解，但没有标记文件时
  下游 mypy / IDE 拿不到任何类型信息，注解只是给人看的注释
- PyPI 包描述改为与仓库口径一致（旧的仍以「日志压缩」打头）

### 版本号说明

2.0.1 于 2026-10-14 05:40 发布到 PyPI。其后 main 又积累了本节内容，
因此本版升为 **2.1.0** 而非复用 2.0.1 —— 同一版本号对应两套代码，
恰恰是本项目最不能犯的错（自称可复核，版本号却在撒谎）。

### 测试

825 → **840**（+6 接口测试、+8 别名测试、+4 编码不变量），覆盖率 94%。

---

## [2.0.1] - 2026-10-14

### 新增 · 取证闭环

- **证据引用**：每条结论绑定 `evidence_id`（确定性 sha256）+ 文件 +
  行号区间 + 原文内容摘要。见 `log_ai_compressor/core/evidence.py`
- **`log-ai-compressor verify`**：重读原始日志逐条核对结论出处。
  两种模式 —— `--report <report.json>` 复核已导出报告（有取证意义）、
  `verify <log>` 当场自检（同义反复，仅健康检查，输出中明说两者区别）
- 判定只有 `VERIFIED` / `TAMPERED` / `NO_EVIDENCE` 三档，**没有「大部分通过」**
- **确定性指纹**：同一份输入两次分析，结论部分逐字节相同。报告头部、
  JSON 导出与 Web API 均携带
- JSON 导出现在携带完整证据摘要，精简模式同样携带

### 新增 · 结构化日志

- **JSON Lines 与 logfmt 零配置原生解析**，无新增依赖
  （`log_ai_compressor/core/structured.py`）
- 兼容 OTel `log.level` 点号命名空间、ECS `severity_text`、
  真嵌套 `{"service":{"name":…}}`、`exception.type`、
  logfmt 引号转义与 `msg=` 后的自由文本尾部
- 判定用「第一个 kv 对必须从行首开始」而非覆盖率 —— 覆盖率会同时造成
  误吞普通消息与漏判长消息两类错误

### 新增 · 调用链重建与证据缺口自闭环

- 按 `trace_id` / `request_id` 还原跨服务链路与首个失败点
  （`log_ai_compressor/core/traces.py`），不需要 agent、不需要拓扑
- 缺口清单改为数据驱动：日志带关联键 → 链路重建成功 →
  「缺少调用链关联」这条权重 80 的缺口**自动关闭**；一条都没有 →
  如实申报并说清补什么格式就能补上
- **严格语义边界**：链路只证明顺序不证明因果，**永不因此升到
  CONFIRMED**。CONFIRMED 的唯一来源始终是条目内 `Caused by:` 链

### 新增 · 对抗性评测（CI 门禁）

- `scripts/adversarial_eval.py`：10 个专门设计来误导本工具的场景，
  35 项评分。当前 **100% 正确弃权 / 0% 自信错误率**
- 口径写死在脚本里：**弃权算通过**（只算命中率的评测会偏好猜得最自信的
  系统）；**自信错误率权重最高**（值班的人会照着错误根因去排查）
- 含反向场景（证据够了不该虚报缺口）与误判场景（普通文本不得被吞成 logfmt）

### 新增 · 自证脚本（CI 门禁）

- `scripts/prove.py`：把对外每条主张变成一条命令可跑的数字，
  并**刻意打印尚无数据的主张**。README 与产品不允许漂移
- 附带输出 `docs/images/web-ui.jpg` 对应的真实界面截图说明

### 新增 · 接入面

- MCP 新增第 8 个工具 `verify_evidence`（含复核已导出报告模式）
- MCP `INSTRUCTIONS` 由功能清单重写为 **Agent 使用剧本**：
  明确教 Agent 不要自己 grep、不要把 LIKELY 说成根因、
  不要把共现说成因果、引用结论必须带行号
- 新增 `SKILL.md`（Agent Skills 开放标准，`npx skills add` 可安装）
- Web 界面展示每簇的证据出处与调用链

### 修复 · 18 个已复现缺陷

以下问题此前 650 条测试与 ruff 全绿，**一条都抓不到** —— 它们属于同一类：
工具在用户不知情时给出看起来正常的错误结果。

- 簇 `last_seen` 被无时间戳条目抹成 `None`（报告「首末时间」变空、
  优先级里的「持续发生」判定失效）
- 故障窗口 z-score 的分子未按裁剪窗口统计（日志范围外的实例被计入
  分子，稳态错误被报成突发）
- 跨零点时段过滤（22:00~06:00）把一整天都判为区间外 → 夜间窗口返回空报告
- 变量分布传错模板，带堆栈的簇（几乎所有真错误）恒为空
- 行数上限采样在 md/txt/html/json 四种报告里完全不可见
  → 报告看起来完整，实际只分析了前 N 行
- 前端：搜索参数写反导致普通子串搜索恒零命中；
  进度条读取后端从不下发的字段（永远 0% / 0 错误）；
  用户「取消」被谎报成「已达行数上限」；
  优先级档位显示成「P80.0」而非 P0~P4；
  `localStorage.getItem` 未保护，禁用存储时整个脚本后半段不执行
- 接入层：`--level WARNING` 静默变成默认级别（用户要警告，报告全是错误）；
  非法规则抛裸 `re.error`（用户看到 traceback 而非一句话）；
  参数类型错误返回 500 而非 4xx；
  `use_regex` 在配置往返中丢失（正则模式静默变回子串匹配）；
  MCP 声称按 mtime 排序却没排（轮转日志时间线全错且看不出异常）
- 其它：脱敏把版本号 `1.2.3.4` 当 IP 抹掉；
  `normalize_level("ERROR:")` 静默降级为 INFO 导致错误行被过滤掉

### 工程

- 测试 650 → **817**，覆盖率 **94%**
- 新增不变量测试 27 条：20 万行同类日志簇数必须为 1、畸形 JSON 不崩、
  分批聚类幂等、非 CONFIRMED 簇永不输出因果措辞、
  CONFIRMED 必须在样本堆栈里真有 `Caused by:` 行
- CI 从 3 道门禁增至 **5 道**
- `pyproject.toml` 的 `ruff target-version` 从 `py39` 修正为 `py311`
  （与 `requires-python >=3.11` 对齐；此前 PyPI 上的 2.0.0 仍自称支持 3.9）

### 文档

- README 第一屏改为「差异点优先」：可复核 / 弃权 / 对抗评测上浮，
  压缩对比表下沉；新增「它做不到什么」一节
- 标题由「日志AI压缩器」改为「本机日志取证层」（包名不变）
- `docs/POSITIONING.md` 升 v3：P0 四条标完成，补入结构化日志的
  P0 级论证与赛道判断的独立验证（Nitpick / FileGrail / LogsLop）
- 新增本 CHANGELOG

### 已知限制（未修复）

- **根因判定的召回率仍无真实数据**：精确率有（0% 自信错误率），
  召回率需要含真实异常栈的数据集，本机网络下反复下载失败。
  宁可空着也不编一个估计值
- 轮转日志在 CLI 与 Web 端仍需手动指定路径（MCP 已按 mtime 自动排序）
- 结构化日志的字段别名表覆盖常见生态，遇到罕见命名需要 YAML 规则补充

---

## [2.0.0] - 2026-10-06

首个公开版本。聚类去重 / 根因置信三档 / 证据充分性评估 / 异常检测 /
优先级评分 / 堆栈降噪 / 多文件对比 / Web 界面 / MCP Server / 可选 AI 解读。
