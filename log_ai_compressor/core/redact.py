# -*- coding: utf-8 -*-
"""敏感信息脱敏：投喂外部大模型前的出站打码（纯本地规则，离线不破）。

优化缺陷R86：分析结果在本地保持原样（便于排查），仅在「导出报告 /
复制摘要」出站时调用 redact_text 打码。打码模式（保守，宁漏勿滥，
避免误伤业务可读性）：
- Bearer/Basic 凭据 -> [密钥]（最优先，防 Authorization 头被键值
  规则吞掉 Bearer 后 token 残留）；
- 密钥键值对（token/api_key/secret/password/authorization/access_key）
  -> [密钥]（可能内含邮箱/IP）；
- URL 账号段 scheme://user[:pass]@ -> scheme://[账号]@；
- 邮箱 -> [邮箱]；手机号（11 位 1[3-9] 开头，\\b 边界防 13 位
  epoch 毫秒误伤）-> [电话]；IPv4（可带端口）-> [IP]。
主机名（如 gerrit.imv.local）无法与正常文本可靠区分，不打码
（误伤代价大于收益，由用户在导出前自行斟酌）。
"""
from __future__ import annotations

import re

_MASK_SECRET_KV = re.compile(
    r"(?i)\b(token|api[_-]?key|secret|password|passwd|authorization"
    r"|access[_-]?key)\s*[:=]\s*['\"]?[^\s,'\"]+")
_MASK_BEARER = re.compile(
    r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_MASK_URL_CRED = re.compile(
    r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@")
_MASK_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_MASK_PHONE = re.compile(r"\b1[3-9]\d{9}\b")
_MASK_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")


def redact_text(text: str, custom_rules: list = None) -> str:
    """出站打码（顺序敏感：Bearer > 密钥键值 > URL 账号 > 邮箱 > 电话 > IP）。

    Bearer 必须先于密钥键值：「Authorization: Bearer xxx」若先走键值
    规则会把 Bearer 当作值吞掉（Authorization=[密钥]），真正的 token
    反而残留（自测发现的顺序缺陷）。

    优化缺陷R103：用户自定义正则追加到最后一道防线，每行一条
    （非法正则已在界面层拦截）。命中统一替换为 [自定义]。
    """
    text = _MASK_BEARER.sub(r"\1 [密钥]", text)
    text = _MASK_SECRET_KV.sub(lambda m: m.group(1) + "=[密钥]", text)
    text = _MASK_URL_CRED.sub(r"\1[账号]@", text)
    text = _MASK_EMAIL.sub("[邮箱]", text)
    text = _MASK_PHONE.sub("[电话]", text)
    text = _MASK_IPV4.sub("[IP]", text)
    for rule in (custom_rules or []):
        if rule:
            try:
                text = re.sub(rule, "[自定义]", text)
            except re.error:
                pass
    return text
