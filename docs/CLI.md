# CLI 使用与自定义解析规则

> 这份文档由 `tests/test_cli_doc_gate.py` 守着：**子命令与长选项必须与解析器
> 实际注册的一致**，所以照着抄不会抄错，也不会抄到一个不存在的参数。
> 上一版这份文件里 `bash` 围栏开了没关、CLI 章节一个字都没有，而 README 却
> 称它「完整命令行用法」—— 那种文档比没有更糟。

## 全部子命令

| 子命令 | 干什么 | 退出码 |
| --- | --- | --- |
| `run` | 分析日志并导出报告 | 0 成功 / 1 出错 / **2 不是日志文件** |
| `compare` | 两份日志（v2 vs v1）逐条对比 | 0 / 1 |
| `verify` | 重读原文，核对每条结论的出处摘要 | **见下方「verify 的四档」** |
| `rules` | `rules list` / `rules show <模板名>` | 0 / 1 |
| `web` | 启动本地 Web 界面 | 0 / 1 |
| `mcp` | 以 MCP Server 方式运行，或 `--install` 写入 Agent 配置 | 0 / 1 |
| `ai` | 解读（可选 extra；**不参与判定**，只读已算好的证据） | 0 / 1 |

> **退出码 2 是新加的**（2026-10-22）：喂进去的不是文本日志（纯二进制 /
> 图片 / 压缩包外的随机字节）。此前它会被当文本「成功分析」一遍，报告里写着
> 「压缩 95%」，退出码 0 —— 用户完全看不出自己喂错了文件。
> 「文件选错了」（2）与「分析失败」（1）对调用方意义不同，CI 里按码分流才用得上。

## `run` —— 分析并导出

```bash
log-ai-compressor run app.log                      # 默认 md，输出 app_report.md
log-ai-compressor run app.log -f json -o report.json
log-ai-compressor run a.log b.log --top 50         # 多文件合并聚类
log-ai-compressor run app.log --rule my_format.yaml
log-ai-compressor run -                            # 从标准输入读（管道）
log-ai-compressor run app.log --tail 5000          # 只看最后 5000 行
log-ai-compressor run app.log -f json              # stdout 就是 JSON
```

| 选项 | 说明 |
| --- | --- |
| `--level`, `-l` | 只看这些级别（如 `ERROR,WARN`） |
| `--include`, `-k` | 包含关键字（逗号分隔，任一命中保留） |
| `--exclude` | 排除关键字 |
| `--top`, `-t` | Top N（默认 20） |
| `--context` | 典型样例上下文行数（默认 50） |
| `--rule`, `-r` | 解析规则：模板名（`generic` / `embedded` / `jenkins`）或 YAML 路径 |
| `-o`, `--output` | 输出路径 |
| `--format`, `-f` | `json` / `markdown` / `md` / `text` / `txt`（默认按输出后缀推断） |
| `--no-analysis` | 跳过根因/异常/优先级分析 |
| `--quiet`, `-q` | 抑制进度输出 |
| `--tail N` | 只分析**最后 N 行**（0 = 全部）。行号仍指向**原文件**，报告里会写明「只看最后 N 行（文件共 M 行，起始行 K）」 |

**`-f` 只接受上面这五个值。** 写 `summary` 会直接报错退出 —— 这不是文档
细节，`CONTRIBUTING.md` 曾经就写着 `-f summary`，而它跑不起来。

### 输出到哪儿：`-o` 给了就落盘，没给就看格式

| 情况 | 结果 |
| --- | --- |
| `-o out.json` | 写文件；stdout 打印人话摘要 |
| `-f json`（无 `-o`） | **JSON 打到 stdout**（可直接接 `jq`）；不产生文件 |
| `-f txt`（无 `-o`） | 纯文本打到 stdout |
| 都不给 | 写 `<日志名>_report.md`；stdout 打印人话摘要 |

> `-f json` 不带 `-o` 时 stdout 是 JSON —— 此前不管 `-f` 写什么都只打印中文
> 摘要，而 `-f json` 这个参数看上去像是生效了的。接 `jq` 会直接失败。
> 一条命令的输出格式由**格式参数**决定，语义才该由「写文件还是打屏幕」决定。

## `compare` —— 多文件对比

**2~3 个文件**（第一个为基准；少于 2 个或多于 3 个会被明确拒绝并给出
人话错误，不是 argparse 的 usage 堆栈）。

```bash
log-ai-compressor compare v1.log v2.log                 # 写 compare_report.md
log-ai-compressor compare v1.log v2.log -f json         # stdout 是 JSON
log-ai-compressor compare v1.log v2.log -o diff.md -q    # 静默
```

| 选项 | 说明 |
| --- | --- |
| `--level`, `-l` / `--include`, `-k` / `--exclude` / `--top`, `-t` / `--context` / `--rule`, `-r` | 与 `run` 同义 |
| `-o`, `--output` | 输出路径（默认 `compare_report.md`） |
| `--format`, `-f` | `json` / `markdown` / `md` / `text` / `txt` |
| `--quiet`, `-q` | 抑制进度输出 |

**对比按两个键对齐**：先按**消息模板**（同一错误一处带堆栈、一处不带也算
同一类），再按 **`trace_id` 兜底** —— 同一次请求在两个文件里措辞不同
（上游 `connection refused`、下游 `upstream unavailable`）时，按模板看是两种
错误，按 trace 看是**同一件事**。没有第二级的话，这一次事故会被算成
「新增 1 种 + 消失 1 种」，读起来像「出了新问题、同时修好了老问题」。

> **`trace_id` 兜底只对结构化日志（JSON Lines / logfmt）成立。**
> 关联键是从结构化字段里**认领**的；纯文本里写着的 `trace_id=t1` **认不出来**
> （工具不猜），那种情况下仍会算成「新增 + 消失」。

报告里带 🔗 的行就是按 trace 对上的。`compare -f json` 的每一项还带
`matched_by` 字段（`template` / `trace`）—— MCP 的 `compare_log_files`
工具也带它，所以 Agent 看到的是**同一份**对齐依据。

## `verify` —— 重读原文自校验

```bash
log-ai-compressor verify --report report.json    # 复核一份已导出的报告
log-ai-compressor verify app.log                 # 当场自检
log-ai-compressor verify --report r.json --json  # 机器可读
```

结果有**四档**，措辞与退出码都不同：

| verdict | 含义 | 退出码 |
| --- | --- | --- |
| `VERIFIED` | 每条结论都能在原始日志里逐行复核 | 0 |
| `TAMPERED` | **回读过了，对不上** —— 日志确实被改过 | 1 |
| `UNVERIFIABLE` | 有引用，但**一条都回读不了**（源被删/轮转/粘贴文本） | 1 |
| `NO_EVIDENCE` | 本来就没有证据引用 | 0 |

`UNVERIFIABLE` **不等于** `TAMPERED`：前者是「验不了」，后者是「查出了对不上的
东西」。把它俩混为一谈等于指控一个没发生的篡改。两者退出码都是 1，因为结论
**确实已无法被核实** —— 返回 0 等于说「那份报告还成立」。

### 证据摘要怎么算的 —— 让你能脱离本工具自己复核

报告里每条结论都带一个 `digest`（那段原文的 sha256）。本工具承诺
「用户可脱离本工具自校验」—— 那就得**说得出怎么算**：

> **取报告里的行区间 `[line_start, line_end]` →
> 逐行去掉行尾的 `\r\n` → 用 `\n` 连接 → 对这段 UTF-8 字节算 sha256。**

举例（`examples/app.log` 的第 18~22 行）：

```python
import hashlib, pathlib
raw = pathlib.Path("examples/app.log").read_bytes().splitlines(keepends=True)
block = b"\n".join(ln.rstrip(b"\r\n") for ln in raw[17:22])
print(hashlib.sha256(block).hexdigest())
# 与报告里那条引用的 digest 相同
```

三条容易踩的：

| 事项 | 规矩 |
| --- | --- |
| **按原始字节算** | 行里有 ANSI 转义序列时，摘要按**带转义**的原始字节算 —— 剥掉 ANSI 再算就对不上 |
| **行尾不算进去** | 每行末尾的 `\r\n` / `\n` 都去掉，所以同一份日志在 LF 与 CRLF 两个平台上的摘要**相同** |
| **多行区间用 `\n` 连接** | 不是把行首尾相接，也不是带换行 —— 行数变一个字符，摘要就变 |

> 这一段此前**没有任何文档写过**：README、docs/CLI.md、docs/FAQ.md 只说
> 「那段原文的 sha256」，没给算法。承诺本身是成立的（实测 4/4 条引用都能
> 自己对上），但读者只能逆推 —— 而「可脱离本工具自校验」这句话的意思
> 恰恰是「不用逆推」。现在由 `tests/test_evidence_digest_formula.py` 钉住。

## `web` / `mcp` / `ai`

```bash
log-ai-compressor web                                  # 默认 127.0.0.1
log-ai-compressor web --host 0.0.0.0 --allow-remote    # 必须显式确认
log-ai-compressor mcp --install claude-code
log-ai-compressor mcp --transport streamable-http
```

| 选项 | 属于 | 说明 |
| --- | --- | --- |
| `--host` | `web` | 绑定地址（默认仅本机） |
| `--allow-remote` | `web` | 确认绑定非本机地址。**服务没有鉴权**，网络内任何人能读本机文件 |
| `--port` | `web` / `mcp` | 监听端口 |
| `--no-browser` | `web` | 不自动开浏览器 |
| `--log-level` | `web` | `critical` / `error` / `warning` / `info` / `debug` |
| `--transport` | `mcp` | `stdio` / `streamable-http` / `sse` |
| `--install` | `mcp` | 写入指定 Agent 的 MCP 配置 |

`web` 默认同时做两件事：**回环绑定** + **Host 头校验**。只绑回环**不足以**
推出「只有本机能访问」—— 同源策略挡不住 DNS rebinding。

## 自定义解析规则

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

内置模板：`log-ai-compressor rules list` / `rules show generic`。
