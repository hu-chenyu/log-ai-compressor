# 贡献指南

面向人类贡献者。AI agent 与自动化工具请看 [`AGENTS.md`](AGENTS.md)
（两者在「不许造数字」「门禁必须可失效」「不许对着基准调参」这三条上是同一套纪律）。

先读一遍 [`AGENTS.md`](AGENTS.md) 再动手 —— 那里写的是**这个仓库的形状**，
本文写的是**流程**。

---

## 1. 环境

```bash
git clone https://github.com/hu-chenyu/log-ai-compressor
cd log-ai-compressor
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements-dev.txt
```

Python 3.11 起（`pyproject.toml` 的 `requires-python` 下限，CI 的最低矩阵档）。

硬依赖只有 PyYAML。`web` / `mcp` / `ai` 是可选 extra，装不上也不影响
core/rules/export 三层与全部核心测试 —— 但 `requirements-dev.txt` 会把它们
都装上，因为要真跑那三层的测试。

**Windows 用户**：`start.bat` 是 Windows 一等入口（不只是开发便利，
CI 的 `windows-3.12` 矩阵也在守它）。它必须保持**纯 ASCII + CRLF**，
中文提示一律放在 Web 界面里 —— cmd.exe 逐字节按控制台代码页解析批处理，
一个非 ASCII 字节就可能让每行开头被吞、程序永远起不来。

## 2. 跑起来看看

```bash
log-ai-compressor run examples/sample_system.log -f json -o report.json
log-ai-compressor verify --report report.json      # 重读原文自校验
python -m pytest -q                                # 全量测试应该十几秒内跑完
python scripts/prove.py                            # 11 条自证主张应全部成立
python scripts/mutation_scorecard.py               # 148 条已知危险变异应全被抓住
```

## 3. 分支与提交

不要在 `main` 上直接提交：

```bash
git checkout -b fix/short-description
```

提交信息用 [Conventional Commits](https://www.conventionalcommits.org/)。
**subject 写为什么，不写什么** —— 「什么」git 已经知道了：

```
fix(bench): 判定按计数比，胜负直接进报告；两个门禁测试本身是错的
fix(ci): 覆盖率是平台相关的 —— 门禁第一次运行就抓到我对口径的错误假设
fix(clustering): 相似度回退只比最近 256 个簇，实测出真实的行序依赖
```

scope 用真实子系统：`core` / `clustering` / `analysis` / `export` / `ci` /
`bench` / `docs`。

## 4. 测试

```bash
python -m pytest -q                                        # 全量
python -m pytest tests/test_clustering.py -q               # 单文件
python -m pytest --cov=log_ai_compressor --cov-report=term-missing
```

三条写测试时的规矩：

1. **每条降级分支配一条「正常路径没被写坏」的对照。**
   降级写成「恒返回空」时测试照样是绿的 —— 没有对照就分不出
   「异常时返回空」和「什么都没干」。

2. **新增门禁/断言必须配一条「故意做坏 → 必须变红」的测试。**
   一个从未曾变红过的门禁等于没有门禁。范式见
   `tests/test_prove_claims_gate.py`。

3. **别用 `skip` 掩盖真失败。** 子集运行导致的 `prove.py` 假失败
   （`_is_full_run` 会把子集误判成全量）有专门的豁免标记，不是让你跳过整条主张。

## 5. 门禁

CI 跑十二道，全部必须在本地先跑通（见 [`AGENTS.md` §4](AGENTS.md#4-ci-的十二道门禁)）。
最常用的三条：

```bash
ruff check log_ai_compressor tests scripts
python scripts/prove.py
python scripts/adversarial_eval.py
```

**不要通过删断言、跳过测试、放宽阈值让 CI 变绿。**
如果你发现某道门禁本身有 bug，修门禁并补上它的可失效性测试 ——
`claim_layering` 扫了 0 个文件却报「通过」就是现成的例子。

## 6. 基准

```bash
python scripts/fetch_datasets.py --all     # Loghub 2.0，约 178 MB，不入库
python scripts/benchmark_loghub.py --no-logslop --no-drain3 --budgets 32000
```

全量 10 个数据集很慢（单是 Hadoop 就要对 18 万行做 4 次完整分析，
实测单次约 194 秒）。迭代时用 `--datasets HPC` 之类的子集。

**跑基准时不要并发跑别的东西。** 分析是纯 CPU 密集的，并发跑测试会把
两边的墙钟时间一起拉长好几倍，最后谁都说不清「这次改动花了多久」。

报告三个口径（交付 `top_n=20` / 同预算 32k / 全量），因为它们回答三个不同的问题，
混着说就是不诚实。判定按**事件种数**比、0.05pp 内记平 —— 报告只有一位小数，
落在舍入误差里的差不是领先。

### 不许对着基准调参

这是最重要的一条。改阈值、改掩码规则、改 `top_n` 默认值来让某个数据集赢，
得到的领先不是领先 —— 你已经把评测集也一起改了。

每次「修好了某数据集」的提交必须**全量重跑 10 个数据集**，并给出
「哪些变了、为什么」。只报告变好的那一个数据集等于隐瞒。

两个数据集各自要求不同的规则、且没有一条全局规则能同时满足时，
**诚实的答案是把这个矛盾写进文档**，不是折中出一个两边都次优的阈值。
（见 [`CHANGELOG.md`](CHANGELOG.md) 里 Proxifier 与 HPC 的那几轮。）

## 7. PR

- 一个 PR 一件事。跨两层（core + 接入层）的重构请拆开。
- 描述里写**为什么现在改**，以及**怎么验证的**（贴门禁输出或基准对比）。
- 动了对外数字（README / docs / CHANGELOG）：说明复现命令，并确认
  `scripts/prove.py` 全绿。`prove.py` 是逐条重算的，文档漂移它必红。
- 门禁有红的且你打算「先合了再说」：那不叫门禁。

## 8. 发版

固定顺序，每一步都有它的理由：

```bash
# 1. 版本号 + CHANGELOG + 提交，推分支，等 CI 绿
# 2. 构建，并立刻记下哈希
python scripts/build_repro.py --verify   # 不用 python -m build（那个每次字节都不同）
sha256sum dist/*

# 2.5 **审计产物本身**（不是源码树）—— 这一步在 2.12.3 之前是不存在的
python scripts/audit_release_artifact.py
#    查四件事：产物里的每个文本文件用措辞门禁那套词表再扫一遍；
#    把 sdist 解包后跑它自带的 scripts/sast_gate.py（自洽性）；
#    产物里的每个文件是否都来自源码树；产物里是否带着构建机的路径。
#    **为什么必须做**：2.12.3 的产物是措辞清理**之前**构建的 ——
#    实测产物里 88 处、38 个文件（含一个真实内部主机名），而仓库当时
#    已经是 0 命中。清理只改了仓库、没重建产物，而 PyPI 不允许覆盖
#    已发布文件，那一版**永远留在那儿**。仓库干净 ≠ 产物干净。
#    **要在仓库里跑**：脚本随 sdist 发布，而 sdist 里没有 .git ⇒
#    拿不到源码树清单时那一项**未执行**（退出码 2 = 无法判定），
#    不构成通过。

# 3. 先传 PyPI —— 必须在推 tag 之前
#    **按完整文件名钉死这两个，不要 `dist/*`**
python -m twine upload \
  dist/log_ai_compressor-${VERSION}-py3-none-any.whl \
  dist/log_ai_compressor-${VERSION}.tar.gz

# 4. 再打 tag、推 tag —— 这一步会触发 Registry 发布
git tag -a vX.Y.Z -m "..."
git push origin vX.Y.Z

# 5. 最后建 GitHub Release，挂同一对产物
# 6. 用公开 API 逐条核实，不看页面渲染
```

- **上传的文件名要写全，不要 `dist/*`。** `dist/` 是累积的 —— 本仓库实测
  一次发布后那里躺着 2.11.1 到 2.12.1 共 **12 个文件**。

  这里先把**实测**和**推演**分开，因为两者很容易被混着说：

  * **实测**（把已在 PyPI 上的 `2.11.1` 原样再传一次）：**退出码 0**，
    而该版本的文件数、`upload_time`、`sha256` **全部未变**；全项目 29 个版本
    仍然每个 2 个文件，没有多出任何东西。也就是说，**内容完全相同的重复
    上传是静默 no-op，不会报错**。
  * **推演**（本轮**没有实测**，别照着这句话下结论）：如果 `dist/` 里某个
    历史产物与 PyPI 上的**内容不同**（例如用不同的 epoch 重打过），这次上传
    会不会被拒、失败发生在第几个文件、后面的新版还会不会继续传 —— 三件事
    都没测过。

  仍然按完整文件名钉死的理由是**确定性与可读性**：这次发的是哪两个文件，
  应该在命令行里一眼看得见；让它取决于 `dist/` 里此刻恰好躺着什么，
  等于把「我发了哪个版本」交给目录内容决定。同理，任何脚本从 `dist/` 挑产物
  都要**按完整文件名钉死**，不要 `glob(...)[0]` —— 目录里躺着历史版本时，
  `[0]` 拿到的是最旧的那个，而它**恰好还是合法的文件名**，所以不会报错。

- **先 PyPI、后 tag。** 推 tag 会触发 `.github/workflows/publish-mcp.yml`，
  而 Registry **会校验清单里那个版本在 PyPI 上确实存在**。顺序反了，
  工作流的每一次重试都拿到同一个 400：

  ```text
  PyPI package 'log-ai-compressor' exists, but version '2.12.1' was not
  found (status: 404). A newly published release can take a moment to
  appear on PyPI. Wait and retry, or publish version '2.12.1' before
  registering it
  ```

  **错因藏在 `errors[0].message` 里，不在步骤名上** —— 步骤只叫 `Publish`，
  而那一步既可能因为上游缺失而红，也可能因为别的原因而红。
  v2.12.1 实测连红 3 次、退出码 1，而 Registry 上 `isLatest` 停在旧版本。
  工作流自带的「重试前先回查目标版本在不在册」防的是**重复提交**，
  防不住**上游根本还不存在** —— 这两件事要分开看。

- **不要为了重跑 workflow 去删 tag。**
  `git push origin --delete vX.Y.Z` 会把该 tag 上的 **GitHub Release 对象
  一起删掉**，而重推同一个 tag **不会**把它带回来。tag 可逆 ≠ 操作可逆：
  本地持有同一个 annotated tag 对象，只能证明**重建得回来**，不能证明
  **外部状态没被改**。v2.12.1 实测：tag 逐位还原了（`6879b6f… → 6879b6f…`），
  而发布页变成 404，只能整个重建（标题 + 7,124 字符正文 + 两个附件重传）。
  要重跑用 `workflow_dispatch`（该工作流已开）或 Actions 页的
  Re-run failed jobs，这两条都不碰 tag。

- **三方 sha256 必须逐字节相同**：本地构建产物 == GitHub Release 附件 digest
  == PyPI 的 `digests` 字段。用
  `GET /repos/{owner}/{repo}/releases/tags/{tag}` 拿附件的 `digest`，
  不靠页面底部 Assets 区块是否渲染出来（那是懒加载 fragment，
  在很多客户端里长期转圈，**看不到不等于没上传**）。
- 发布后改已推送内容 = 换版本号重发，不要原地修。已发到 PyPI 的产物
  不可覆盖，所以更正只能**再发一版**（v2.12.0 → v2.12.1 就是这么来的）。
- 这份清单本身也是一条**对外流程主张**，同样受 §2.1 约束：它的顺序
  是被一次真实事故改出来的（v2.12.1 的 Registry 三连 400），不是推演出来的。
  再改它之前先问「这次事故的日志说了什么」，而不是「哪一步看着更顺」。

## 9. 撤回

这个仓库撤回过多次**发布数字**与**修法**（v2.7.0 / v2.9.5 / v2.9.6 / v2.9.8 /
v2.9.9 各有记述，完整清单见 [`CHANGELOG.md`](CHANGELOG.md) 里标着「撤回」的条目）。
发现对外数字错了而修不好，正确做法是**撤回并写清原因**，不是换个措辞让它
看着没那么糟。

值得注意的是其中两次的性质不同，值得分开看：

- **撤回数字**（v2.7.0 的 Hadoop 91.4%、v2.9.6 里对 v2.9.4 基准的撤回）——
  数字本身错了或不可复现。
- **撤回修法**（v2.9.8 的「字面词元守卫」）—— 数字在涨，但产品的交付口径
  崩了一半。**指标变好不等于产品变好**，这类撤回更容易被忽略。

撤回记录**不要删改**。后来的版本在 CHANGELOG 里追加
「此前发布的 X 已撤回，原因是 Y」，让读者看到纠正过程本身。
一个会公开撤回自己招牌数字的项目，它剩下的数字才有分量。

## 10. 代码风格

- `ruff` 管，配置在 `pyproject.toml`；行长上限 110。
- 行尾按 `.gitattributes`：`*.bat` 强制 CRLF，其余源码与文档统一 LF。
  改完用**二进制读**复核 —— `read_text()` 会走通用换行把 CRLF 归一成 LF，
  自检会得到一个百分之百确定的假结论。
- 公开函数写中文 docstring，说明**为什么这样设计**，不是复述函数名。
  这个仓库的注释密度高是有意的：很多决策的约束条件不写在代码里就没人知道。
- 类型注解齐全（`py.typed` 已在包里），新增公开函数请补上。

---

## 相关文档

[`README.md`](README.md) · [`AGENTS.md`](AGENTS.md) · [`CHANGELOG.md`](CHANGELOG.md) ·
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) ·
[`docs/BENCHMARK.md`](docs/BENCHMARK.md) · [`docs/POSITIONING.md`](docs/POSITIONING.md) ·
[`docs/FAQ.md`](docs/FAQ.md)
