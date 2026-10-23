# -*- coding: utf-8 -*-
"""第 11 道门禁（产物审计）必须能抓到「产物里混进了不该有的东西」。

为什么这道门禁要单独有测试
--------------------------
`scripts/audit_release_artifact.py` 是 CI 的第十一道门禁，但在此之前
**一个测试都没有** —— 也就是说它「有没有牙齿」从来没被验证过。

而 2026-10-23 第十一轮外部复核正好撞上了它的盲区：已发布的 2.12.6 sdist 里
夹着三个**从未进过任何 commit** 的文件（还带着构建机的绝对路径与账户名），
而这道门禁在**那份产物上照样通过**。原因很直白：它当时只查两件事 ——
词表命中（200 条词里没有「个人路径」这一类）与产物自带 SAST 的自洽性。

所以这里补两件事：
  1. 门禁新增的两道检查（文件是否来自源码树 / 是否带构建机路径）有测试；
  2. **门禁本身**要被验证：造一个带野文件的产物，门禁必须报红。

**改坏会红吗**：把 `audit_release_artifact.py` 里「产物里的文件不在源码树里」
那段判断去掉（改成不收集 extra），本文件第 2 条立刻红；
把 `build_machine_markers()` 改成返回空列表，第 3 条立刻红。
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "audit_release_artifact.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_release_artifact", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_release_artifact"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_artifacts(dist: Path, sdist_members: dict, wheel_members: dict) -> None:
    """造一对最小产物：sdist 顶层目录名要跟真实产物一致。"""
    dist.mkdir(parents=True, exist_ok=True)
    top = "log_ai_compressor-2.12.6"
    with tarfile.open(dist / "log_ai_compressor-2.12.6.tar.gz", "w:gz") as t:
        for name, data in sdist_members.items():
            raw = data.encode("utf-8")
            info = tarfile.TarInfo("%s/%s" % (top, name))
            info.size = len(raw)
            t.addfile(info, io.BytesIO(raw))
    with zipfile.ZipFile(dist / "log_ai_compressor-2.12.6-py3-none-any.whl", "w") as z:
        for name, data in wheel_members.items():
            z.writestr(name, data.encode("utf-8"))


def _run_audit(dist: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--version", "2.12.6", "--dist", str(dist)],
        cwd=str(_ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=600)


class TestTheArtifactAuditHasTeeth:
    def test_a_clean_artifact_has_no_stray_files(self, tmp_path: Path):
        """对照组：产物里的文件都在源码树里 ⇒ 不许报「不在源码树里」。

        **改坏会红吗**：把「不在源码树里」的判断改成永远为空列表，
        本条**不会**红（所以它只是对照组）；真正守门的是下面两条。
        """
        dist = tmp_path / "dist"
        _make_artifacts(
            dist,
            {"README.md": "# readme\n", "log_ai_compressor/__init__.py": "x = 1\n"},
            {"log_ai_compressor/__init__.py": "x = 1\n"})
        proc = _run_audit(dist)
        assert "不在源码树里" not in proc.stdout, (
            "干净的产物被报成有野文件：%s" % proc.stdout[-800:])

    def test_a_file_that_is_not_in_the_source_tree_is_reported(self, tmp_path: Path):
        """产物里混进源码树没有的文件 ⇒ 必须报红并点名它。

        这正是已发布的 2.12.6 sdist 里的那三个文件
        （examples/app_report.md 等）。

        **改坏会红吗**：把 `audit_release_artifact.py` 里收集 extra 的那段
        去掉（extra 永远是空），本条立刻红。
        """
        dist = tmp_path / "dist"
        _make_artifacts(
            dist,
            {"README.md": "# readme\n",
             "examples/app_report.md": "# 报告\n路径：C:\\Users\\someone\\app.log\n"},
            {"log_ai_compressor/__init__.py": "x = 1\n"})
        proc = _run_audit(dist)
        assert proc.returncode == 1, (
            "混进野文件的产物居然通过了门禁（退出码 %d）：%s"
            % (proc.returncode, proc.stdout[-800:]))
        assert "不在源码树里" in proc.stdout, (
            "门禁没说出「不在源码树里」：%s" % proc.stdout[-800:])
        assert "examples/app_report.md" in proc.stdout, (
            "门禁没点名那个野文件：%s" % proc.stdout[-800:])

    def test_the_build_machine_path_in_an_artifact_is_reported(self, tmp_path: Path):
        """产物里出现构建机的路径 ⇒ 必须报红。

        **改坏会红吗**：把 `build_machine_markers()` 改成返回空列表，
        本条立刻红。
        """
        dist = tmp_path / "dist"
        _make_artifacts(
            dist,
            {"README.md": "# readme\n",
             "log_ai_compressor/notes.md": "日志在 %s\\examples\\app.log\n" % _ROOT},
            {"log_ai_compressor/__init__.py": "x = 1\n"})
        proc = _run_audit(dist)
        assert proc.returncode == 1, (
            "带构建机路径的产物居然通过了门禁（退出码 %d）：%s"
            % (proc.returncode, proc.stdout[-800:]))
        assert "构建机的路径" in proc.stdout, (
            "门禁没说出「构建机的路径」：%s" % proc.stdout[-800:])


class TestTheHelpers:
    def test_build_metadata_is_not_treated_as_a_stray_file(self):
        """构建工具自己生成的元数据不算「野文件」。

        **改坏会红吗**：把 `_BUILD_METADATA` 的正则删掉，本条立刻红。
        """
        mod = _load()
        for name in ("PKG-INFO", "setup.cfg",
                     "log_ai_compressor.egg-info/SOURCES.txt",
                     "log_ai_compressor-2.12.6.dist-info/METADATA"):
            assert mod._BUILD_METADATA.match(name), (
                "%s 被当成了野文件 —— 那会让门禁永远红" % name)
        for name in ("examples/app_report.md", "README.md"):
            assert not mod._BUILD_METADATA.match(name), (
                "%s 不该被当成构建元数据" % name)

    def test_the_markers_cover_the_repository_root(self):
        """标记里必须含仓库根目录 —— 那正是泄漏的那条路径。

        **改坏会红吗**：把 `build_machine_markers()` 里的 ROOT 去掉，本条立刻红。
        """
        mod = _load()
        markers = [raw for _label, raw in mod.build_machine_markers()]
        assert any(str(_ROOT).encode("utf-8") in m or
                   str(_ROOT).encode("utf-8") in m.replace(b"/", b"\\")
                   for m in markers), (
            "标记里没有仓库根目录 —— 泄漏就抓不到")
