# 核心特性

## 核心特性

**分析引擎（全部本地算法，零出网）**

- 双输入模式：文件导入（超大文件、编码自动适配 UTF-8/GBK/GB2312/UTF-16/UTF-32，`.gz`/`.zip` 透明解压）+ 文本粘贴
- **接管道**：`log-ai-compressor run -` 从标准输入读日志（解码与文件路径走**同一个**解码器，所以「管道进来的 GBK 日志」和「文件里的 GBK 日志」解得一样）
- **只看最近 N 行**：`--tail N`。日志几十 GB 而你要看的是最后几千行；
  **行号仍指向原文件**（尾部窗口的第 1 行在原文件里可能是第 999 万行 —— 行号错了，证据引用就指错了地方）
- **混合编码也能解**：同一文件里 UTF-8 行与 GBK 行混着时逐行兜底解码，
  并**如实说**「有 N 行不是主编码」（此前那批行会静默变成替换符，数据被改了而报告里看不出异常）
- **不是日志就明说**：纯二进制 / 图片喂进来直接拒绝（**退出码 2**，与「分析失败」的 1 区分开），
  而不是当文本「成功分析」一遍再写一句「压缩 95%」
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
- 多文件对比：2~3 个文件的新增 / 消失 / 共同错误与数量变化率，适配版本对比与修复验证。
  **两级对齐**：先按消息模板（同一错误一处带堆栈、一处不带也算同一类），
  再按 **`trace_id` 兜底** —— 同一次请求在两个文件里措辞不同
  （上游 `connection refused`、下游 `upstream unavailable`）时，
  按模板看是两种错误，按 trace 看是**同一件事**。没有第二级的话，
  这一次事故会被算成「新增 1 种 + 消失 1 种」，读起来像「出了新问题、
  同时修好了老问题」。报告里带 🔗 的行就是按 trace 对上的
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
