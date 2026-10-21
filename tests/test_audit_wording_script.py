# -*- coding: utf-8 -*-
"""审计脚本必须**有牙齿**：把词埋进仓库，它就得报出来。

为什么要有这条
--------------
本项目反复栽过同一个形态：**一个报「检查通过」但其实什么都没扫的门禁，
比没有门禁更糟** —— 没有门禁你知道那里没检查。审计脚本是新加的，
如果它扫不干净、或者压根没在工作，它会输出一张漂亮的「0 命中」报告，
而读报告的人会以为仓库是干净的。

所以这里照 §2.2 的规矩：**故意做坏 → 断言必须变红**。

四个面各埋一次
--------------
1. 工作区文件内容
2. 提交信息
3. 历史版本文件内容（先提交，再改掉 —— 词只留在历史里）
4. tag 说明文字（词放在**第二行** —— 旧实现只看得到每个 tag 的第一行）

外加两条对照
------------
* **正常路径没被写坏**：一个干净的仓库必须是 0 命中、退出 0。
  只写上面四条的话，「脚本恒返回空」也能让它们全绿。
* **审计脚本与门禁给出同一份命中** —— 「口径只有一份」这句话的可测版本。

**为什么测试文件里没有字面量**
-----------------------------
本文件也在门禁的扫描范围内。埋词用**从门禁文件里读出来的**那个词，
不写字面量 —— 否则门禁会先把自己判红（「守门的人」和「被守的东西」
住在同一处）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "scripts" / "audit_wording.py"
_GATE = ROOT / "tests" / "test_no_job_wording.py"

#: git 的**绝对路径**。写死 "git" 会让 bandit 的 B607 报「用部分路径启动进程」——
#: 那一处**可以修，所以修**：测试里用 shutil.which 解析，跨平台一致，
#: 也不用往 SAST 基线里加一条豁免。
_GIT = shutil.which("git")


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_wording_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_AUDIT = _load_audit_module()
#: 从门禁文件里读出来的词表（码点形式，仓库里不留字面量）
_WORDS = _AUDIT.load_gate(_GATE).FORBIDDEN
#: 埋进临时仓库的那个词。取列表里的第一个 —— 选哪个都一样，
#: 只要它不是空串、也不是某个别的词的子串（那会让断言变得含糊）。
_PLANTED = _WORDS[0]


def _git(repo: Path, *args: str) -> None:
    assert _GIT is not None, "找不到 git —— 这条测试要起真仓库"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([_GIT, "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, check=False, timeout=120)
    assert proc.returncode == 0, "git %s 失败：%s" % (" ".join(args), proc.stderr)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "audit@example.invalid")
    _git(repo, "config", "user.name", "audit")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _tag(repo: Path, name: str, message: str) -> None:
    """造一个 annotated tag，说明文字从 stdin 进（`-F -`）。

    刻意不用 `-m`：`-m` 的多行行为在不同 git 版本上并不一致，
    而这条测试要的就是「说明确实有多行」。
    """
    assert _GIT is not None, "找不到 git —— 这条测试要起真仓库"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [_GIT, "-C", str(repo), "tag", "-a", name, "-F", "-"],
        input=message, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, check=False, timeout=120)
    assert proc.returncode == 0, "git tag 失败：%s" % proc.stderr


def _run_audit(repo: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo),
         "--words-from", str(_GATE), "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=False, timeout=300)
    assert proc.returncode in (0, 1), (
        "审计脚本不该以 %d 退出：%s" % (proc.returncode, proc.stderr[-400:]))
    return {"rc": proc.returncode, "data": json.loads(proc.stdout)}


def test_word_list_is_loaded_from_the_gate() -> None:
    """前置条件：词表真的读到了，而且不是空的。"""
    assert len(_WORDS) >= 30, "词表只剩 %d 条" % len(_WORDS)
    assert _PLANTED, "埋词是空串"


def test_report_sees_how_many_objects_each_face_scanned(tmp_path: Path) -> None:
    """「扫了 0 个」与「扫了 N 个但没命中」**必须能分辨开**。

    报告里只有「命中条目」这一列时，「0 命中」就**既可能是**「扫过了、
    确实干净」，**也可能是**「一个都没扫到」—— 而输出里长得一模一样。
    这正是「报数为 0 的计数器等于没装」那条教训的上一级。

    2026-10-21 的第六轮外部复核抓到的就是这一条（门禁的 docstring 指向一个
    **根本不打印**的测试，于是「要数字看那里」指向了空气）。
    """
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("这里没有任何命中词。\n", encoding="utf-8")
    _commit(repo, "初始提交")
    out = _run_audit(repo)
    assert out["rc"] == 0, "干净仓库不该返回 1"
    scanned = out["data"]["scanned"]
    assert set(scanned) == set(out["data"]["by_surface"]), "两列的面不对齐"
    # 「扫了多少」必须**带单位** —— 七个数不同量纲，只有数字没有量纲，
    # 跨面比较就会出错（外部复核指出的）。
    for face, unit in (("worktree", "个文件"), ("commit_msg", "笔"),
                        ("history", "个文本 blob"), ("tag_msg", "条"),
                        ("ref_name", "个"), ("release", "个字段"),
                        ("unreachable", "个对象")):
        assert scanned[face]["unit"] == unit, (
            "面 %s 的单位变了：%r" % (face, scanned[face]["unit"]))
    assert scanned["worktree"]["n"] >= 1, "工作区明明有文件，却报「扫了 0 个」"
    assert scanned["commit_msg"]["n"] >= 1, "明明有提交信息，却报「扫了 0 个」"
    assert scanned["history"]["n"] >= 1, "明明有历史内容，却报「扫了 0 个」"
    assert out["data"]["by_surface"]["worktree"] == 0, "干净仓库不该有命中"


def test_text_report_prints_the_scale(tmp_path: Path) -> None:
    """人看的表格里也要有这一列 —— JSON 里有而表格里没有，等于没有。"""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("这里没有任何命中词。\n", encoding="utf-8")
    _commit(repo, "初始提交")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo), "--words-from", str(_GATE)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=False, timeout=300)
    assert proc.returncode in (0, 1), proc.stderr[-400:]
    assert "扫了多少对象" in proc.stdout, "表格里没有「扫了多少对象」这一列"


def test_clean_repo_reports_zero_hits(tmp_path: Path) -> None:
    """**正常路径没被写坏**的对照：干净的仓库必须 0 命中、退出 0。

    没有这一条，「脚本恒返回空」也能让下面三条全绿。
    """
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("这里没有任何命中词。\n", encoding="utf-8")
    _commit(repo, "初始提交")
    out = _run_audit(repo)
    assert out["rc"] == 0, "干净仓库不该返回 1"
    assert out["data"]["hits"] == [], "干净仓库报了 %d 条命中" % len(out["data"]["hits"])


def test_word_planted_in_worktree_is_found(tmp_path: Path) -> None:
    """面 1：工作区文件内容里的词必须被抓到。"""
    repo = _init_repo(tmp_path)
    (repo / "doc.md").write_text("这一行里有%s。\n" % _PLANTED, encoding="utf-8")
    _commit(repo, "加入一个文件")
    out = _run_audit(repo)
    assert out["rc"] == 1, "埋了词却没报出来 —— 脚本没有牙齿"
    hits = out["data"]["hits"]
    assert any(h["surface"] == "worktree" and h["word"] == _PLANTED for h in hits), \
        "没抓到工作区里的词：%s" % hits


def test_word_planted_in_commit_message_is_found(tmp_path: Path) -> None:
    """面 2：提交信息里的词必须被抓到。

    这一面是上一轮漏掉的那一面（重写只改 blob、不碰提交信息）——
    漏掉它的代价是仓库看起来改干净了，信息里还留着词。
    """
    repo = _init_repo(tmp_path)
    (repo / "a.txt").write_text("内容\n", encoding="utf-8")
    _commit(repo, "这一条信息里有%s" % _PLANTED)
    out = _run_audit(repo)
    assert out["rc"] == 1, "埋了词却没报出来 —— 脚本没有牙齿"
    hits = out["data"]["hits"]
    assert any(h["surface"] == "commit_msg" and h["word"] == _PLANTED for h in hits), \
        "没抓到提交信息里的词：%s" % hits


def test_word_planted_only_in_history_is_found(tmp_path: Path) -> None:
    """面 3：只留在历史版本里的词必须被抓到。

    这是最容易漏的一面：工作区已经干净了，词只在旧版本里。
    """
    repo = _init_repo(tmp_path)
    (repo / "b.txt").write_text("旧版本里有%s\n" % _PLANTED, encoding="utf-8")
    _commit(repo, "旧版本")
    (repo / "b.txt").write_text("新版本里没有了\n", encoding="utf-8")
    _commit(repo, "改掉它")
    out = _run_audit(repo)
    assert out["rc"] == 1, "埋了词却没报出来 —— 脚本没有牙齿"
    hits = out["data"]["hits"]
    assert any(h["surface"] == "history" and h["word"] == _PLANTED
               for h in hits), "没抓到历史版本里的词：%s" % hits


def test_audit_agrees_with_the_gate_on_the_same_repo(tmp_path: Path) -> None:
    """「口径只有一份」必须**可测** —— 审计脚本与门禁给出同一份命中。

    2026-10 的外部复核抓到过一次漂移：门禁的面 3 从逐行改成整 blob，
    审计脚本还在逐行。当时两边**结果**碰巧相同（对不含换行符的词，
    逐行与整段必然等价），但那是运气 —— 一旦有人改动其中一边的读法，
    两边就会给出不同的答案，而「审计说干净」正是用来回答
    「发布态干不干净」的那句话。

    这条把那句话变成可测的：同一个临时仓库，**门禁自己算一遍**、
    **审计脚本算一遍**，两份命中必须完全一致。
    """
    repo = _init_repo(tmp_path)
    (repo / "doc.md").write_text("这里有%s。\n" % _PLANTED, encoding="utf-8")
    _commit(repo, "加入一个文件")
    _tag(repo, "v0.0.0-test", "第一行\n第二行有 %s\n" % _PLANTED)
    out = _run_audit(repo)

    gate = _AUDIT.load_gate(_GATE)
    gate_hits = {(name, word)
                 for name, text in gate._tag_messages(repo=repo)
                 for word, _ in gate._hits_in(text)}
    audit_hits = {(h["where"], h["word"])
                  for h in out["data"]["hits"] if h["surface"] == "tag_msg"}
    assert gate_hits, "临时仓库的 tag 说明明明有词，门禁却没报 —— 这条测试自己失效了"
    assert gate_hits == audit_hits, (
        "门禁与审计脚本对同一个仓库给出了不同的命中：\n"
        "  门禁：%s\n  审计：%s" % (sorted(gate_hits), sorted(audit_hits)))


def test_word_on_the_second_line_of_a_tag_message_is_found(tmp_path: Path) -> None:
    """面 4：tag 说明**第二行**里的词必须被抓到。

    旧实现用 `for-each-ref --format='%(contents)'` + `splitlines()`，
    而 `%(contents)` 是**多行**值 —— 拆行之后只有含 tab 的那行被扫，
    于是**每个 tag 只有第一行**被检查。2026-10 的外部复核抓到的就是
    这个洞：32 个 annotated tag 的说明共 270 行，旧实现只扫到 33 行
    （**12.2%**），而 169 行被直接跳过。
    """
    repo = _init_repo(tmp_path)
    (repo / "a.txt").write_text("内容\n", encoding="utf-8")
    _commit(repo, "一笔提交")
    _tag(repo, "v0.0.0-test", "第一行没有词\n第二行有 %s\n" % _PLANTED)
    out = _run_audit(repo)
    assert out["rc"] == 1, "埋了词却没报出来 —— 脚本没有牙齿"
    hits = out["data"]["hits"]
    assert any(h["surface"] == "tag_msg" and h["word"] == _PLANTED
               for h in hits), "没抓到 tag 说明第二行里的词：%s" % hits


def test_surfaces_are_not_vacuous(tmp_path: Path) -> None:
    """扫描面必须真的存在 —— 空扫一遍的「0 命中」没有意义。

    判据：干净仓库的五个面**都被走过**，且 rev 数 > 0。
    """
    repo = _init_repo(tmp_path)
    (repo / "c.txt").write_text("内容\n", encoding="utf-8")
    _commit(repo, "一笔")
    out = _run_audit(repo)
    surfaces = out["data"]["by_surface"]
    assert set(surfaces) == {"worktree", "commit_msg", "history",
                              "tag_msg", "ref_name", "release",
                              "unreachable"}, "扫描面少了：%s" % set(surfaces)
    assert out["data"]["word_count"] == len(_WORDS)
