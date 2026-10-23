# -*- coding: utf-8 -*-
"""build_repro.py 的归一化范围必须只覆盖**本次构建**的产物。

背景：正式构建的 outdir 就是 dist/，那里堆着历次发布的产物。原来的判据是
``sorted(outdir.glob("*.tar.gz"))`` —— 抓的是目录里**所有** sdist，而不是这次
刚生成的那一个。于是每次发版都用「最后一次提交」的时间戳把历史产物全部重打一遍。

实测后果（2026-10-18 发 v2.11.1 时撞到）：本地 ``2.11.0`` 的 sdist 从
``1ff4b384…``（= PyPI 上的值，738804 B）变成 ``3cfef3bc…``（738800 B）。
README 里「本地 / PyPI / GitHub 三方 sha256 对账」这句话，对**任何已发布版本**
都当场失效 —— 而它当时全绿着。

这个缺陷的形状与本仓库记过多次的「什么都没校却报通过」同族：脚本报告了
「构建成功 / 可复现」，但它顺手改掉的东西没有任何一处会告诉你。
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import os
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 脚本不在包内（scripts/ 无 __init__.py），按路径加载
_SPEC = importlib.util.spec_from_file_location(
    "build_repro_under_test", ROOT / "scripts" / "build_repro.py")
assert _SPEC and _SPEC.loader
br = importlib.util.module_from_spec(_SPEC)
sys.modules["build_repro_under_test"] = br
_SPEC.loader.exec_module(br)


# 一笔固定的「最后提交时间」。测试不依赖真实仓库状态。
EPOCH = 1792257170

# 已发布版本的成员时间戳。必须与 EPOCH 不同 —— 否则归一化是恒等变换，
# 「字节有没有被重写」这条断言就永远为真，等于没有断言。
PUBLISHED_MTIME = EPOCH - 86400


def _write_sdist(path: Path, member_mtime: int) -> None:
    """造一个**合法**的 .tar.gz，且它的字节会被 normalize_sdist 重写。

    三个必须与归一化目标不同的字段：成员 mtime、uid/gid、uname/gname。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        payload = b"name = log-ai-compressor\n"
        info = tarfile.TarInfo("log_ai_compressor-2.11.2/PKG-INFO")
        info.size = len(payload)
        info.mtime = member_mtime
        info.uid = info.gid = 1000
        info.uname = info.gname = "builder"
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(payload))
    with path.open("wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(buf.getvalue())


def _install_fake_build(monkeypatch, outdir: Path, produced: Path,
                        member_mtime: int, touch: bool = True) -> None:
    """把 ``python -m build`` 换成一个假构建。

    ``touch=False`` 表示假构建不碰产物文件 —— 用来观察调用方**自己**判定
    出来的归一化范围，而不是被构建步骤的副作用带出来的。

    2026-10-23 起 ``build()`` 在**干净快照**里构建（未入库/被忽略的文件
    不该进产物，见 ``build_repro.py`` 的模块文档）。快照这一步单独 stub 掉：
    否则它会真的去调 ``git archive``，而本测试刻意**不依赖真实仓库状态**。
    """
    def _fake_run(cmd, **kwargs):  # noqa: ARG001 - 签名对齐 subprocess.run
        if touch:
            _write_sdist(produced, member_mtime)
        return None

    def _fake_snapshot(dest: Path, ref: str = "HEAD") -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    monkeypatch.setattr(br.subprocess, "run", _fake_run)
    monkeypatch.setattr(br, "snapshot_into", _fake_snapshot)
    # last_commit_epoch 现在带 ref 参数（构建哪一笔提交）
    monkeypatch.setattr(br, "last_commit_epoch", lambda ref="HEAD": EPOCH)


@pytest.fixture()
def dist_with_a_published_artifact(tmp_path):
    """outdir 里先放一份「已发布版本」的 sdist，mtime 拨到一天前。"""
    outdir = tmp_path / "dist"
    outdir.mkdir()
    old = outdir / "log_ai_compressor-9.9.9.tar.gz"
    _write_sdist(old, PUBLISHED_MTIME)
    yesterday = time.time() - 86400
    os.utime(old, (yesterday, yesterday))
    return outdir, old, old.read_bytes()


def test_published_artifacts_are_not_rewritten(dist_with_a_published_artifact,
                                               monkeypatch):
    """本次构建**不得**动 dist/ 里已发布版本的字节。

    这条红了就说明有人把判据改回了「归一化目录里所有 .tar.gz」。
    """
    outdir, old, before = dist_with_a_published_artifact
    _install_fake_build(
        monkeypatch, outdir, outdir / "log_ai_compressor-2.11.2.tar.gz", EPOCH + 7)

    made = br.build(outdir)

    assert [p.name for p in made] == ["log_ai_compressor-2.11.2.tar.gz"], (
        "只应归一化本次构建的产物，却把历史产物也算进去了")
    assert old.read_bytes() == before, (
        "已发布版本的字节被本次构建重写了 —— README 的三方 sha256 对账对已发布版本失效")


def test_the_builds_own_artifact_is_still_normalized(dist_with_a_published_artifact,
                                                    monkeypatch):
    """本次构建自己的产物必须**被**归一化。

    与上一条成对：只留「新增文件」的实现（按文件名判新旧）会让这条变红 ——
    重跑同一个版本时文件已经存在，可复现性就被悄悄弄丢了。
    """
    outdir, _old, _before = dist_with_a_published_artifact
    produced = outdir / "log_ai_compressor-2.11.2.tar.gz"
    _write_sdist(produced, EPOCH + 7)
    five_seconds_ago = time.time() - 5
    os.utime(produced, (five_seconds_ago, five_seconds_ago))
    unnormalized = produced.read_bytes()

    # 假构建会把同名文件重写一遍（mtime 随之变新），这正是「重跑」的情形
    _install_fake_build(monkeypatch, outdir, produced, EPOCH + 7)

    made = br.build(outdir)

    assert [p.name for p in made] == ["log_ai_compressor-2.11.2.tar.gz"]
    assert produced.read_bytes() != unnormalized, (
        "本次构建自己的产物没有被归一化 —— 可复现性这条承诺已经落空")


def test_a_file_stamped_just_before_the_sample_is_still_normalized(tmp_path,
                                                                   monkeypatch):
    """产物 mtime 落在取样时刻之前一点点时，仍然必须被归一化。

    这不是假设出来的场景，Windows 上实测会咬人：文件时间戳按 ~15.625ms 的
    时钟 tick 向下取整，而 ``started`` 取的是**浮点**秒 —— 同一次调用里写出的
    文件，其 mtime 可能比那个浮点值略小，于是「本次构建的产物」被判成
    「不是这次写的」而漏掉。可复现性就在自己脚下失效。

    ``int()`` 之后判据变成「同一秒内都算」，这 10 毫秒的容差就有了。

    把 ``time.time`` 钉死，避免秒进位带来的偶发通过 —— 那样这条测试就会
    变成「有时红有时绿」的 flaky，而 flaky 的测试等于没有测试。
    """
    outdir = tmp_path / "dist"
    outdir.mkdir()
    produced = outdir / "log_ai_compressor-2.11.2.tar.gz"
    _write_sdist(produced, EPOCH + 7)
    unnormalized = produced.read_bytes()

    # 采样时刻带 0.75 秒小数；文件 mtime 比它早 10 毫秒（同秒内）
    sampled_at = 1_700_000_000.75
    os.utime(produced, (sampled_at - 0.01, sampled_at - 0.01))
    _install_fake_build(monkeypatch, outdir, produced, EPOCH + 7, touch=False)
    monkeypatch.setattr(
        br, "time", SimpleNamespace(time=lambda: sampled_at,
                                    monotonic=time.monotonic))

    made = br.build(outdir)

    assert [p.name for p in made] == ["log_ai_compressor-2.11.2.tar.gz"], (
        "本次构建自己的产物被漏掉了（started 用了浮点秒，容不下 mtime 的 tick 取整）")
    assert produced.read_bytes() != unnormalized, (
        "整秒内的产物没有被归一化 —— started 没有取整")
