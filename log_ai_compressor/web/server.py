# -*- coding: utf-8 -*-
"""本地 Web 服务：REST + SSE + 静态界面。

安全边界
--------
本服务只绑定 127.0.0.1，且只读用户自己机器上的文件。
- 没有鉴权是有意的：它等价于「本机进程」—— 但这个等价**成立的前提是
  服务端校验 Host 头**，不是浏览器同源策略。同源策略挡不住 DNS rebinding：
  攻击者把自己控制的 evil.test 解析到 127.0.0.1 后，浏览器发的是**同源**
  请求（同源策略因此放行），可 Host 头是 evil.test，服务端若不校验就照样
  应答。实测（2026-10-18 审计）：不校验时 `Host: evil.attacker.example` 与
  `Host: localhost.evil.test:9999` 都返回 200 与完整负载。
  所以边界是「**回环绑定 + Host 校验**」两道，缺一道「只有本机能访问」就不成立。
  见下方 `_host_allowed`。
- 文件浏览接口**默认**从用户主目录起步，但范围是**可选**的：设了
  LOG_AI_FS_ROOT 才限定在该根目录内，没设就**刻意不限范围**（理由见
  下方「关于范围限制」注释，它同时管住 /api/fs/list 与 /api/fs/file）。
- 分析接口接受任意绝对路径 —— 用户是显式指定自己要分析哪个文件的。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from log_ai_compressor import __app_name__, __version__, service as S
from log_ai_compressor.web import jobs as J

STATIC_DIR = Path(__file__).parent / "static"

# Host 白名单的默认值。只认这三个回环写法；端口部分会先剥掉。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# `--allow-remote` 的显式让步：调用方传 ("*",) 表示「用户已知情并主动放开」，
# 此时不再校验 Host。见 serve()。
_ALLOW_ANY_HOST = "*"


def _host_name(value: str) -> str:
    """从 Host 头里取出主机名，剥掉端口（IPv6 的 `[::1]:8765` 也要对）。"""
    value = value.strip()
    if value.startswith("["):                     # [::1]:8765
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if value.count(":") == 1:                     # 127.0.0.1:8765
        return value.rsplit(":", 1)[0]
    return value                                  # 裸 IPv6 ::1


def _host_allowed(host_header: Optional[str], allowed: Iterable[str]) -> bool:
    """这个 Host 头值在不在允许名单里。"""
    allowed = {a.strip().lower() for a in allowed}
    if _ALLOW_ANY_HOST in allowed:
        return True
    # 不带 Host（HTTP/1.0 客户端、进程内测试）不拦 —— 没有可伪造的来源。
    if not host_header:
        return True
    name = _host_name(host_header).lower()
    return name in allowed

# 文件浏览器的默认起点是用户主目录（范围限制见下方 FS_ROOT 说明）
HOME = Path(os.path.expanduser("~"))
# 大文件自动提示阈值
BIG_FILE_MB = 64

# 修复缺陷#5：粘贴文本上限（字符数）。文件模式是流式处理、没有上限的
# 必要；粘贴模式整段文本随 HTTP 请求体一次性进内存，不限长等于把本机
# 服务白送给一个恶意/失控的请求体 —— 超限直接 400 并指路「用文件导入」。
MAX_PASTE_CHARS = 20 * 1024 * 1024


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    """一次分析请求。三种模式由 mode 区分。"""
    mode: str = Field("file", description="file / text / compare")
    paths: List[str] = Field(default_factory=list)
    text: str = ""
    params: Dict[str, Any] = Field(default_factory=dict)


class ExportRequest(BaseModel):
    job_id: str
    format: str = "md"
    redact: bool = False
    custom_rules: List[str] = Field(default_factory=list)
    top_n: Optional[int] = None


# ---------------------------------------------------------------------------
# 文件浏览（本地工具不需要上传，直接让用户挑本机文件）
# ---------------------------------------------------------------------------
# 关于范围限制：这里**刻意不限主目录**。早期版本锁死在用户主目录，结果
# 三个问题：(1) 日志放在 D:\ / /var/log 的用户完全用不了浏览器；
# (2) CI 检出目录本来就不在 HOME 里，测试在所有 runner 上都挂；
# (3) 安全收益近乎为零 —— 服务只监听 127.0.0.1，且 /api/analyze 本来就
# 接受任意路径，锁浏览器并不构成任何真正的边界。
# 真要收紧，用 LOG_AI_FS_ROOT 环境变量限定一个根目录。
FS_ROOT = os.environ.get("LOG_AI_FS_ROOT") or None


def _fs_entry(p: Path) -> Dict[str, Any]:
    try:
        st = p.stat()
        is_dir = p.is_dir()
        size = 0 if is_dir else st.st_size
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
    except OSError:
        # 权限不足 / 条目在列举后被删除 / 路径过长。mtime 必须在同一个
        # try 里取：此前它在返回字典里单独 stat 一次，条目刚好消失时
        # 这个二次调用会抛 OSError，把整个目录列举变成 500。
        is_dir, size, mtime = p.is_dir(), 0, ""
    return {
        "name": p.name,
        "path": str(p),
        "is_dir": is_dir,
        "size": size,
        "size_text": "" if is_dir else _human_size(size),
        "mtime": mtime,
    }


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def _list_dir(target: str) -> Dict[str, Any]:
    path = Path(target).expanduser() if target else (Path(FS_ROOT) if FS_ROOT else HOME)
    if not path.is_absolute():
        path = (Path(FS_ROOT) / path) if FS_ROOT else (HOME / path)
    if FS_ROOT and not _within_root(path):
        raise HTTPException(400, f"只能浏览 {FS_ROOT} 内的路径")
    if not path.exists():
        raise HTTPException(404, f"路径不存在：{path}")
    if not path.is_dir():
        path = path.parent
    try:
        items = sorted(
            (_fs_entry(p) for p in path.iterdir()),
            key=lambda e: (not e["is_dir"], e["name"].lower()),
        )
    except PermissionError:
        raise HTTPException(403, f"无权读取：{path}")
    except OSError as exc:
        raise HTTPException(400, f"读取失败：{exc}")
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else "",
        "can_go_up": not (FS_ROOT and not _within_root(path.parent)),
        "home": str(HOME),
        "fs_root": FS_ROOT or "",
        "entries": items,
    }


def _within_root(path: Path) -> bool:
    """仅当设置了 LOG_AI_FS_ROOT 时才生效。"""
    if not FS_ROOT:
        return True
    try:
        path.resolve().relative_to(Path(FS_ROOT).resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------
def create_app(*, allowed_hosts: Optional[Iterable[str]] = None) -> FastAPI:
    """构造应用。

    ``allowed_hosts`` 默认只认回环写法。传 ``("*",)`` 是 ``--allow-remote``
    的显式让步（用户已知情并主动把服务暴露到网络上）。
    """
    hosts = tuple(allowed_hosts) if allowed_hosts is not None else _LOOPBACK_HOSTS
    app = FastAPI(
        title=f"{__app_name__} 本地服务",
        version=__version__,
        description="本地日志分析取证台：数据不出网",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # ---- Host 白名单 ----------------------------------------------------
    # 「只绑回环」单独**不足以**推出「只有本机能访问」：DNS rebinding 下
    # 远端页面发的是同源请求，同源策略放行，Host 头却是攻击者的域名。
    # 这一层与回环绑定是**两道独立的门**，少一道边界就不成立。
    @app.middleware("http")
    async def _guard_host(request: Request, call_next):
        host_header = request.headers.get("host")
        if not _host_allowed(host_header, hosts):
            return JSONResponse(
                {"ok": False,
                 "error": f"拒绝非本机 Host：{host_header!r}。"
                          "本服务只接受来自本机浏览器的请求。"},
                status_code=403,
            )
        return await call_next(request)

    # ---- 健康检查 / 元信息 ---------------------------------------------
    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        from log_ai_compressor.rules.engine import list_presets
        return {
            "ok": True,
            "app": __app_name__,
            "version": __version__,
            "home": str(HOME),
            "rules": list_presets(),
            "ai": _ai_status(),
        }

    # ---- 文件浏览 -------------------------------------------------------
    @app.get("/api/fs/list")
    def fs_list(path: str = Query("", description="目录绝对路径，空=主目录")):
        return _list_dir(path)

    @app.get("/api/fs/file")
    def fs_file(path: str = Query(..., description="文件绝对路径")):
        """单文件信息：用于输入框自动补全与大小提示。"""
        p = Path(path).expanduser()
        # 与 _list_dir 同一个闸门。此前只有 /api/fs/list 受 LOG_AI_FS_ROOT
        # 约束，这个接口绕开它 —— 旋钮存在却不同时管住两个接口，等于名不副实。
        # 顺序：先查根、再查存在性。反过来会泄露「根外路径是否存在」。
        if FS_ROOT and not _within_root(p):
            raise HTTPException(400, f"只能访问 {FS_ROOT} 内的文件")
        if not p.is_file():
            raise HTTPException(404, f"文件不存在：{p}")
        info = _fs_entry(p)
        info["big"] = info["size"] >= BIG_FILE_MB * 1024 * 1024
        return info

    # ---- 分析任务 -------------------------------------------------------
    @app.post("/api/analyze")
    def analyze(req: AnalyzeRequest):
        mode = (req.mode or "file").lower()
        if mode not in ("file", "text", "compare"):
            raise HTTPException(400, f"未知模式：{req.mode}")
        # 先干跑一次参数校验 + 文件存在性检查，把 4xx 立刻返回，
        # 别让用户开着一个 SSE 连接等半天才看到「文件不存在」
        try:
            S.normalize_params(req.params)
            if mode == "text":
                if not (req.text or "").strip():
                    raise HTTPException(400, "请先粘贴日志文本")
                # 修复缺陷#5：粘贴体大小上限（文件导入无此限制）
                if len(req.text) > MAX_PASTE_CHARS:
                    raise HTTPException(
                        400, f"粘贴文本超过 {MAX_PASTE_CHARS // (1024 * 1024)} MB 上限，"
                             f"请改用「文件导入」模式（超大文件是流式处理的）")
            else:
                paths = [p.strip() for p in req.paths if p.strip()]
                if mode == "compare":
                    if len(paths) < 2:
                        raise HTTPException(400, "对比分析至少需要 2 个日志文件")
                elif not paths:
                    raise HTTPException(400, "请先选择日志文件")
                missing = [p for p in paths if not Path(p).is_file()]
                if missing:
                    shown = "、".join(missing[:3])
                    raise HTTPException(
                        400, f"文件不存在：{shown}" + ("…" if len(missing) > 3 else ""))
        except S.ServiceError as exc:
            raise HTTPException(400, str(exc))
        job = J.start_job(mode, {"paths": req.paths, "text": req.text,
                                 "params": req.params})
        return {"job_id": job.id, "mode": mode}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = J.REGISTRY.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在或已被淘汰（请重新分析）")
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/stream")
    def job_stream(job_id: str):
        job = J.REGISTRY.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在或已被淘汰（请重新分析）")
        return EventSourceResponse(J.stream_events(job))

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str):
        job = J.REGISTRY.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        job.cancel_event.set()
        return {"cancelled": True}

    @app.post("/api/verify")
    def verify(req: Dict[str, Any] = Body(...)):
        """重读原始日志，核对本次分析引用的每一条证据出处。

        Web 界面的「复核证据」按钮。存在的理由是取证层的核心主张在
        浏览器里也必须**当场可验**：用户不能只在命令行跑 verify 才知道
        自己的日志有没有被改过 —— 否则「可复核」只是一句 README 里的
        话。job.result 是内存中的 AnalysisResult，直接交给 service 层
        复用同一套 verify_refs，不另写一份校验逻辑。
        """
        job = J.REGISTRY.get(str(req.get("job_id") or ""))
        if job is None or job.result is None:
            raise HTTPException(404, "结果不存在或已被淘汰（请重新分析）")
        try:
            return S.verify_result(job.result)
        except Exception as exc:                        # noqa: BLE001
            raise HTTPException(500, f"复核失败：{exc}")

    # ---- 导出 -----------------------------------------------------------
    def _export_common(req: ExportRequest) -> tuple:
        job = J.REGISTRY.get(req.job_id)
        if job is None or job.result is None:
            raise HTTPException(404, "结果不存在或已被淘汰（请重新分析）")
        fmt = (req.format or "md").lower()
        try:
            if job.mode == "compare":
                text = job.payload_out.get("markdown", "") if job.payload_out else ""
                if req.redact:
                    text = S.redact_text(text, req.custom_rules or None)
            else:
                text = S.export_text(
                    job.result, fmt, top_n=req.top_n,
                    redact=req.redact, custom_rules=req.custom_rules)
        except S.ServiceError as exc:
            raise HTTPException(400, str(exc))
        return fmt, text

    @app.post("/api/export")
    def export(req: ExportRequest):
        fmt, text = _export_common(req)
        return Response(
            content=text,
            media_type=S.media_type(fmt),
            headers={"Content-Disposition":
                     f'attachment; filename="log-report.{fmt}"'},
        )

    @app.post("/api/export/copy")
    def export_copy(req: ExportRequest):
        """纯文本回传：前端一键复制用（不触发下载）。"""
        fmt, text = _export_common(req)
        return {"format": fmt, "text": text, "length": len(text)}

    # ---- AI 解读 --------------------------------------------------------
    @app.get("/api/ai/status")
    def ai_status():
        """前端 api() 默认发 GET，必须是 GET 路由。"""
        return _ai_status()

    @app.post("/api/ai/config")
    def ai_config_save(req: Dict[str, Any] = Body(default_factory=dict)):
        """保存 AI 服务商配置。

        安全约束：**绝不在响应里回显明文 Key**；请求里 key 为空表示
        「沿用已保存的 Key 或环境变量」，而不是清空它。
        """
        from log_ai_compressor.ai.config import load_config, save_config
        current = load_config()
        merged = {
            "provider": req.get("provider", current.provider),
            "base_url": req.get("base_url", current.base_url),
            "model": req.get("model", current.model),
            # 空 key 不覆盖：避免前端不回显明文导致用户一点保存就丢 Key
            "api_key": req.get("api_key") or current.api_key,
            "temperature": req.get("temperature", current.temperature),
            "max_tokens": req.get("max_tokens", current.max_tokens),
            "timeout": req.get("timeout", current.timeout),
        }
        try:
            cfg = load_config(merged)
            save_config(cfg)
        except Exception as exc:                      # noqa: BLE001
            raise HTTPException(400, f"配置无效：{exc}")
        return _ai_status()

    @app.post("/api/ai/explain")
    def ai_explain(req: Dict[str, Any] = Body(...)):
        from log_ai_compressor.ai import LLMError, explain_job
        try:
            return explain_job(req)
        except LLMError as exc:
            # AI 不可用是配置问题（未启用/Key 错），属 4xx，别让前端当崩溃
            raise HTTPException(400, str(exc))

    # ---- 静态界面 -------------------------------------------------------
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True),
                  name="static")
    else:                                            # pragma: no cover
        @app.get("/")
        def _missing_ui():
            raise HTTPException(500, f"前端资源缺失：{STATIC_DIR}")

    return app


def _ai_status() -> Dict[str, Any]:
    """AI 层可用性：未配置时明确告诉前端「不可用」而不是报错。"""
    try:
        from log_ai_compressor.ai import describe_config
        return describe_config()
    except Exception as exc:                          # noqa: BLE001
        return {"available": False, "reason": f"AI 模块未就绪：{exc}"}


def _port_in_use(host: str, port: int) -> bool:
    """端口是否真的有人在用（v2.8.1 修 Windows 探测失效）。

    此前是「bind + SO_REUSEADDR 探测」，那是错的：**Windows 上
    SO_REUSEADDR 允许第二个 socket 绑到已被监听的端口** —— 实测第一个
    socket 已经 `listen`，第二个照样 `bind` 成功。于是探测永远返回「空闲」，
    `pick_port` 原样返回被占的端口，真正 bind 的 uvicorn 再抛一个
    「address already in use」。

    而「双击 start.bat、上一次残留进程还占着 8765」**正是本函数存在的理由**，
    且 Windows 恰恰是 start.bat 的一等入口平台 —— 也就是说这个探测在它
    唯一重要的平台上一直是坏的。

    改成两道：先真连一下（有人在监听就连得上），再裸 bind 一次
    （占了但没监听的端口 bind 会失败）。裸 bind **不加** SO_REUSEADDR。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        if s.connect_ex((host, port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))       # 刻意不设 SO_REUSEADDR
        except OSError:
            return True
    return False


def pick_port(host: str, port: int, tries: int = 20) -> int:
    """找一个空闲端口：双击启动时 8765 常被上一次残留进程占着。

    逐个往后试，用真连接探测而不是靠运气；全被占则抛 OSError。
    """
    for offset in range(max(1, tries)):
        candidate = port + offset
        if not _port_in_use(host, candidate):
            return candidate
    raise OSError(f"{port}~{port + tries - 1} 端口都被占用，请用 --port 指定其它端口")


def serve(host: str = "127.0.0.1", port: int = 8765, *,
          open_browser: bool = True, log_level: str = "warning",
          auto_port: bool = True) -> int:
    """启动服务（CLI / 启动器入口）。

    Args:
        host: 绑定地址。默认只监听回环，不对外暴露。
        port: 起始端口；auto_port 为真时占用会自动顺延
        open_browser: 启动后自动打开浏览器
        log_level: uvicorn 日志级别
        auto_port: 端口被占时是否自动往后找

    Returns:
        实际监听的端口
    """
    import uvicorn

    if auto_port:
        port = pick_port(host, port)

    # 只绑回环时 Host 校验才有意义（配合它构成两道门）；绑到非回环说明调用方
    # 已经过 CLI 的 --allow-remote 显式确认，这时再拦 Host 只会让用户够不着
    # 自己的服务，所以放行 —— 这一步的知情同意在 CLI 层，不在这里。
    bound = _host_name(host).lower()
    is_loopback = bound in _LOOPBACK_HOSTS or bound.startswith("127.")
    allowed = None if is_loopback else (_ALLOW_ANY_HOST,)

    if open_browser:
        import threading
        import webbrowser

        url = f"http://{host}:{port}/"

        def _open() -> None:
            # 等 uvicorn 真正 bind 完再开，否则浏览器会先撞上
            # ERR_CONNECTION_REFUSED，看起来像"服务没起来"
            for _ in range(40):
                time.sleep(0.25)
                import socket as _s
                with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as probe:
                    if probe.connect_ex((host, port)) == 0:
                        break
            try:
                webbrowser.open(url)
            except Exception:                          # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    print(f"日志AI压缩器 v{__version__} · 本地服务已启动", flush=True)
    print(f"  界面：  http://{host}:{port}/", flush=True)
    print(f"  API 文档：http://{host}:{port}/api/docs", flush=True)
    print("  数据不出网，按 Ctrl+C 停止", flush=True)
    uvicorn.run(create_app(allowed_hosts=allowed), host=host, port=port,
                log_level=log_level)
    return port
