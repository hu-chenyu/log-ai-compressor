# -*- coding: utf-8 -*-
"""敏感信息脱敏单元测试（优化缺陷R86）：出站打码规则与误伤防护。"""
from __future__ import annotations

from log_ai_compressor.core.redact import redact_text


class TestSecretMasking:
    def test_key_value_pairs(self):
        assert redact_text("api_key=abc123XYZ") == "api_key=[密钥]"
        out = redact_text("password: 'hunter2'")
        assert "hunter2" not in out
        assert out.startswith("password=[密钥]")  # 尾部引号原样保留
        assert "tok99" not in redact_text("token=tok99 rest")

    def test_bearer_and_basic(self):
        out = redact_text("Authorization: Bearer abcdefgh12345678")
        assert "abcdefgh12345678" not in out
        assert "[密钥]" in out

    def test_url_credential(self):
        out = redact_text("dial mysql://root:s3cret@db.internal:3306/x")
        assert out == "dial mysql://[账号]@db.internal:3306/x"

    def test_email_phone_ip(self):
        out = redact_text("admin@corp.com 13812345678 10.0.0.1:8080")
        assert out == "[邮箱] [电话] [IP]"

    def test_order_secret_before_email(self):
        # 密钥值内含邮箱时优先按密钥打码（不被邮箱规则截断）
        out = redact_text("password=admin@corp.com")
        assert out == "password=[密钥]"


class TestFalsePositiveGuard:
    def test_epoch_millis_not_phone(self):
        # 13 位 epoch 毫秒不得误判为手机号
        text = "ts=1715000000000 ERROR boom"
        assert redact_text(text) == text

    def test_plain_text_unchanged(self):
        text = ("2024-01-01 09:00:05 ERROR [db] connection refused "
                "to db-primary:5432")
        # db-primary:5432 非 IPv4，主机名不打码（宁漏勿滥）
        assert redact_text(text) == text

    def test_version_number_not_ip(self):
        assert redact_text("version 1.2.3 released") == \
            "version 1.2.3 released"


# ---------------------------------------------------------------------------
# 优化缺陷R103：用户自定义脱敏规则
# ---------------------------------------------------------------------------
class TestCustomRules:
    def test_custom_rule_masks_hit(self):
        """自定义正则命中 → [自定义]（内部工单号场景）。"""
        text = "工单 PMS-102938 处理失败，请重试"
        out = redact_text(text, custom_rules=[r"PMS-\d{6}"])
        assert out == "工单 [自定义] 处理失败，请重试"

    def test_custom_rules_multiple_and_blank(self):
        """多条规则按序生效；空串跳过不报错。"""
        text = "host app.corp 连接 foo.internal 失败"
        out = redact_text(text,
                          custom_rules=[r"[\w.-]+\.corp", r"[\w.-]+\.internal",
                                        ""])
        assert out == "host [自定义] 连接 [自定义] 失败"

    def test_invalid_custom_rule_ignored(self):
        """非法正则静默跳过（界面层已计数提示），内置规则仍生效。"""
        text = "mail a@b.com from PMS-1"
        out = redact_text(text, custom_rules=["[unclosed"])
        assert out == "mail [邮箱] from PMS-1"

    def test_none_custom_rules_compatible(self):
        """不传自定义规则时行为与旧版一致。"""
        text = "ip 10.0.0.1 down"
        assert redact_text(text) == redact_text(text, custom_rules=None) \
            == "ip [IP] down"

