# -*- coding: utf-8 -*-
"""`.github/workflows/publish-mcp.yml` 这条发布路径的门禁测试。

为什么一个 YAML 文件也要门禁
---------------------------
`publish-mcp.yml` 是本仓库**唯一**一条往官方 MCP Registry 提交清单的路径，
而 Registry 是公开的、条目一经提交就对所有人可见。它上面有三类东西值得钉住：

1. **凭据形态**。必须走 OIDC（`id-token: write` + `login github-oidc`）。
   换成 `login github` 看着也能跑，但命名空间由「谁登录」决定 —— 用本机
   账号登录会把条目发到 `io.github.<本机账号>/*` 去，而清单写的是
   `io.github.hu-chenyu/*`，直接 403。
2. **清单不许带着坏数据去换权限**。`validate` 不需要认证，而 `login` 需要。
   两者顺序反了，等于让一份非法清单先碰到发布凭据。
3. **「命令返回 0」不等于「登记生效了」**。所以发布后必须有自检，且自检
   判据是**全名精确命中**而不是 count > 0。

还有一条是实测踩出来的：Registry 的 `GET /v0/servers/<id>` 端点**不存在**
（返回 404 `Endpoint not found`），只有 `?search=` 能用。照抄别人文档里的
端点形状会写出一条永远查不动的自检。

本文件的一半用例是「故意改坏 → 必须变红」。依据 AGENTS.md §2.2：
一个从未变红过的门禁等于没有门禁。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parent.parent
WORKFLOW = BASE / ".github" / "workflows" / "publish-mcp.yml"

INSTALL_STEP = "Install mcp-publisher (checksum-verified)"
GUARD_STEP = "Guard - the listing must describe the version being released"
VALIDATE_STEP = "Validate server.json against the live registry"
LOGIN_STEP = "Authenticate (GitHub OIDC)"
PUBLISH_STEP = "Publish"
VERIFY_STEP = "Verify the listing is actually reachable"

# 长期凭据的指纹。工作流里不该出现任何一种。
SECRET_MARKERS = (
    "ghp_",
    "gho_",
    "github_pat_",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "MCP_REGISTRY_TOKEN",
)


# ---------------------------------------------------------------------------
# 读取与判据（拆成独立函数，变异用例才能拿改坏的副本喂进来）
# ---------------------------------------------------------------------------
def _text(path: Path = WORKFLOW) -> str:
    return path.read_text(encoding="utf-8")


def _doc(path: Path = WORKFLOW) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """YAML 1.1 把裸 `on` 解析成布尔 True，两种键名都要认。"""
    return doc.get("on") if "on" in doc else doc.get(True)


def _runs(doc: dict) -> dict[str, str]:
    """step name -> 该 step 的 run 脚本正文。"""
    out: dict[str, str] = {}
    for step in doc["jobs"]["publish"]["steps"]:
        name = step.get("name") or step.get("uses") or ""
        out[name] = step.get("run") or ""
    return out


def _order(doc: dict) -> list[str]:
    return [(s.get("name") or s.get("uses") or "") for s in doc["jobs"]["publish"]["steps"]]


# --- 单条判据 --------------------------------------------------------------
def check_oidc_permission(doc: dict) -> None:
    perms = doc.get("permissions") or {}
    assert perms.get("id-token") == "write", (
        f"没有 id-token: write，OIDC 登录拿不到令牌；实际 {perms!r}")
    assert perms.get("contents") == "read", (
        f"checkout 需要 contents: read；实际 {perms!r}")


def check_oidc_login(runs: dict[str, str]) -> None:
    body = runs[LOGIN_STEP]
    assert "github-oidc" in body, (
        "必须用 login github-oidc：OIDC 的命名空间由仓库 owner 推导，"
        "结构上就对得上清单里的 io.github.hu-chenyu/*")
    assert "login github\n" not in body and "login github " not in body, (
        "login github 的命名空间由「谁登录」决定，会把条目发到别人的命名空间")


def check_no_long_lived_secret(text: str) -> None:
    for marker in SECRET_MARKERS:
        assert marker not in text, (
            f"工作流里出现长期凭据 {marker!r} —— 发布身份应当只由 OIDC 承担")


def check_checksum_before_execute(runs: dict[str, str]) -> None:
    body = runs[INSTALL_STEP]
    assert "sha256sum" in body, "下载的二进制没有校验和"
    assert '"$expected"' in body and '"$actual"' in body, (
        "只算出了两个值却没比较 —— 那不是校验")
    assert '"$expected" != "$actual"' in body, (
        "校验和比较被去掉了或取反了")
    assert body.index("sha256sum") < body.index("tar xzf"), (
        "必须在解压执行之前校验，否则校验只是一段装饰")


def check_version_guard(runs: dict[str, str]) -> None:
    body = runs[GUARD_STEP]
    assert '"$declared" != "$packaged"' in body, (
        "少了 server.json 与 pyproject 的一致性检查 —— "
        "两者错开时 Registry 会挂着一个指向别的版本的条目，而它看起来完全正常")
    assert '"$declared" != "$tag"' in body, (
        "少了 tag 与清单版本的一致性检查")


def check_validate_precedes_login(doc: dict) -> None:
    order = _order(doc)
    runs = _runs(doc)
    assert VALIDATE_STEP in order, "缺少 validate：非法清单会在换到发布权限之后才被发现"

    # 步骤名对了不等于它真的跑了。只认名字的话，把 run 换成一句 echo
    # 就能让这道守卫变绿 —— 而那正是它想防的事。
    assert "mcp-publisher validate" in runs[VALIDATE_STEP], (
        f"「{VALIDATE_STEP}」这一步的 run 里没有 mcp-publisher validate："
        "步骤名对了不等于它真的跑了")

    assert LOGIN_STEP in order, "缺少登录步骤"
    assert "mcp-publisher login" in runs[LOGIN_STEP], (
        f"「{LOGIN_STEP}」这一步的 run 里没有 mcp-publisher login")
    assert order.index(VALIDATE_STEP) < order.index(LOGIN_STEP), (
        "validate 不需要认证而 login 需要。顺序反了 = "
        "让一份没过校验的清单先碰到发布凭据")


def check_publish_present(doc: dict) -> None:
    order = _order(doc)
    assert PUBLISH_STEP in order, "缺少发布步骤"
    assert "mcp-publisher publish" in _runs(doc)[PUBLISH_STEP], (
        f"「{PUBLISH_STEP}」这一步的 run 里没有 mcp-publisher publish："
        "步骤名对了不等于它真的跑了")


def check_publish_tolerates_ambiguous_failure(runs: dict[str, str]) -> None:
    """发布失败时，先问「到了吗」再决定要不要重试。

    这条来自一次真实事故：Registry 自己的 nginx 返回
    ``504 Gateway Time-out``，``mcp-publisher`` 报 publish failed，而
    **服务端到底提交没有是未知的**。第一版工作流在这种失败上做两件错事：

    1. 不重试 —— 把一次瞬时网关故障当成永久失败，登记从此卡在旧版本；
    2. 不回查 —— ``Publish`` 一红，``Verify`` 被跳过，于是**没有人知道
       服务端的真实状态**。

    而盲目重试同样错：若第一次其实已提交，重试会撞上
    "version already exists"，把一次**成功**的发布报成失败。所以重试前
    必须先独立回答「目标版本在不在册」。
    """
    body = runs[PUBLISH_STEP]
    assert "for i in 1 2 3" in body, (
        "publish 没有重试 —— Registry 实测会返回 504，"
        "单次尝试等于把瞬时故障当成永久失败")
    # 认的是**调用点**而不是函数名：函数定义还在、调用被换掉的话，
    # 「有没有回查」这件事已经变了，而只搜函数名会照样绿。
    assert "if already_listed; then" in body, (
        "重试前没有先独立回查「目标版本在不在册」。504 是有歧义的失败，"
        "盲目重试会撞上 version already exists，"
        "把一次其实成功的发布报成失败")


def check_exact_name_verify(runs: dict[str, str]) -> None:
    body = runs[VERIFY_STEP]
    # 顺序有意：先查那条**实测不存在**的端点。
    # 否则改用它的变异会被下面那条「应当走 search」先拦下 ——
    # 守卫照样红，但红的原因指错了地方（变异存活不等于没牙齿，
    # 也要看红的是不是该红的那一条）。
    assert "/v0/servers/$name" not in body, (
        "GET /v0/servers/<id> 实测返回 404 Endpoint not found，这条端点不存在")
    assert "v0/servers'" in body, "自检应当走 ?search= 查询"
    assert '["name"] == want_name' in body, (
        "自检必须是全名精确匹配。count > 0 会被同名子串命中 —— "
        "搜 log-ai-compressor 会连别的条目一起算进来，那种绿灯是假的")

    # 只核对名字是不够的：同一个 name 挂着多个版本是 Registry 的常态。
    # v2.11.3 那次 504 事故的现场就是「名字命中、版本停在 2.11.2」。
    assert "want_ver not in versions" in body, (
        "自检没有核对**版本**。只认名字的话，Registry 上挂着旧版本也会放行 —— "
        "而「登记生效了」这句话的全部内容就是那个版本号")


def check_manual_trigger(doc: dict) -> None:
    triggers = _triggers(doc) or {}
    assert "workflow_dispatch" in triggers, (
        "缺 workflow_dispatch：补登记 / 重发就只能改 tag 硬触发")
    tags = ((triggers.get("push") or {}).get("tags")) or []
    assert tags, "缺 tag 触发：发版后清单版本会与 Registry 上的条目悄悄错开"


# ---------------------------------------------------------------------------
# 正向：正常路径没被写坏
# ---------------------------------------------------------------------------
class TestWorkflowIsWellFormed:
    def test_workflow_exists(self):
        assert WORKFLOW.is_file(), (
            f"缺 {WORKFLOW.name} —— 那就只剩笔记本上手工一条发布路径，"
            "而 AUDIT.md 记的正是这个缺口")

    def test_is_valid_yaml_with_the_expected_shape(self):
        doc = _doc()
        assert doc["name"]
        assert "publish" in doc["jobs"]

    def test_oidc_permission_is_present(self):
        check_oidc_permission(_doc())

    def test_uses_oidc_login(self):
        check_oidc_login(_runs(_doc()))

    def test_holds_no_long_lived_credential(self):
        check_no_long_lived_secret(_text())

    def test_publisher_checksum_is_verified_before_execution(self):
        check_checksum_before_execute(_runs(_doc()))

    def test_version_guard_is_present(self):
        check_version_guard(_runs(_doc()))

    def test_validate_runs_before_login(self):
        check_validate_precedes_login(_doc())

    def test_publish_step_exists(self):
        check_publish_present(_doc())

    def test_post_publish_verification_is_exact_match(self):
        check_exact_name_verify(_runs(_doc()))

    def test_publish_tolerates_an_ambiguous_failure(self):
        check_publish_tolerates_ambiguous_failure(_runs(_doc()))

    def test_both_triggers_are_present(self):
        check_manual_trigger(_doc())


# ---------------------------------------------------------------------------
# 负向：故意改坏 → 必须变红
# ---------------------------------------------------------------------------
def _mutant(tmp_path: Path, old: str, new: str) -> Path:
    """把工作流里的 `old` 换成 `new`，落成一个副本。

    `old` 不在原文里就直接失败 —— 否则「变异没改坏任何东西」会让用例
    因为另一个原因变绿或变红，判据就指错了地方（变异存活 ≠ 守卫没牙齿，
    也可能是变异本身没落地）。
    """
    src = _text()
    assert old in src, f"变异锚点不在工作流原文里：{old!r}"
    dst = tmp_path / "publish-mcp.yml"
    dst.write_text(src.replace(old, new, 1), encoding="utf-8")
    return dst


def _mutant_all(tmp_path: Path, old: str, new: str) -> Path:
    """替换**全部**出现处。

    有些锚点在工作流里不止一处（publish 与 verify 都查 `?search=`），
    只改第一处会改到另一个 step，而判据读的是指定 step —— 变异看着落地了，
    被判据读的那份其实没动。
    """
    src = _text()
    assert old in src, f"变异锚点不在工作流原文里：{old!r}"
    dst = tmp_path / "publish-mcp.yml"
    dst.write_text(src.replace(old, new), encoding="utf-8")
    return dst


class TestGuardsHaveTeeth:
    """每一格都是「把它改坏，负责它的判据必须报出来」。"""

    def test_downgrading_oidc_permission_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "id-token: write", "id-token: read")
        with pytest.raises(AssertionError, match="id-token"):
            check_oidc_permission(_doc(p))

    def test_dropping_the_oidc_permission_line_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "  id-token: write\n", "")
        with pytest.raises(AssertionError, match="id-token"):
            check_oidc_permission(_doc(p))

    def test_switching_to_device_flow_login_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "login github-oidc", "login github")
        with pytest.raises(AssertionError, match="oidc"):
            check_oidc_login(_runs(_doc(p)))

    def test_injecting_a_long_lived_token_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "    runs-on: ubuntu-latest",
                    "    runs-on: ubuntu-latest\n    env:\n"
                    "      GITHUB_TOKEN: ${{ secrets.MCP_REGISTRY_TOKEN }}")
        with pytest.raises(AssertionError, match="凭据|token"):
            check_no_long_lived_secret(_text(p))

    def test_inverting_the_checksum_comparison_is_caught(self, tmp_path):
        p = _mutant(tmp_path, '"$expected" != "$actual"',
                    '"$expected" = "$actual"')
        with pytest.raises(AssertionError, match="校验和"):
            check_checksum_before_execute(_runs(_doc(p)))

    def test_removing_the_checksum_step_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "sha256sum", "cat")
        with pytest.raises(AssertionError, match="校验和"):
            check_checksum_before_execute(_runs(_doc(p)))

    def test_removing_the_manifest_version_guard_is_caught(self, tmp_path):
        p = _mutant(tmp_path, '"$declared" != "$packaged"',
                    '"$declared" = "$packaged"')
        with pytest.raises(AssertionError, match="一致性"):
            check_version_guard(_runs(_doc(p)))

    def test_removing_the_tag_version_guard_is_caught(self, tmp_path):
        p = _mutant(tmp_path, '"$declared" != "$tag"', '"$declared" = "$tag"')
        with pytest.raises(AssertionError, match="一致性"):
            check_version_guard(_runs(_doc(p)))

    def test_emptying_the_publish_step_is_caught(self, tmp_path):
        """只把真正的调用换成 echo、步骤名留着 —— 必须照样被抓住。"""
        p = _mutant(tmp_path, "if ./mcp-publisher publish; then",
                    "if echo 'publish skipped'; then")
        with pytest.raises(AssertionError, match="mcp-publisher publish"):
            check_publish_present(_doc(p))

    def test_emptying_the_login_step_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "run: ./mcp-publisher login github-oidc",
                    "run: echo 'login skipped'")
        with pytest.raises(AssertionError, match="mcp-publisher login"):
            check_validate_precedes_login(_doc(p))

    def test_removing_validate_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "run: ./mcp-publisher validate",
                    "run: echo 'validate skipped'")
        with pytest.raises(AssertionError, match="validate"):
            check_validate_precedes_login(_doc(p))

    def test_moving_login_before_validate_is_caught(self, tmp_path):
        """顺序守卫也得有牙齿：只是删掉 validate 不够。

        只测「删掉」的话，把 login 提到 validate 前面这种改法仍然能过 ——
        而那正是这条守卫想防的事。
        """
        doc = _doc()
        steps = doc["jobs"]["publish"]["steps"]
        names = [s.get("name") or "" for s in steps]
        i, j = names.index(VALIDATE_STEP), names.index(LOGIN_STEP)
        steps[i], steps[j] = steps[j], steps[i]
        with pytest.raises(AssertionError, match="顺序"):
            check_validate_precedes_login(doc)

    def test_loosening_exact_match_to_a_bare_count_is_caught(self, tmp_path):
        # 必须替换全部：`s["server"]["name"] == want_name` 里含有
        # `["name"] == want_name` 作子串，只改第一处会落到 publish 那一步，
        # 而判据读的是 verify —— 变异看着落地了，被读的那份其实没动。
        p = _mutant_all(tmp_path, '["name"] == want_name', '["name"]')
        with pytest.raises(AssertionError, match="全名精确匹配"):
            check_exact_name_verify(_runs(_doc(p)))

    def test_removing_the_publish_retry_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "for i in 1 2 3", "for i in 1")
        with pytest.raises(AssertionError, match="重试"):
            check_publish_tolerates_ambiguous_failure(_runs(_doc(p)))

    def test_neutralising_the_ambiguity_recheck_is_caught(self, tmp_path):
        """函数定义还在、调用点被换掉 —— 只搜函数名的判据会照样绿。"""
        p = _mutant(tmp_path, "if already_listed; then", "if false; then")
        with pytest.raises(AssertionError, match="回查|独立"):
            check_publish_tolerates_ambiguous_failure(_runs(_doc(p)))

    def test_dropping_the_version_check_in_verify_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "want_ver not in versions", "False")
        with pytest.raises(AssertionError, match="版本"):
            check_exact_name_verify(_runs(_doc(p)))

    def test_using_the_nonexistent_direct_endpoint_is_caught(self, tmp_path):
        p = _mutant_all(tmp_path, "/v0/servers'", "/v0/servers/$name'")
        with pytest.raises(AssertionError, match="404|不存在"):
            check_exact_name_verify(_runs(_doc(p)))

    def test_removing_the_manual_trigger_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "  workflow_dispatch:          # 手工触发：补登记用\n", "")
        with pytest.raises(AssertionError, match="workflow_dispatch"):
            check_manual_trigger(_doc(p))

    def test_removing_the_tag_trigger_is_caught(self, tmp_path):
        p = _mutant(tmp_path, "    tags: [ 'v*' ]\n", "")
        with pytest.raises(AssertionError, match="tag"):
            check_manual_trigger(_doc(p))
