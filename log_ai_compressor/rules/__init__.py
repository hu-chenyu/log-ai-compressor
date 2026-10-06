# -*- coding: utf-8 -*-
"""可插拔解析规则引擎：YAML 配置驱动，解析规则与核心逻辑解耦。"""

from log_ai_compressor.rules.engine import (
    RuleSet,
    list_presets,
    load_ruleset,
)

__all__ = ["RuleSet", "load_ruleset", "list_presets"]
