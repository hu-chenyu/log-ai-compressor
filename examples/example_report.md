# 日志AI压缩报告：examples\sample_system.log

> log-ai-compressor v1.0.0 | 处理 52 行 | 耗时 0.00s | 4.3 万行/秒 | 规则 generic

**初步定位根因**：connection refused to db-primary:5432（另有 1 个根因候选）

## 一、概览统计

| 指标 | 数值 |
| --- | --- |
| 日志来源 | examples\sample_system.log |
| 编码 | utf-8 |
| 总行数 | 52 |
| 错误行数（FATAL/ERROR/FAIL） | 22 |
| 错误种类数（去重后） | 8 |
| 错误总次数（过滤后） | 22 |
| 日志时间范围 | 2024-03-15 08:00:00 ~ 2024-03-15 08:12:05 |

级别分布：DEBUG=1, ERROR=21, FATAL=1, INFO=9, WARN=5

## 二、Top 8 错误清单（按优先级排序）

| # | 优先级 | 级别 | 次数 | 模块 | 根因 | 异常 | 错误摘要 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | P0 | FATAL | 1 | core | — | 罕见异常 | out of memory: worker-3 aborted, allocation of 1073741824 bytes failed |
| 2 | P1 | ERROR | 4 | db | ✔ 时间连锁源头（窗口内首发且含根因 | — | connection refused to db-primary:5432 |
| 3 | P1 | ERROR | 1 | auth | ✔ 时间连锁源头（窗口内首发且含根因 | 罕见异常 | session token validation failed: token expired |
| 4 | P1 | ERROR | 12 | api | — | — | request 10842 failed: upstream unavailable |
| 5 | P2 | ERROR | 1 | scheduler | — | 罕见异常 | task cleanup aborted after 3 retries |
| 6 | P2 | ERROR | 1 | cache | — | 罕见异常 | redis protocol error: invalid bulk length |
| 7 | P2 | ERROR | 1 | core | — | 罕见异常 | worker-3 terminated unexpectedly (exit code 137) |
| 8 | P2 | ERROR | 1 | api | — | 罕见异常 | rare one-time glitch in request 5123521 serialization |

## 三、典型样例详情（每错误一份，含上下文与降噪堆栈）

### 1. [P0][FATAL] out of memory: worker-3 aborted, allocation of 1073741824 bytes failed

- 出现 1 次 | 行 47~47 | 首末时间 2024-03-15 08:07:45 ~ 2024-03-15 08:07:45 | 模块 core
- 智能分析：疑似连锁衍生错误（被动失败特征）；异常：罕见异常

**前上下文**
```
  File "/opt/acme/cache_client.py", line 88, in get
    value = self._read_bulk(sock)
  File "/usr/lib/python3.9/site-packages/redis/connection.py", line 221, in read_response
    response = self._parser.read_response()
ValueError: invalid bulk length: 18446744073709551615
```

**典型样例**
```
2024-03-15 08:07:45.000 FATAL [core] out of memory: worker-3 aborted, allocation of 1073741824 bytes failed
```

**后上下文**
```
2024-03-15 08:07:45.100 ERROR [core] worker-3 terminated unexpectedly (exit code 137)
2024-03-15 08:07:45.200 INFO [supervisor] restarting worker-3
2024-03-15 08:10:02.000 ERROR [api] rare one-time glitch in request 5123521 serialization
2024-03-15 08:12:00.000 WARN [pool] connection pool recovered
2024-03-15 08:12:05.000 INFO [api] service healthy again
```

### 2. [P1][ERROR] connection refused to db-primary:5432

- 出现 4 次 | 行 11~25 | 首末时间 2024-03-15 08:05:12 ~ 2024-03-15 08:05:14 | 模块 db
- 智能分析：根因：时间连锁源头（窗口内首发且含根因特征）

**前上下文**
```
2024-03-15 08:01:10.050 INFO [api] GET /api/users 200 12ms
2024-03-15 08:01:10.200 INFO [api] GET /api/orders 200 33ms
2024-03-15 08:02:11.000 INFO [api] POST /api/login 200 58ms
2024-03-15 08:03:40.220 INFO [api] GET /api/products 200 9ms
2024-03-15 08:04:59.900 WARN [db] slow query detected: 1203ms SELECT * FROM audit_events
```

**典型样例**
```
2024-03-15 08:05:12.300 ERROR [db] connection refused to db-primary:5432
```

**堆栈（已降噪：业务帧 3 行，折叠系统/第三方帧 3 行）**
```
java.net.ConnectException: Connection refused (Connection refused)
	at com.acme.db.ConnectionPool.init(ConnectionPool.java:142)
    ...... 已折叠 2 行系统库/第三方栈帧 ......
	at com.acme.core.ServiceManager.start(ServiceManager.java:88)
    ...... 已折叠 1 行系统库/第三方栈帧 ......
```

**后上下文**
```
2024-03-15 08:05:12.320 WARN [pool] retrying connection attempt 1 of 5
2024-03-15 08:05:13.100 ERROR [db] connection refused to db-primary:5432
	at com.acme.db.ConnectionPool.init(ConnectionPool.java:142)
	at java.base/java.net.Socket.connect(Socket.java:606)
Caused by: java.net.ConnectException: Connection refused
```

### 3. [P1][ERROR] session token validation failed: token expired

- 出现 1 次 | 行 39~39 | 首末时间 2024-03-15 08:06:00 ~ 2024-03-15 08:06:00 | 模块 auth
- 智能分析：根因：时间连锁源头（窗口内首发且含根因特征）；异常：罕见异常

**前上下文**
```
2024-03-15 08:05:15.450 ERROR [api] request 10855 failed: upstream unavailable
2024-03-15 08:05:16.020 ERROR [api] request 10861 failed: upstream unavailable
2024-03-15 08:05:16.090 ERROR [api] request 10862 failed: upstream unavailable
2024-03-15 08:05:20.700 ERROR [api] request 10901 failed: upstream unavailable
2024-03-15 08:05:21.000 ERROR [scheduler] task cleanup aborted after 3 retries
```

**典型样例**
```
2024-03-15 08:06:00.000 ERROR [auth] session token validation failed: token expired
```

**后上下文**
```
2024-03-15 08:06:30.250 ERROR [cache] redis protocol error: invalid bulk length
Traceback (most recent call last):
  File "/opt/acme/cache_client.py", line 88, in get
    value = self._read_bulk(sock)
  File "/usr/lib/python3.9/site-packages/redis/connection.py", line 221, in read_response
```

### 4. [P1][ERROR] request 10842 failed: upstream unavailable

- 出现 12 次 | 行 26~37 | 首末时间 2024-03-15 08:05:14 ~ 2024-03-15 08:05:20 | 模块 api

**前上下文**
```
	at java.base/java.net.Socket.connect(Socket.java:606)
Caused by: java.net.ConnectException: Connection refused
2024-03-15 08:05:13.610 ERROR [db] connection refused to db-primary:5432
2024-03-15 08:05:13.900 WARN [pool] retrying connection attempt 3 of 5
2024-03-15 08:05:14.100 ERROR [db] connection refused to db-primary:5432
```

**典型样例**
```
2024-03-15 08:05:14.180 ERROR [api] request 10842 failed: upstream unavailable
```

**后上下文**
```
2024-03-15 08:05:14.190 ERROR [api] request 10843 failed: upstream unavailable
2024-03-15 08:05:14.220 ERROR [api] request 10844 failed: upstream unavailable
2024-03-15 08:05:14.310 ERROR [api] request 10845 failed: upstream unavailable
2024-03-15 08:05:15.050 ERROR [api] request 10851 failed: upstream unavailable
2024-03-15 08:05:15.140 ERROR [api] request 10852 failed: upstream unavailable
```

### 5. [P2][ERROR] task cleanup aborted after 3 retries

- 出现 1 次 | 行 38~38 | 首末时间 2024-03-15 08:05:21 ~ 2024-03-15 08:05:21 | 模块 scheduler
- 智能分析：疑似连锁衍生错误（被动失败特征）；异常：罕见异常

**前上下文**
```
2024-03-15 08:05:15.310 ERROR [api] request 10854 failed: upstream unavailable
2024-03-15 08:05:15.450 ERROR [api] request 10855 failed: upstream unavailable
2024-03-15 08:05:16.020 ERROR [api] request 10861 failed: upstream unavailable
2024-03-15 08:05:16.090 ERROR [api] request 10862 failed: upstream unavailable
2024-03-15 08:05:20.700 ERROR [api] request 10901 failed: upstream unavailable
```

**典型样例**
```
2024-03-15 08:05:21.000 ERROR [scheduler] task cleanup aborted after 3 retries
```

**后上下文**
```
2024-03-15 08:06:00.000 ERROR [auth] session token validation failed: token expired
2024-03-15 08:06:30.250 ERROR [cache] redis protocol error: invalid bulk length
Traceback (most recent call last):
  File "/opt/acme/cache_client.py", line 88, in get
    value = self._read_bulk(sock)
```

### 6. [P2][ERROR] redis protocol error: invalid bulk length

- 出现 1 次 | 行 40~46 | 首末时间 2024-03-15 08:06:30 ~ 2024-03-15 08:06:30 | 模块 cache
- 智能分析：异常：罕见异常

**前上下文**
```
2024-03-15 08:05:16.020 ERROR [api] request 10861 failed: upstream unavailable
2024-03-15 08:05:16.090 ERROR [api] request 10862 failed: upstream unavailable
2024-03-15 08:05:20.700 ERROR [api] request 10901 failed: upstream unavailable
2024-03-15 08:05:21.000 ERROR [scheduler] task cleanup aborted after 3 retries
2024-03-15 08:06:00.000 ERROR [auth] session token validation failed: token expired
```

**典型样例**
```
2024-03-15 08:06:30.250 ERROR [cache] redis protocol error: invalid bulk length
```

**堆栈（已降噪：业务帧 5 行，折叠系统/第三方帧 1 行）**
```
Traceback (most recent call last):
  File "/opt/acme/cache_client.py", line 88, in get
    value = self._read_bulk(sock)
    ...... 已折叠 1 行系统库/第三方栈帧 ......
    response = self._parser.read_response()
ValueError: invalid bulk length: 18446744073709551615
```

**后上下文**
```
2024-03-15 08:07:45.000 FATAL [core] out of memory: worker-3 aborted, allocation of 1073741824 bytes failed
2024-03-15 08:07:45.100 ERROR [core] worker-3 terminated unexpectedly (exit code 137)
2024-03-15 08:07:45.200 INFO [supervisor] restarting worker-3
2024-03-15 08:10:02.000 ERROR [api] rare one-time glitch in request 5123521 serialization
2024-03-15 08:12:00.000 WARN [pool] connection pool recovered
```

### 7. [P2][ERROR] worker-3 terminated unexpectedly (exit code 137)

- 出现 1 次 | 行 48~48 | 首末时间 2024-03-15 08:07:45 ~ 2024-03-15 08:07:45 | 模块 core
- 智能分析：异常：罕见异常

**前上下文**
```
    value = self._read_bulk(sock)
  File "/usr/lib/python3.9/site-packages/redis/connection.py", line 221, in read_response
    response = self._parser.read_response()
ValueError: invalid bulk length: 18446744073709551615
2024-03-15 08:07:45.000 FATAL [core] out of memory: worker-3 aborted, allocation of 1073741824 bytes failed
```

**典型样例**
```
2024-03-15 08:07:45.100 ERROR [core] worker-3 terminated unexpectedly (exit code 137)
```

**后上下文**
```
2024-03-15 08:07:45.200 INFO [supervisor] restarting worker-3
2024-03-15 08:10:02.000 ERROR [api] rare one-time glitch in request 5123521 serialization
2024-03-15 08:12:00.000 WARN [pool] connection pool recovered
2024-03-15 08:12:05.000 INFO [api] service healthy again
```

### 8. [P2][ERROR] rare one-time glitch in request 5123521 serialization

- 出现 1 次 | 行 50~50 | 首末时间 2024-03-15 08:10:02 ~ 2024-03-15 08:10:02 | 模块 api
- 智能分析：异常：罕见异常

**前上下文**
```
    response = self._parser.read_response()
ValueError: invalid bulk length: 18446744073709551615
2024-03-15 08:07:45.000 FATAL [core] out of memory: worker-3 aborted, allocation of 1073741824 bytes failed
2024-03-15 08:07:45.100 ERROR [core] worker-3 terminated unexpectedly (exit code 137)
2024-03-15 08:07:45.200 INFO [supervisor] restarting worker-3
```

**典型样例**
```
2024-03-15 08:10:02.000 ERROR [api] rare one-time glitch in request 5123521 serialization
```

**后上下文**
```
2024-03-15 08:12:00.000 WARN [pool] connection pool recovered
2024-03-15 08:12:05.000 INFO [api] service healthy again
```

