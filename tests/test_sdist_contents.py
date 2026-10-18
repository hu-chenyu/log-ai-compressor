# -*- coding: utf-8 -*-
"""sdist 交付物完整性测试：README 承诺的文件必须真的在包里。

**这条测试是被一次真实事故逼出来的**
------------------------------------
2.1.0 的 sdist 按"源码交付"设计（MANIFEST.in 显式带上 tests/、scripts/、
examples/），却漏了三样 README 明确引用过的东西：

* ``start.bat`` —— README §3「Windows：双击运行」整节指着它，
  解压出来的目录里**整条 Windows 入口消失**；
* ``docs/images/web-ui.jpg`` —— README 首图。
  ``recursive-include docs *.md`` 只收 markdown，图片被静默滤掉，
  从 sdist 读 README 得到的是一张裂图；
* ``SKILL.md`` —— Agent Skills 标准文件，README §3 引用它。

漏掉的原因不是忘了写规则，而是**没人核对规则与产物是否一致**：
MANIFEST.in 里写着 ``*.md`` 看起来完全合理，只有解开包才知道图没了。
SKILL.md 更隐蔽 —— 它在仓库根目录、不在任何通配符能扫到的地方。

所以这里不测"MANIFEST.in 有没有写某行字"（那只是检查拼写），
而是测**产物本身**：自动扫出 README 里所有仓库内相对链接 + 反引号提到的路径，
先核对能被规则覆盖，再解开 dist/ 下的 sdist 核对同一批文件是否真的在里面。
没构建过就跳过，构建了就必须全中。

判据
----
**README 里引用的每一个仓库内文件，都必须出现在 sdist 里。**
两边不一致时包比文档更可能是错的 —— 文档是承诺，包是交付。

与 ``test_packaging.py`` 的分工：那边管"可选依赖装不装得上"，
这边管"包里有没有该有的东西"，互不重叠。
"""
from __future__ import annotations

import fnmatch
import re
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_IN = ROOT / "MANIFEST.in"
README = ROOT / "README.md"
DIST = ROOT / "dist"

# README 里以反引号提到、但不是 markdown 链接的仓库内路径。
# markdown 链接下面会自动扫，这里只补扫不到的。
BACKTICK_PATHS = (
    "start.bat",
    "scripts/prove.py",
    "scripts/adversarial_eval.py",
)

_MD_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _manifest_rules() -> tuple[list[tuple[str, list[str]]], list[str]]:
    """解析 MANIFEST.in，返回 (include 规则, prune 目录)。"""
    includes: list[tuple[str, list[str]]] = []
    pruned: list[str] = []
    for raw in MANIFEST_IN.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "include" and len(parts) >= 2:
            includes.append(("", parts[1:]))
        elif parts[0] == "recursive-include" and len(parts) >= 3:
            includes.append((parts[1].strip("/"), parts[2:]))
        elif parts[0] == "prune" and len(parts) >= 2:
            pruned.append(parts[1].strip("/"))
    return includes, pruned


def _covered_by_manifest(rel: str) -> bool:
    """rel（仓库相对路径）是否会被 MANIFEST.in 收进 sdist。"""
    includes, pruned = _manifest_rules()
    posix = rel.replace("\\", "/")
    for pruned_dir in pruned:
        if posix == pruned_dir or posix.startswith(pruned_dir + "/"):
            return False
    for base, patterns in includes:
        if not base:
            if any(fnmatch.fnmatch(posix, pat) for pat in patterns):
                return True
            continue
        if not (posix == base or posix.startswith(base + "/")):
            continue
        name = posix.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(name, pat) for pat in patterns):
            return True
    return False


def _readme_link_targets() -> list[str]:
    """README 里所有指向仓库内部的相对链接目标（不判存在性）。"""
    found: list[str] = []
    for target in _MD_LINK.findall(README.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        clean = target.split("#", 1)[0].split("?", 1)[0].lstrip("./")
        if clean and not clean.startswith("/"):
            found.append(clean)
    return found


def _readme_referenced_paths() -> set[str]:
    """README 引用、且仓库里确实存在的仓库内文件。"""
    return {p for p in _readme_link_targets() if (ROOT / p).is_file()}


@pytest.fixture(scope="module")
def sdist() -> tarfile.TarFile | None:
    """dist/ 下最新的 sdist；没构建过则返回 None。"""
    archives = sorted(DIST.glob("log_ai_compressor-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    if not archives:
        return None
    return tarfile.open(archives[-1], "r:gz")


def _sdist_names(archive: tarfile.TarFile) -> set[str]:
    """解出成员名，去掉顶层目录前缀。"""
    names: set[str] = set()
    for member in archive.getmembers():
        if not member.isfile():
            continue
        parts = member.name.split("/", 1)
        if len(parts) == 2:
            names.add(parts[1])
    return names


# ---------------------------------------------------------------------------
# 第一层：文档自身不能指向不存在的文件
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rel", BACKTICK_PATHS)
def test_backticked_paths_exist(rel: str) -> None:
    assert (ROOT / rel).is_file(), f"README 提到 {rel}，但仓库里没有这个文件"


def test_readme_relative_links_all_resolve() -> None:
    missing = [p for p in _readme_link_targets() if not (ROOT / p).exists()]
    assert not missing, f"README 指向了仓库里不存在的路径：{sorted(set(missing))}"


# ---------------------------------------------------------------------------
# 第二层：MANIFEST.in 的规则必须覆盖 README 承诺的文件
# ---------------------------------------------------------------------------
def test_manifest_covers_readme_referenced_paths() -> None:
    uncovered = sorted(p for p in _readme_referenced_paths() if not _covered_by_manifest(p))
    assert not uncovered, (
        "这些文件被 README 引用，但 MANIFEST.in 的规则收不进 sdist："
        f"{uncovered}。修 MANIFEST.in，别改 README —— 文档是承诺，包是交付。")


def test_manifest_covers_backticked_paths() -> None:
    uncovered = sorted(p for p in BACKTICK_PATHS if not _covered_by_manifest(p))
    assert not uncovered, f"MANIFEST.in 漏收了 README 承诺的文件：{uncovered}"


def test_start_bat_is_explicitly_included() -> None:
    """start.bat 必须靠裸的 `include` 进包。

    通配符救不了它：``recursive-include`` 只收列出的扩展名，
    批处理文件不会被任何一条规则命中。
    """
    rules = [line.split() for line in MANIFEST_IN.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert ["include", "start.bat"] in rules, "MANIFEST.in 必须有裸的 `include start.bat`"


def test_docs_rules_cover_images() -> None:
    """docs 的通配符必须命中图片扩展名，否则 README 首图在包里是裂的。"""
    doc_rules = [line.split() for line in MANIFEST_IN.read_text(encoding="utf-8").splitlines()
                 if line.strip().startswith("recursive-include docs")]
    assert doc_rules, "MANIFEST.in 没有 docs 的递归规则"
    covered = {pat for rule in doc_rules for pat in rule[2:]}
    # 用 fnmatch 语义判断，而不是直接比对字符串 —— 规则写的是 *.jpg 不是 .jpg
    assert any(fnmatch.fnmatch("web-ui.jpg", pat) for pat in covered), (
        f"docs 规则只收了 {sorted(covered)}，命中不了 .jpg —— "
        "README 首图在 sdist 里会是裂的")


# ---------------------------------------------------------------------------
# 第三层：产物本身（没构建过就跳过，构建了就必须全中）
# ---------------------------------------------------------------------------
def test_sdist_contains_readme_referenced_paths(sdist: tarfile.TarFile | None) -> None:
    if sdist is None:
        pytest.skip("dist/ 下没有 sdist，跳过产物层校验（先跑 python -m build）")
    names = _sdist_names(sdist)
    missing = sorted(p for p in _readme_referenced_paths() if p not in names)
    assert not missing, f"sdist 里缺少 README 引用到的文件：{missing}"


def test_sdist_contains_backticked_paths(sdist: tarfile.TarFile | None) -> None:
    if sdist is None:
        pytest.skip("dist/ 下没有 sdist，跳过产物层校验")
    names = _sdist_names(sdist)
    missing = sorted(p for p in BACKTICK_PATHS if p not in names)
    assert not missing, f"sdist 里缺少 README 承诺的文件：{missing}"


def test_sdist_contains_the_test_suite(sdist: tarfile.TarFile | None) -> None:
    """sdist 的意义是"源码交付"：没有测试它就只是个 zip。"""
    if sdist is None:
        pytest.skip("dist/ 下没有 sdist，跳过产物层校验")
    names = _sdist_names(sdist)
    for required in ("tests/test_forensics.py",
                     "tests/test_packaging.py",
                     "tests/test_sdist_contents.py",
                     "scripts/prove.py",
                     "scripts/adversarial_eval.py",
                     "examples/sample_system.log"):
        assert required in names, f"sdist 里缺少 {required}"


def test_sdist_contains_changelog(sdist: tarfile.TarFile | None) -> None:
    """变更记录属于交付物。

    README 没链接 CHANGELOG.md，所以上面那条"README 引用即必须在包里"的
    规则扫不到它 —— 这类"没人引用但按理该在"的交付约定要单独钉。
    """
    if sdist is None:
        pytest.skip("dist/ 下没有 sdist，跳过产物层校验")
    assert "CHANGELOG.md" in _sdist_names(sdist), (
        "sdist 里没有 CHANGELOG.md —— 源码交付不带变更记录说不过去")


def test_sdist_contains_contributor_guides(sdist: tarfile.TarFile | None) -> None:
    """AGENTS.md / CONTRIBUTING.md 必须在包里。

    与 CHANGELOG 同理：README 未必链接它们，但源码交付的读者要能查到
    「怎么在这个仓库里干活」。它们在**仓库根目录**，不在任何
    ``recursive-include`` 能扫到的位置，只能靠裸 ``include`` ——
    少了那两行，这条测试就会抓到（MANIFEST.in 里已经显式 include）。
    """
    if sdist is None:
        pytest.skip("dist/ 下没有 sdist，跳过产物层校验")
    names = _sdist_names(sdist)
    missing = [p for p in ("AGENTS.md", "CONTRIBUTING.md") if p not in names]
    assert not missing, f"sdist 里缺少贡献指南：{missing}"

    uncovered = [p for p in ("AGENTS.md", "CONTRIBUTING.md")
                 if not _covered_by_manifest(p)]
    assert not uncovered, (
        f"MANIFEST.in 的规则收不进这些根级文件：{uncovered}。"
        "根目录的 .md 不会被 recursive-include 命中，必须裸 include。")


def test_sdist_contains_audit_doc(sdist: tarfile.TarFile | None) -> None:
    """`AUDIT.md` 必须在包里 —— 它是**测试自己**要读的数据文件。

    这条的形状与 CHANGELOG / 贡献指南不同，所以要单独钉：

    ``AUDIT.md`` 是缺陷形态记录，而 ``tests/test_mutation_scorecard.py`` 与
    ``tests/test_prove_claims_gate.py`` 都**无守卫直读**它。tests/ 随 sdist
    交付，于是文件一漏，从源码交付里跑 pytest 就是两条 FileNotFoundError。

    它同时是「检查器自己会坏」那一类：一条需要包外状态才成立的判据，
    在拿到源码的人手里是坏的，比没有更让人误解 —— 而且**难查**，因为
    从 GitHub clone 的人一切正常，只有 pip download 的人会撞上。
    """
    assert _covered_by_manifest("AUDIT.md"), (
        "MANIFEST.in 收不进 AUDIT.md —— 它在仓库根目录，"
        "不在任何 recursive-include 能扫到的位置，必须裸 include")

    if sdist is None:
        pytest.skip("dist/ 下没有 sdist，跳过产物层校验")
    assert "AUDIT.md" in _sdist_names(sdist), (
        "sdist 里没有 AUDIT.md —— 从源码交付里跑 pytest 会有两条 "
        "FileNotFoundError，而从 GitHub clone 的人永远碰不到")


def test_sdist_contains_benchmark_baseline(sdist: tarfile.TarFile | None) -> None:
    """``docs/benchmark_baseline.json`` 必须在包里。

    ``scripts/prove.py`` 随 sdist 一起交付，而它有一条主张要读这份基线
    对账 README 的基准表。基线不进包 = **从 sdist 里跑自证脚本必然那条红**。

    这就是「检查器自己会坏」：一条需要外部状态才成立的门禁，在别人手里
    是坏的，比没有更让人误解。
    """
    if sdist is None:
        pytest.skip("dist/ 下没有 sdist，跳过产物层校验")
    assert "docs/benchmark_baseline.json" in _sdist_names(sdist), (
        "sdist 里没有基准基线 —— 从源码交付里跑 scripts/prove.py 时，"
        "「README 基准表与实测一致」这条主张必然失败")
    assert _covered_by_manifest("docs/benchmark_baseline.json"), (
        "MANIFEST.in 的 docs 规则没覆盖 *.json —— 基准基线收不进包")




def test_sdist_excludes_bulk_dataset(sdist: tarfile.TarFile | None) -> None:
    """Loghub 数据集 178 MB，绝不能进 sdist（会卡死上传）。"""
    if sdist is None:
        pytest.skip("dist/ 下没有 sdist，跳过产物层校验")
    leaked = sorted(n for n in _sdist_names(sdist) if n.startswith("data/"))
    assert not leaked, f"sdist 里混进了 data/ 数据集：{leaked[:5]}"
