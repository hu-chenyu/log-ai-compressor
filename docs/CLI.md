# CLI 使用与自定义解析规则

## CLI 使用

```bash

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

---
