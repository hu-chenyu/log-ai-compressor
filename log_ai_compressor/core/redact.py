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
主机名（如 build-01.corp.example）无法与正常文本可靠区分，不打码
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
# 版本号语境：紧跟在 version/ver/release/版本 之后的四段数字不是 IP
# （实测 "version 1.2.3.4 released" 被整段打成 [IP]，把最关键的排查
# 线索从报告里抹掉了 —— 与本模块「宁漏勿滥、避免误伤可读性」的立场
# 相悖）。这些词后面的点分数字直接跳过。
# 词表刻意不含 build：日志里 "build 10.1.2.3" 不如 "from 10.0.0.1"
# 常见，把它算成版本号就等于放过了一个真实 IP —— 宁可误伤也不漏打。
_VERSION_CTX = re.compile(
    r"(?:version|ver|release|版本号?)\s*[:=]?\s*$", re.IGNORECASE)


def _mask_ipv4(text: str) -> str:
    """IPv4 打码，跳过版本号语境。"""
    out: list = []
    last = 0
    for m in _MASK_IPV4.finditer(text):
        if _VERSION_CTX.search(text[last:m.start()]):
            continue                       # 版本号，原样保留
        out.append(text[last:m.start()])
        out.append("[IP]")
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def compile_custom_rules(rules) -> list:
    """把自定义规则全部**先编译一遍**，非法的一条当场报错。

    修复缺陷（v2.11.2）：原先是在 `re.sub` 上 try/except `re.error` 然后
    `pass` —— 一条写错的自定义规则**静默地什么也不做**。实测：

        合法规则  account CUST-998877 locked  ->  account [自定义] locked
        非法规则  account CUST-998877 locked  ->  account CUST-998877 locked

    用户以为自己加了一条打码规则、报告已经脱敏，实际敏感串**原样出去**，
    而屏幕上没有任何提示。对一个卖点里写着「出站打码」的工具，
    **静默地不打码比报错严重得多**：报错只损失一次操作，
    静默则把秘密送出去且不自知。

    原注释里写着「非法正则已在界面层拦截」——**那句话是假的**：Web 前端恒传
    `custom_rules: []`（app.js），界面层压根没有自定义规则的入口，也就从没
    拦过任何东西。**不要把校验寄托在调用方身上** ——「我以为别处已经校验过」
    是这类校验最常见的消失方式。

    抛 `ValueError` 而不是自定义异常：core 层不该反向依赖 service，
    由 `service.export_text` 把它翻成 `ServiceError`（Web 层已接成 400）。
    """
    compiled = []
    for i, rule in enumerate(rules or []):
        if not rule:
            continue
        try:
            compiled.append(re.compile(rule))
        except re.error as exc:
            raise ValueError(
                f"第 {i + 1} 条自定义打码规则不是合法正则，已**拒绝导出**而不是"
                f"悄悄跳过（跳过的后果是该规则本该遮住的内容原样出现在报告里）："
                f"{rule!r} —— {exc}") from exc
    return compiled


def redact_text(text: str, custom_rules: list = None) -> str:
    """出站打码（顺序敏感：Bearer > 密钥键值 > URL 账号 > 邮箱 > 电话 > IP）。

    Bearer 必须先于密钥键值：「Authorization: Bearer xxx」若先走键值
    规则会把 Bearer 当作值吞掉（Authorization=[密钥]），真正的 token
    反而残留（自测发现的顺序缺陷）。

    用户自定义正则追加到最后一道防线。**非法正则一律抛错**，理由见
    `compile_custom_rules`。
    """
    text = _MASK_BEARER.sub(r"\1 [密钥]", text)
    text = _MASK_SECRET_KV.sub(lambda m: m.group(1) + "=[密钥]", text)
    text = _MASK_URL_CRED.sub(r"\1[账号]@", text)
    text = _MASK_EMAIL.sub("[邮箱]", text)
    text = _MASK_PHONE.sub("[电话]", text)
    text = _mask_ipv4(text)
    for pattern in compile_custom_rules(custom_rules):
        text = pattern.sub("[自定义]", text)
    return text
