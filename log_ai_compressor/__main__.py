# -*- coding: utf-8 -*-
"""支持 `python -m log_ai_compressor` 方式启动。

**必须把 main() 的返回值交给 sys.exit**（v2.8.0 修）。

此前这里直接 `main()`，返回值被丢弃，于是 `python -m log_ai_compressor
run 不存在的.log` 会打印错误却**退出码 0** —— 而控制台入口
`log-ai-compressor run 不存在的.log` 退出码是 1。同一个失败，两个退出码。

为什么这不是小事：本工具的 `verify` 子命令，退出码**就是它的契约本身**
（0 = VERIFIED，1 = TAMPERED / 调用失败）。任何用 `python -m` 写的 CI 脚本、
shell 管道、pre-commit 钩子，都会把失败当成成功通过。

这类问题的形状值得记住：**两条入口路径共用同一个 main()，但其中一条忘了
接返回值**。模块代码「能跑」不等于「契约成立」——契约住在进程退出码上，
而退出码只有显式 `sys.exit` 才会被设置。
"""
import sys

from log_ai_compressor.cli import main

if __name__ == "__main__":
    sys.exit(main())
