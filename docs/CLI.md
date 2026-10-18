# CLI 使用与自定义解析规则

> 这份文档由 `tests/test_cli_doc_gate.py` 守着：**子命令与长选项必须与解析器
> 实际注册的一致**，所以照着抄不会抄错，也不会抄到一个不存在的参数。
> 上一版这份文件里 `bash` 围栏开了没关、CLI 章节一个字都没有，而 README 却
> 称它「完整命令行用法」—— 那种文档比没有更糟。

## 全部子命令

| 子命令 | 干什么 | 退出码 |
| --- | --- | --- |
| `run` | 分析日志并导出报告 | 0 成功 / 1 出错 |
| `compare` | 两份日志（v2 vs v1）逐条对比 | 0 / 1 |
| `verify` | 重读原文，核对每条结论的出处摘要 | **见下方「verify 的四档」** |
| `rules` | `rules list` / `rules show <模板名>` | 0 / 1 |
| `web` | 启动本地 Web 界面 | 0 / 1 |
| `mcp` | 以 MCP Server 方式运行，或 `--install` 写入 Agent 配置 | 0 / 1 |
| `ai` | 解读（可选 extra；**不参与判定**，只读已算好的证据） | 0 / 1 |

## `run` —— 分析并导出

```bash
log-ai-compressor run app.log                      # 默认 md，输出 app_report.md
log-ai-compressor run app.log -f json -o report.json
log-ai-compressor run a.log b.log --top 50         # 多文件合并聚类
log-ai-compressor run app.log --rule my_format.yaml
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

**`-f` 只接受上面这五个值。** 写 `summary` 会直接报错退出 —— 这不是文档
细节，`CONTRIBUTING.md` 曾经就写着 `-f summary`，而它跑不起来。

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
