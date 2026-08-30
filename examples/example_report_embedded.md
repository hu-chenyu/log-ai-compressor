# 日志AI压缩报告：examples\sample_embedded.log

> log-ai-compressor v1.0.0 | 处理 21 行 | 耗时 0.00s | 3.7 万行/秒 | 规则 embedded

**初步定位根因**：pll lock timeout after 50 ms (code -110)

## 一、概览统计

| 指标 | 数值 |
| --- | --- |
| 日志来源 | examples\sample_embedded.log |
| 编码 | utf-8 |
| 总行数 | 21 |
| 错误行数（FATAL/ERROR/FAIL） | 7 |
| 错误种类数（去重后） | 6 |
| 错误总次数（过滤后） | 7 |
| 日志时间范围 | 0.000s ~ 0.140s |

级别分布：ERROR=4, FAIL=3, INFO=9, WARN=1

## 二、Top 6 错误清单（按优先级排序）

| # | 优先级 | 级别 | 次数 | 模块 | 根因 | 异常 | 错误摘要 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | P0 | ERROR | 2 | im_pll | ✔ 时间连锁源头（窗口内首发且含根因 | — | pll lock timeout after 50 ms (code -110) |
| 2 | P2 | ERROR | 1 | im_clk_a400 | — | — | a400 clock rate mismatch: expected 400 MHz |
| 3 | P2 | ERROR | 1 | im_pll.c | — | — | reference clock signal lost |
| 4 | P2 | FAIL | 1 | ut | — | — | tc_im_pll_relock ............ FAIL |
| 5 | P2 | FAIL | 1 | ut | — | — | assertion failed at im_clk_a400.c:253: expected 400000000, got 0 |
| 6 | P2 | FAIL | 1 | ut | — | — | suite summary: 2 tests failed of 5 |

## 三、典型样例详情（每错误一份，含上下文与降噪堆栈）

### 1. [P0][ERROR] pll lock timeout after 50 ms (code -110)

- 出现 2 次 | 行 9~10 | 首末时间 0.118s ~ 0.119s | 模块 im_pll
- 智能分析：根因：时间连锁源头（窗口内首发且含根因特征）

**前上下文**
```
[    0.020] [INFO] [im_gate] clock gate enabled for domain A400
[    0.031] [INFO] [im_clk_a400] a400 domain ready
[    0.100] [INFO] [ut] running unit test suite: clock
[    0.105] [INFO] [ut] tc_im_pll_lock .............. PASS
[    0.110] [INFO] [ut] tc_im_pll_jitter ............. PASS
```

**典型样例**
```
[    0.118] [ERR ] [im_pll] pll lock timeout after 50 ms (code -110)
```

**后上下文**
```
[    0.119] [ERR ] [im_pll] pll lock timeout after 50 ms (code -110)
[    0.121] [FAIL] [ut] tc_im_pll_relock ............ FAIL
[    0.121] [FAIL] [ut] assertion failed at im_clk_a400.c:253: expected 400000000, got 0
[    0.122] [ERR ] [im_clk_a400] a400 clock rate mismatch: expected 400 MHz
	#0  0x08004a1b in im_clk_a400_enable (im_clk_a400.c:253)
```

### 2. [P2][ERROR] a400 clock rate mismatch: expected 400 MHz

- 出现 1 次 | 行 13~17 | 首末时间 0.122s ~ 0.122s | 模块 im_clk_a400

**前上下文**
```
[    0.110] [INFO] [ut] tc_im_pll_jitter ............. PASS
[    0.118] [ERR ] [im_pll] pll lock timeout after 50 ms (code -110)
[    0.119] [ERR ] [im_pll] pll lock timeout after 50 ms (code -110)
[    0.121] [FAIL] [ut] tc_im_pll_relock ............ FAIL
[    0.121] [FAIL] [ut] assertion failed at im_clk_a400.c:253: expected 400000000, got 0
```

**典型样例**
```
[    0.122] [ERR ] [im_clk_a400] a400 clock rate mismatch: expected 400 MHz
```

**堆栈（已降噪：业务帧 4 行，折叠系统/第三方帧 0 行）**
```
	#0  0x08004a1b in im_clk_a400_enable (im_clk_a400.c:253)
	#1  0x08003f2c in im_clk_a400_init (im_clk_a400.c:88)
	#2  0x08002d1e in clock_subsys_init (im_clock.c:120)
	#3  0x080019f0 in main (main.c:42)
```

**后上下文**
```
[    0.130] [FAIL] [ut] suite summary: 2 tests failed of 5
im_pll.c:118: error: reference clock signal lost
im_gate.c:77: warning: gate status readback mismatch
[    0.140] [INFO] [ut] test suite done
```

### 3. [P2][ERROR] reference clock signal lost

- 出现 1 次 | 行 19~19 | 模块 im_pll.c

**前上下文**
```
	#0  0x08004a1b in im_clk_a400_enable (im_clk_a400.c:253)
	#1  0x08003f2c in im_clk_a400_init (im_clk_a400.c:88)
	#2  0x08002d1e in clock_subsys_init (im_clock.c:120)
	#3  0x080019f0 in main (main.c:42)
[    0.130] [FAIL] [ut] suite summary: 2 tests failed of 5
```

**典型样例**
```
im_pll.c:118: error: reference clock signal lost
```

**后上下文**
```
im_gate.c:77: warning: gate status readback mismatch
[    0.140] [INFO] [ut] test suite done
```

### 4. [P2][FAIL] tc_im_pll_relock ............ FAIL

- 出现 1 次 | 行 11~11 | 首末时间 0.121s ~ 0.121s | 模块 ut

**前上下文**
```
[    0.100] [INFO] [ut] running unit test suite: clock
[    0.105] [INFO] [ut] tc_im_pll_lock .............. PASS
[    0.110] [INFO] [ut] tc_im_pll_jitter ............. PASS
[    0.118] [ERR ] [im_pll] pll lock timeout after 50 ms (code -110)
[    0.119] [ERR ] [im_pll] pll lock timeout after 50 ms (code -110)
```

**典型样例**
```
[    0.121] [FAIL] [ut] tc_im_pll_relock ............ FAIL
```

**后上下文**
```
[    0.121] [FAIL] [ut] assertion failed at im_clk_a400.c:253: expected 400000000, got 0
[    0.122] [ERR ] [im_clk_a400] a400 clock rate mismatch: expected 400 MHz
	#0  0x08004a1b in im_clk_a400_enable (im_clk_a400.c:253)
	#1  0x08003f2c in im_clk_a400_init (im_clk_a400.c:88)
	#2  0x08002d1e in clock_subsys_init (im_clock.c:120)
```

### 5. [P2][FAIL] assertion failed at im_clk_a400.c:253: expected 400000000, got 0

- 出现 1 次 | 行 12~12 | 首末时间 0.121s ~ 0.121s | 模块 ut

**前上下文**
```
[    0.105] [INFO] [ut] tc_im_pll_lock .............. PASS
[    0.110] [INFO] [ut] tc_im_pll_jitter ............. PASS
[    0.118] [ERR ] [im_pll] pll lock timeout after 50 ms (code -110)
[    0.119] [ERR ] [im_pll] pll lock timeout after 50 ms (code -110)
[    0.121] [FAIL] [ut] tc_im_pll_relock ............ FAIL
```

**典型样例**
```
[    0.121] [FAIL] [ut] assertion failed at im_clk_a400.c:253: expected 400000000, got 0
```

**后上下文**
```
[    0.122] [ERR ] [im_clk_a400] a400 clock rate mismatch: expected 400 MHz
	#0  0x08004a1b in im_clk_a400_enable (im_clk_a400.c:253)
	#1  0x08003f2c in im_clk_a400_init (im_clk_a400.c:88)
	#2  0x08002d1e in clock_subsys_init (im_clock.c:120)
	#3  0x080019f0 in main (main.c:42)
```

### 6. [P2][FAIL] suite summary: 2 tests failed of 5

- 出现 1 次 | 行 18~18 | 首末时间 0.130s ~ 0.130s | 模块 ut

**前上下文**
```
[    0.122] [ERR ] [im_clk_a400] a400 clock rate mismatch: expected 400 MHz
	#0  0x08004a1b in im_clk_a400_enable (im_clk_a400.c:253)
	#1  0x08003f2c in im_clk_a400_init (im_clk_a400.c:88)
	#2  0x08002d1e in clock_subsys_init (im_clock.c:120)
	#3  0x080019f0 in main (main.c:42)
```

**典型样例**
```
[    0.130] [FAIL] [ut] suite summary: 2 tests failed of 5
```

**后上下文**
```
im_pll.c:118: error: reference clock signal lost
im_gate.c:77: warning: gate status readback mismatch
[    0.140] [INFO] [ut] test suite done
```

