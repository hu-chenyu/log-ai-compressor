# -*- coding: utf-8 -*-
"""构建的输入必须是**已提交的树**，不是工作区。

为什么
----
2026-10-23 第十一轮外部复核实测：已发布的 2.12.6 sdist 里夹着三个**从未进过
任何 commit** 的文件（``examples/app_report.md`` 等），而且它们嵌着构建机的
绝对路径与账户名（各 7～11 次）。

机制是三件各自都正确的事凑在一起：

* ``MANIFEST.in`` 收了 ``examples/*`` —— 生成物也匹配；
* ``.gitignore`` 把 ``*_report.*`` 挡住 —— 它们在 ``git status`` 里**隐身**；
* 构建在工作区里跑 —— 它们被静默打包。

⇒ 「三方 sha256 逐字节相同」对 sdist **不成立**（从 tag 重建出来是另一份字节），
而这一版的门禁（词表扫描 + 产物自带 SAST）原理上查不到这一类。

修法是**换掉构建的输入**：`scripts/build_repro.py` 改用 ``git archive`` 导出
一份快照，在快照里构建。未入库/被忽略的文件根本不在快照里；
而且快照按 ``.gitattributes`` 归一行尾 ⇒ 同一份源码在任何机器上导出成
同样的字节。

**改坏会红吗**：把 `build_repro.py` 的构建改回在工作区里跑（``cwd=ROOT`` 且
不导出快照），本文件第 2 条立刻红 —— 工作区里的 CRLF 文件会被原样打进产物。
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "build_repro.py"

#: git 的**绝对路径**。SAST（bandit B607）报的是「用部分路径启动进程」——
#: 写死 "git" 会在 git 不在 PATH 时抛 FileNotFoundError，看不出是
#: 「git 缺失」还是「测试跑错目录」。scripts/build_repro.py 里也是这么做的。
_GIT = shutil.which("git") or "git"


def _load():
    spec = importlib.util.spec_from_file_location("build_repro", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_repro"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(*args: str) -> bytes:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([_GIT, *args], cwd=str(_ROOT), capture_output=True,
                          env=env, timeout=180)
    return proc.stdout


def _attr(path: str, name: str) -> str:
    """问 git：这个文件的某个属性是什么（``eol`` / ``text``）。

    输出形如 ``<路径>: eol: lf`` —— 取**最后**一段才是属性值
    （路径本身可能带冒号）。
    """
    out = _git("check-attr", name, "--", path).decode("utf-8", "replace").strip()
    m = re.search(r":\s*([\w-]+)\s*$", out)
    return m.group(1) if m else ""


class TestTheBuildInputIsTheCommittedTree:
    def test_the_snapshot_has_exactly_the_committed_files(self, tmp_path: Path):
        """快照里的文件清单必须与 HEAD **逐项相同**（不多不少）。

        多了就是「野生文件」被带进产物；少了就是源码没进产物。

        **改坏会红吗**：把 `snapshot_into` 改成直接复制工作区，
        本条立刻红（工作区里有被忽略的生成文件）。
        """
        mod = _load()
        dest = mod.snapshot_into(tmp_path / "snap", "HEAD")
        on_disk = set()
        for p in dest.rglob("*"):
            if p.is_file():
                on_disk.add(p.relative_to(dest).as_posix())
        committed = {ln.strip() for ln in _git("ls-tree", "-r", "--name-only", "HEAD")
                     .decode("utf-8", "replace").splitlines() if ln.strip()}
        extra = sorted(on_disk - committed)
        missing = sorted(committed - on_disk)
        assert not extra, "快照里多了 %d 个源码树里没有的文件：%s" % (len(extra), extra[:10])
        assert not missing, "快照里少了 %d 个源码树里有的文件：%s" % (len(missing), missing[:10])

    def test_the_snapshot_content_is_the_blob_not_the_worktree(self, tmp_path: Path):
        """快照里的字节必须与 **git 里存的那份**相同 —— 不是工作区那份。

        判据用 ``docs/sast_baseline.json``：它的 blob 是 LF，而 Windows 上
        检出后是 CRLF（本机 ``core.autocrlf=true``）。两份不一样，判据才有意义。

        **改坏会红吗**：把 `snapshot_into` 改成复制工作区，本条立刻红。
        """
        mod = _load()
        dest = mod.snapshot_into(tmp_path / "snap", "HEAD")
        target = "docs/sast_baseline.json"
        blob = _git("show", "HEAD:" + target)
        copied = (dest / target).read_bytes()
        assert copied == blob, (
            "快照里的 %s 与 git 里存的那份不一样（%d vs %d 字节）—— "
            "构建的输入不是源码树" % (target, len(copied), len(blob)))

    def test_every_text_file_in_the_tree_has_an_eol_rule(self):
        """进产物的每个文本文件都必须有 eol 规则，否则产物**随构建机变**。

        实测：已发布的 2.12.6 sdist 里 MANIFEST.in / docs\\*.json / docs\\*.csv /
        server.json 是 CRLF，而 git 里存的是 LF —— 同一份源码在不同机器上
        构建出不同字节的产物，「三方 sha256 对账」只在那台机器上成立。
        同样的洞让 wheel 差 21 字节：``LICENSE`` 没钉 eol。

        **改坏会红吗**：把 ``.gitattributes`` 里 ``*.json text eol=lf`` 那一行
        删掉，本条立刻红。
        """
        attrs = (_ROOT / ".gitattributes").read_text(encoding="utf-8")
        rules = set()
        for line in attrs.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if "eol=" in line:
                rules.add(parts[0])
        # 进产物的文本文件：源码 + 文档 + 数据 + 配置
        must_cover = ["*.py", "*.md", "*.json", "*.csv", "*.in", "*.yaml",
                      "*.yml", "*.toml", "*.txt", "*.html", "*.css", "*.js"]
        missing = [pat for pat in must_cover if pat not in rules]
        assert not missing, (
            "这些扩展名没有 eol 规则：%s —— 产物会随构建机的 core.autocrlf 变" % missing)
        assert "LICENSE" in rules, (
            "LICENSE（无扩展名）没有 eol 规则 —— 实测它让 wheel 差了 21 字节")

    def test_every_tracked_text_file_has_an_eol_rule(self):
        """**逐个跟踪文件**核对 —— 而不是核对一份写死的扩展名清单。

        为什么必须逐个：上一条查的是「must_cover = ['*.py', '*.md', ...]」
        这份**写死的清单**，而写死的清单必然漏 —— 漏的正是清单没列的那类。
        实测（2026-10-24 第十二轮外部复核）：``.gitignore`` /
        ``.gitattributes`` / ``py.typed`` 这三个**无扩展名**的文件没有 eol
        规则，于是它们跟着构建机的 ``core.autocrlf`` 走 ——
        已发布的 2.12.7 sdist 在两种机器上重建出**两份字节**
        （29a9f8bf… vs c3001fed…，差 96 个 CR）。

        判据：问 git「这个文件的 eol 是什么」——
        要么有明确的 eol（lf / crlf），要么它被声明成二进制。

        **改坏会红吗**：把 ``.gitattributes`` 里 ``.gitignore text eol=lf``
        那一行删掉，本条立刻红。
        """
        files = [ln for ln in _git("ls-files", "-z").decode("utf-8", "replace").split("\0")
                 if ln]
        assert files, "一个跟踪文件都没有 —— git 读不到？"
        missing = []
        for name in files:
            eol = _attr(name, "eol")
            text = _attr(name, "text")
            if eol == "unspecified" and text != "unset":
                missing.append(name)
        assert not missing, (
            "这些跟踪文件没有 eol 规则（也没声明成二进制）：%s —— "
            "它们的字节会随构建机的 core.autocrlf 变，"
            "「三方 sha256 对账」就只在那台机器上成立" % missing)

    def test_the_export_does_not_depend_on_the_build_machine_config(self):
        """同一笔提交，在两种 git 配置下导出的字节必须**完全一样**。

        这是「产物与构建机无关」这条承诺的**直接判据**：把
        ``core.autocrlf`` 逼成 true 与 false 各导一次，比字节。

        为什么不能靠「同一台机器构建两次」：那是**同一个环境**跑两遍 ——
        而这一版的缺陷恰恰是**环境**参与了结果（§4b 记的同一个形状：
        环境输入泄漏进了结论）。

        **改坏会红吗**：把 ``.gitattributes`` 里 ``.gitignore text eol=lf``
        删掉，本条立刻红（两个导出的那一个文件会差 50 个 CR）。
        """
        mod = _load()
        lf = mod.export_bytes("HEAD", mod._ARCHIVE_CONFIG_LF)
        crlf = mod.export_bytes("HEAD", mod._ARCHIVE_CONFIG_CRLF)
        assert lf and crlf, "导出为空 —— git archive 失败？"
        diff = sorted(k for k in set(lf) | set(crlf) if lf.get(k) != crlf.get(k))
        assert not diff, (
            "这些文件在两种 git 配置下导出的字节不同（%d 个）：%s —— "
            "产物会随构建机变，给它们补一条 eol 规则" % (len(diff), diff[:10]))

    def test_the_build_refuses_to_produce_a_machine_dependent_artifact(self):
        """构建前必须**自证**导出与构建机无关 —— 不然就停下。

        **改坏会红吗**：把 ``build()`` 里那句 ``assert_config_independent(ref)``
        删掉，本条立刻红（源码里找不到了）。
        """
        mod = _load()
        assert callable(mod.assert_config_independent), (
            "没有 assert_config_independent() —— 构建不再自证与机器无关")
        # 导出配置必须**钉死**：空配置 = 跟着构建机的 core.autocrlf 走。
        # 这条是**源码级**判据，而且是环境无关的 —— 它在每台机器上都成立
        #（§4d 规则五：只在作者那台机器上有牙齿的断言等于没有牙齿）。
        assert "core.autocrlf=false" in mod._ARCHIVE_CONFIG_LF, (
            "导出配置没有钉死 core.autocrlf ⇒ 快照的字节跟着构建机走：%s"
            % (mod._ARCHIVE_CONFIG_LF,))
        assert "core.eol=lf" in mod._ARCHIVE_CONFIG_LF, (
            "导出配置没有钉死 core.eol ⇒ 快照的字节跟着构建机走：%s"
            % (mod._ARCHIVE_CONFIG_LF,))
        assert mod.assert_config_independent("HEAD") == [], (
            "当前的导出结果与构建机配置有关 —— 补 eol 规则")
        src = (_ROOT / "scripts" / "build_repro.py").read_text(encoding="utf-8")
        assert "assert_config_independent(ref)" in src, (
            "build() 没调用 assert_config_independent() —— "
            "构建不再自证与机器无关")

    def test_the_build_refuses_a_dirty_worktree(self):
        """工作区有未提交改动时，构建必须**停下来**。

        产物是从已提交的树导出的 ⇒ 继续构建等于交付一份与眼前工作区对不上的
        产物，而「产物对应哪一笔提交」是这个项目最要紧的纪律。

        **改坏会红吗**：把 `main()` 里那段「工作区脏就退出」去掉，本条立刻红。
        """
        mod = _load()
        assert callable(mod.uncommitted_changes), (
            "没有 uncommitted_changes() —— 工作区脏检查不见了")
        src = (_ROOT / "scripts" / "build_repro.py").read_text(encoding="utf-8")
        assert "uncommitted_changes()" in src, (
            "main() 没调用 uncommitted_changes() —— 脏工作区的构建不会被拦下")
        assert "return 2" in src, (
            "工作区脏时没有以非零退出码停下")
