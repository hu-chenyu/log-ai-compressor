# -*- coding: utf-8 -*-
"""log-ai-compressor：全场景通用日志分析前置处理器。

核心定位：
1. 日志压缩投喂大模型 —— 将海量日志压缩为结构化错误报告，适配 LLM 上下文窗口；
2. 快速故障排查 —— 聚类去重、根因定位、优先级排序，辅助人工快速定位问题。

分层架构：
- log_ai_compressor.rules   可插拔解析规则引擎（YAML 配置驱动，与核心逻辑解耦）
- log_ai_compressor.core    核心处理层（解析/过滤/聚类/分析/管线/对比，与 UI 完全分离）
- log_ai_compressor.export  导出层（Markdown / JSON / 纯文本报告）
- log_ai_compressor.gui     GUI 层（CustomTkinter，仅负责交互与展示）
- log_ai_compressor.cli     命令行入口（脚本 / 流水线自动化调用）
"""

__version__ = "1.0.0"
__app_name__ = "log-ai-compressor"
