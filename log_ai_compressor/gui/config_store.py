# -*- coding: utf-8 -*-
"""GUI 配置持久化：用户目录 JSON 存储，自动保存 / 恢复常用参数。

设计说明：
- 配置文件位于 ~/.log_ai_compressor/config.json，避免污染仓库；
- 深度合并默认值：新增配置项后老配置文件仍可正常加载；
- 保存动作自动创建目录，任何 IO 异常静默降级（配置失败不阻断主流程）。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Optional

from log_ai_compressor.constants import CONFIG_FILE

DEFAULT_CONFIG: Dict[str, Any] = {
    # 修复缺陷R10：默认级别含 FATAL（FATAL 受复选框控制）
    "levels": ["FATAL", "ERROR", "FAIL"],
    "include": [],
    "exclude": [],
    "top_n": 20,
    # 修复缺陷#5：默认上下文行数 5 -> 50（详情可看内容太少）
    "context_lines": 50,
    # 修复缺陷R10：字体大小档位（小/中/大/特大，持久化恢复）
    "font_size": "中",
    "rule": "generic",
    "appearance": "dark",
    "window": {"width": 1280, "height": 840},
    "last_files": [],
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并（override 优先，dict 递归合并）。"""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if (key in merged and isinstance(merged[key], dict)
                and isinstance(value, dict)):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class ConfigStore:
    """配置存取器（JSON 文件）。"""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else CONFIG_FILE

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Dict[str, Any]:
        """读取配置（不存在或损坏时返回默认值）。"""
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return _merge(DEFAULT_CONFIG, data)
        except (OSError, ValueError):
            pass
        return copy.deepcopy(DEFAULT_CONFIG)

    def save(self, config: Dict[str, Any]) -> bool:
        """保存配置（自动建目录；失败静默返回 False）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            merged = _merge(DEFAULT_CONFIG, config)
            self._path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return True
        except OSError:
            return False

    def reset(self) -> bool:
        """恢复默认配置。"""
        return self.save(copy.deepcopy(DEFAULT_CONFIG))
