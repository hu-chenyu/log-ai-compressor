# 日志对比分析报告

## 基准 `app_v1.log` vs `app_v2.log`

- 新增错误：**1** 种（共 3 次）
- 消失错误：**2** 种（基准中共 3 次）
- 共同错误：**2** 种（共 6 次）

### 新增错误（基准中不存在）

| 级别 | 次数 | 模块 | 错误摘要 |
| --- | --- | --- | --- |
| ERROR | 3 | newmod | regression: new payment module crash |

### 消失错误（对比文件中已不存在）

| 级别 | 次数 | 模块 | 错误摘要 |
| --- | --- | --- | --- |
| ERROR | 2 | cache | lock contention timeout: key=job:42 |
| ERROR | 1 | legacy | old bug: deprecated endpoint still called |

### 共同错误（数量变化）

| 级别 | 基准次数 | 对比次数 | 变化率 | 模块 | 错误摘要 |
| --- | --- | --- | --- | --- | --- |
| ERROR | 2 | 5 | +150.0% | auth | token expired for session 7c4d5e |
| ERROR | 4 | 1 | -75.0% | db | connection refused to shard-0:5432 |

