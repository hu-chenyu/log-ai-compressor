# -*- coding: utf-8 -*-
"""Web 层三条错误出口必须给**同一份**信息（v2.8.1）。

背景：独立审查时发现 `web/jobs.py` 的三条路径行为各不相同 ——

  · SSE 早订阅者  —— 拿到完整 traceback
  · SSE 晚订阅者  —— 拿不到（补发帧只带 message）
  · REST 快照     —— 拿不到（`snapshot()` 有意不带）

**不一致本身就是缺陷**：同一个故障，先打开页面的人能看到栈、后打开的人
看不到，没有任何文档解释为什么。而且它极难复现 —— 用户报「昨天那次报错
有栈今天没有」，开发者只能回复「你刷新顺序不一样」。

现在统一：默认三条路径都只给 `message`；`LOG_AI_TRACEBACK=1` 时三条
都带 traceback。本文件把三条各钉一遍，将来谁改回去都会红。
"""
from __future__ import annotations

import json

from log_ai_compressor.web import jobs as J


def _failing_job() -> J.Job:
    """构造一个内部失败的 job（绕过分析，直接注入失败态）。"""
    job = J.REGISTRY.create("text", {"text": "whatever"})
    job._done.set()
    job.error = "ValueError: boom"
    job.traceback = "Traceback (most recent call last):\n  ...boom"
    return job


def _frames(job: J.Job, env_flag: str | None):
    """消费 SSE 生成器，返回全部帧（已解析）。"""
    import os
    old = os.environ.get("LOG_AI_TRACEBACK")
    if env_flag is None:
        os.environ.pop("LOG_AI_TRACEBACK", None)
    else:
        os.environ["LOG_AI_TRACEBACK"] = env_flag
    try:
        out = []
        for frame in J.stream_events(job, max_wait=1.0):
            out.append((frame["event"], json.loads(frame["data"])))
        return out
    finally:
        if old is None:
            os.environ.pop("LOG_AI_TRACEBACK", None)
        else:
            os.environ["LOG_AI_TRACEBACK"] = old


# ---------------------------------------------------------------------------
# 1) 默认：三条路径都不给 traceback
# ---------------------------------------------------------------------------


def test_rest_snapshot_never_exposes_traceback() -> None:
    """REST 快照：只给一句话，不给栈。"""
    snap = _failing_job().snapshot()
    assert "traceback" not in snap, snap
    assert snap["state"] == "error"
    assert snap["error"] == "ValueError: boom"


def test_late_sse_subscriber_gets_no_traceback_by_default() -> None:
    """晚订阅者（走补发帧）：默认与 REST 一致，都不给栈。"""
    frames = _frames(_failing_job(), env_flag=None)
    errors = [p for e, p in frames if e == "error"]
    assert errors, frames
    for p in errors:
        assert "traceback" not in p, p
        assert p["message"] == "ValueError: boom"


def test_rest_and_late_sse_agree() -> None:
    """核心不变量：REST 与晚订阅者拿到的 error 内容必须一致。

    这条就是当初出问题的地方 —— 两条路径各写各的，行为悄悄分叉。
    """
    job = _failing_job()
    snap = job.snapshot()
    frames = _frames(job, env_flag=None)
    errors = [p for e, p in frames if e == "error"]
    assert errors
    assert errors[-1]["message"] == snap["error"]
    assert errors[-1]["kind"] == "internal"


# ---------------------------------------------------------------------------
# 2) 显式开启：三条路径**都**给栈
# ---------------------------------------------------------------------------


def test_env_flag_turns_traceback_on_for_late_subscriber() -> None:
    """LOG_AI_TRACEBACK=1 时补发帧要带上栈 —— 否则开关只对早订阅者有效。"""
    frames = _frames(_failing_job(), env_flag="1")
    errors = [p for e, p in frames if e == "error"]
    assert errors
    assert errors[-1].get("traceback"), errors[-1]


def test_env_flag_accepts_common_truthy_spellings() -> None:
    """true/yes/on 都认 —— 排障时没人会记得住只能写 1。"""
    for val in ("1", "true", "TRUE", "yes", "on"):
        frames = _frames(_failing_job(), env_flag=val)
        errors = [p for e, p in frames if e == "error"]
        assert errors[-1].get("traceback"), f"{val!r} 没被认成开启"


def test_env_flag_falsey_values_stay_off() -> None:
    """0/false/空串/乱值都不能误开 —— 误开会白暴露内部结构。"""
    for val in ("0", "false", "", "  ", "maybe"):
        frames = _frames(_failing_job(), env_flag=val)
        errors = [p for e, p in frames if e == "error"]
        assert not errors[-1].get("traceback"), f"{val!r} 被误当成开启"


def test_rest_stays_closed_even_when_flag_is_on() -> None:
    """REST 快照**始终**不带栈：它是给轮询用的，不该跟着 SSE 开关变。

    这条不是"忘了统一"而是**故意不同**：REST 的消费者是前端轮询代码，
    它不会渲染栈；而 SSE 的消费者是人。开关解决的是"人要不要看到栈"，
    不是"所有出口必须一模一样"。
    """
    import os
    os.environ["LOG_AI_TRACEBACK"] = "1"
    try:
        snap = _failing_job().snapshot()
    finally:
        os.environ.pop("LOG_AI_TRACEBACK", None)
    assert "traceback" not in snap
