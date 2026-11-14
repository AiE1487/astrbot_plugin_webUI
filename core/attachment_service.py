"""附件服务：上传保存、类型白名单、大小限制、文本抽取与索引（M3）。

契约（对接契约 §附件元数据 / §REST upload、files；UserPlan §3.4）：
- **必须** ``Path(upload.filename).name`` 防目录穿越；
- 类型白名单：图片 png/jpg/jpeg/webp/gif（扩展名判定）；文本按配置
  ``attachment_text_whitelist``（实时读，热生效）；其余拒收 -> 415；
- 图片走 ``upload.save(path, max_bytes=upload_image_max_mb)``，超限宿主抛
  ``UploadTooLargeError`` -> 413 拒绝，**图片永不截断**（v4.29.4 api/web.py
  L114-126 实证 save 签名：超限时移除半截文件并抛错，返回写入字节数）；
- 文本不按 max_bytes 拒收（否则无法"截断"）：落盘套 16MB 硬上限（契约外防
  磁盘滥用，超出 413），**注入 prompt 的内容**超 ``upload_text_max_kb``（默认
  200KB）截断并置 ``truncated=true``；
- 存储 ``uploads/<yyyyMM>/<file_id>_<安全化name>``，``file_id = f_<uuid4hex>``
  与磁盘文件名同源 uuid；索引 ``<data_dir>/uploads/index.json`` 维护
  file_id -> (相对路径, kind, mime, 引用 cid, created_at)；服务器本地绝对路径
  **永不出现在任何响应中**（files/<file_id> 与级联删除都经索引定位）；
- 生命周期：upload 即登记（未引用）；被 send 引用后绑定 cid；删除会话级联
  删文件与索引；未被引用孤儿 24h 后由 asyncio 后台任务清理（main.terminate
  时取消该任务）；
- 图片 preview：PIL 生成 <=256px webp 缩略 data URL（真机宿主有 Pillow；无
  PIL 时降级为 None，前端按契约回退 ``files/<file_id>``）。
"""

import asyncio
import base64
import io
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import PluginUploadFile


def _retry_file_io(func, attempts: int = 4, delay: float = 0.02):
    """Windows 瞬时共享冲突重试（AV/索引服务短暂锁定刚被 os.replace 的文件）。

    仅对 PermissionError 重试（FileNotFoundError 是合法结果，不重试）。
    """
    for attempt in range(attempts):
        try:
            return func()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)

try:  # 宿主 v4.29.4: astrbot/core/utils/upload.py；桩环境无宿主时用兜底定义
    from astrbot.core.utils.upload import UploadTooLargeError
except ImportError:  # pragma: no cover —— 仅桩环境
    class UploadTooLargeError(Exception):
        """stub 兜底：与宿主 UploadTooLargeError 同名（isinstance 判定用类型名）。"""


UPLOAD_DIR_NAME = "uploads"
INDEX_FILE_NAME = "index.json"
INDEX_VERSION = 1

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
TEXT_MIME = {
    "txt": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "json": "application/json",
    "csv": "text/csv",
    "py": "text/x-python",
    "yaml": "text/yaml",
    "yml": "text/yaml",
    "log": "text/plain",
}

PREVIEW_TEXT_CHARS = 500
PREVIEW_THUMB_SIZE = 256
TEXT_SAVE_HARD_CAP = 16 * 1024 * 1024  # 文本落盘硬上限（契约外防滥用，超出 413）

ORPHAN_MAX_AGE_HOURS = 24.0
CLEANUP_INTERVAL_SECONDS = 3600.0


class UnsupportedMediaTypeError(Exception):
    """类型不在白名单（routes -> 415 unsupported_media_type）。"""


class InvalidFilenameError(ValueError):
    """文件名缺失或非法（routes -> 400 invalid_request）。"""


def _config_int(config: dict, key: str) -> int:
    """实时读配置 int（键由 _conf_schema.json 生成恒存在；默认值单一来源 = schema）。"""
    return max(1, int(config[key]))


def _config_str_list(config: dict, key: str) -> list[str]:
    """实时读配置字符串列表（list 原样；str 按逗号拆分容错）。"""
    value = config[key]
    if isinstance(value, str):
        return [item.strip().lower() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    return []


def classify_upload(
    filename: str, content_type: str = "", text_whitelist: list[str] | None = None
) -> str:
    """判定上传文件类型。

    Args:
        filename: 已安全化的文件名（调用方先做 ``Path(...).name``）。
        content_type: multipart 的 content-type（辅助信息，不作为放行依据）。
        text_whitelist: 文本附件后缀白名单（配置 ``attachment_text_whitelist``，
            不含点号小写）；None 时不放行任何文本类型。

    Returns:
        ``"image"`` / ``"text"`` / ``"unknown"``（unknown 由调用方拒收 415）。
        图片以扩展名判定（png/jpg/jpeg/webp/gif）；文本以后缀白名单为准。
    """
    ext = Path(filename or "").suffix.lower().lstrip(".")
    if ext in IMAGE_EXTENSIONS:
        return "image"
    whitelist = {item.lstrip(".").lower() for item in (text_whitelist or [])}
    if ext and ext in whitelist:
        return "text"
    return "unknown"


def sanitize_filename(filename: str) -> str:
    """目录穿越防御：仅取 ``Path(...).name``；为空抛 InvalidFilenameError。"""
    safe = Path(filename or "").name.strip()
    if not safe or safe in (".", ".."):
        raise InvalidFilenameError("文件名缺失或非法")
    return safe


def uploads_root(data_dir: Path) -> Path:
    """附件存储根目录：``<data_dir>/uploads/``。"""
    return Path(data_dir) / UPLOAD_DIR_NAME


def index_file(data_dir: Path) -> Path:
    """附件索引文件：``<data_dir>/uploads/index.json``（原子写）。"""
    return uploads_root(data_dir) / INDEX_FILE_NAME


def _empty_index() -> dict:
    return {"version": INDEX_VERSION, "files": {}}


def _read_index_sync(path: Path) -> dict:
    try:
        raw = _retry_file_io(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_index()
    except OSError as exc:
        logger.warning(f"[webui] 附件索引读取失败（按空索引继续，不动原文件）{path}: {exc}")
        return _empty_index()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(f"[webui] 附件索引 JSON 损坏（按空索引继续，不动原文件）{path}: {exc}")
        return _empty_index()
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), dict):
        logger.warning(f"[webui] 附件索引结构不符（按空索引继续）{path}")
        return _empty_index()
    return doc


def _write_index_sync(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _retry_file_io(
            lambda: tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        )
        _retry_file_io(lambda: os.replace(tmp, path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


async def _load_index(data_dir: Path) -> dict:
    return await asyncio.to_thread(_read_index_sync, index_file(data_dir))


async def _save_index(data_dir: Path, doc: dict) -> None:
    await asyncio.to_thread(_write_index_sync, index_file(data_dir), doc)


# 索引读-改-写串行化锁：store_upload/bind_file/delete_*/sweep_orphans 的
# load-modify-save 全程持锁，防止并发丢条目（与 uistore 的 per-cid 锁同思路）
_INDEX_LOCK = asyncio.Lock()


def _abs_disk_path(data_dir: Path, meta: dict) -> Path:
    """索引存相对路径（uploads/<yyyyMM>/<file_id>_<name>），绝对路径仅内部使用。"""
    return Path(data_dir) / str(meta.get("path", ""))


def public_meta(meta: dict) -> dict:
    """索引条目 -> 契约附件元数据（剥离 path/cid/created_at 等内部字段）。

    ``truncated`` 仅 text 携带（图片永不置 true，按契约省略该键）。
    """
    out = {
        "file_id": meta["file_id"],
        "kind": meta["kind"],
        "name": meta["name"],
        "size": int(meta.get("size", 0)),
        "mime": meta["mime"],
    }
    if meta.get("preview") is not None:
        out["preview"] = meta["preview"]
    if meta.get("kind") == "text":
        out["truncated"] = bool(meta.get("truncated", False))
    return out


def _image_preview(path: Path) -> str | None:
    """PIL 生成 <=256px webp 缩略 data URL；无 PIL/webp 支持时逐级降级。"""
    try:
        from PIL import Image  # noqa: PLC0415 —— 延迟导入（真机宿主有 Pillow）
    except Exception:  # noqa: BLE001
        return None
    try:
        with Image.open(path) as img:
            img.thumbnail((PREVIEW_THUMB_SIZE, PREVIEW_THUMB_SIZE))
            for fmt, mime in (("WEBP", "image/webp"), ("JPEG", "image/jpeg"), ("PNG", "image/png")):
                try:
                    buf = io.BytesIO()
                    frame = img.convert("RGB") if fmt == "JPEG" else img
                    frame.save(buf, format=fmt)
                    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception:  # noqa: BLE001 —— 逐格式降级
                    continue
        return None
    except Exception as exc:  # noqa: BLE001 —— 图片损坏等
        logger.warning(f"[webui] 图片缩略图生成失败（preview 置空，前端回退 files 端点）{path}: {exc}")
        return None


def _decode_text(data: bytes) -> str:
    """解码策略：UTF-8 严格 -> 尾部裁剪重试（切片边界）-> GBK -> UTF-8 replace 兜底。

    裁剪重试先于 GBK：避免把按字节截断的 UTF-8 内容误判成 GBK 产生 mojibake。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for trim in (1, 2, 3):
        try:
            return data[:-trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("gbk")
    except UnicodeDecodeError:
        pass
    return data.decode("utf-8", errors="replace")


def extract_text_content(path: Path, max_kb: int = 200) -> tuple[str, bool]:
    """读取文本附件内容（UTF-8 优先、GBK/替换符容错），超 max_kb 截断。

    Returns:
        ``(content, truncated)``：content 为解码后文本（超限时仅取前 max_kb
        KB 字节再解码，多字节截断以尾部裁剪/替换符兜底）；truncated 表示磁盘
        内容超过 max_kb（即注入 prompt 的内容被截断）。
    """
    raw = path.read_bytes()
    limit = max(1, int(max_kb)) * 1024
    truncated = len(raw) > limit
    data = raw[:limit] if truncated else raw
    return _decode_text(data), truncated


def build_attachment_block(name: str, content: str) -> str:
    """构造注入 prompt 的附件区块：``<attachment name="...">\\n内容\\n</attachment>``。"""
    return f'<attachment name="{name}">\n{content}\n</attachment>'


async def store_upload(upload: PluginUploadFile, data_dir: Path, config: dict | None = None) -> dict:
    """保存并登记一个上传文件，返回契约附件元数据。

    流程：防穿越安全化文件名 -> classify（unknown -> 415）-> 按类型落盘
    （图片 max_bytes=图片上限，超限 413；文本 16MB 硬上限）-> 抽取文本内容/
    生成缩略 preview -> 写索引（未引用态）。

    Raises:
        InvalidFilenameError: 文件名缺失/非法（-> 400）。
        UnsupportedMediaTypeError: 类型不在白名单（-> 415）。
        UploadTooLargeError: 图片超限或文本超 16MB 硬上限（-> 413）。
    """
    config = config or {}  # 键由 _conf_schema.json 生成恒存在；缺键即配置错误，直接 KeyError
    safe_name = sanitize_filename(getattr(upload, "filename", "") or "")
    kind = classify_upload(
        safe_name,
        str(getattr(upload, "content_type", "") or ""),
        _config_str_list(config, "attachment_text_whitelist"),
    )
    if kind == "unknown":
        raise UnsupportedMediaTypeError(f"不支持的文件类型: {safe_name}")

    file_id = f"f_{uuid.uuid4().hex}"
    month_dir = uploads_root(data_dir) / datetime.now(timezone.utc).strftime("%Y%m")
    disk_path = month_dir / f"{file_id}_{safe_name}"

    if kind == "image":
        max_bytes = _config_int(config, "upload_image_max_mb") * 1024 * 1024
    else:
        max_bytes = TEXT_SAVE_HARD_CAP  # 文本不按注入上限拒收，落盘仅设硬上限
    try:
        size = await upload.save(disk_path, max_bytes=max_bytes)
    except Exception as exc:  # noqa: BLE001 —— 按类型名兜底识别（导入路径未真机核实）
        if isinstance(exc, UploadTooLargeError) or type(exc).__name__ == "UploadTooLargeError":
            raise UploadTooLargeError(str(exc)) from exc
        raise

    truncated = False
    preview = None
    if kind == "image":
        preview = await asyncio.to_thread(_image_preview, disk_path)
        mime = IMAGE_MIME.get(Path(safe_name).suffix.lower().lstrip("."), "application/octet-stream")
    else:
        # 同步文件 IO（可达 16MB）放线程池，避免阻塞事件循环拖慢挂着的 SSE 流
        content, truncated = await asyncio.to_thread(
            extract_text_content,
            disk_path,
            _config_int(config, "upload_text_max_kb"),
        )
        preview = content[:PREVIEW_TEXT_CHARS]
        mime = TEXT_MIME.get(Path(safe_name).suffix.lower().lstrip("."), "text/plain")

    meta = {
        "file_id": file_id,
        "kind": kind,
        "name": safe_name,
        "size": int(size),
        "mime": mime,
        "preview": preview,
        "truncated": truncated,
        # 内部字段：相对路径（绝不进响应；统一 POSIX 分隔符保证跨解释器/平台可移植，
        # Windows 原生 pathlib 的 str() 会产出反斜杠）、引用 cid、登记时间
        "path": disk_path.relative_to(Path(data_dir)).as_posix(),
        "cid": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        index["files"][file_id] = meta
        await _save_index(data_dir, index)
    logger.info(f"[webui] 附件已登记 file_id={file_id} kind={kind} size={size}")
    return public_meta(meta)


async def get_file(data_dir: Path, file_id: str) -> dict | None:
    """按 file_id 取索引条目（含内部字段 path/cid；不存在返回 None）。"""
    index = await _load_index(data_dir)
    meta = index["files"].get(file_id)
    return dict(meta) if meta else None


async def bind_file(data_dir: Path, file_id: str, cid: str) -> bool:
    """send 引用后绑定会话（首次引用生效；已绑定其他 cid 不改写）。"""
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        meta = index["files"].get(file_id)
        if meta is None:
            return False
        if meta.get("cid") != cid:
            meta["cid"] = meta.get("cid") or cid
            await _save_index(data_dir, index)
    return True


async def delete_file(data_dir: Path, file_id: str) -> bool:
    """删除单个附件（磁盘文件 + 索引条目）。"""
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        meta = index["files"].pop(file_id, None)
        if meta is None:
            return False
        await _save_index(data_dir, index)
    await asyncio.to_thread(_unlink_quiet, _abs_disk_path(data_dir, meta))
    return True


async def delete_conversation_files(data_dir: Path, cid: str) -> int:
    """删除会话时级联清理其绑定的全部附件（磁盘 + 索引），返回删除数。"""
    removed: list[str] = []
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        for file_id, meta in list(index["files"].items()):
            if meta.get("cid") == cid:
                index["files"].pop(file_id, None)
                await asyncio.to_thread(_unlink_quiet, _abs_disk_path(data_dir, meta))
                removed.append(file_id)
        if removed:
            await _save_index(data_dir, index)
    if removed:
        logger.info(f"[webui] 会话级联删除附件 {len(removed)} 个 cid={cid}")
    return len(removed)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"[webui] 附件文件删除失败（索引已清理）{path}: {exc}")


async def sweep_orphans(data_dir: Path, max_age_hours: float = ORPHAN_MAX_AGE_HOURS, now: datetime | None = None) -> int:
    """清理未被引用且登记时间超过 max_age_hours 的孤儿附件，返回清理数。

    测试可注入 ``now`` 控制时钟（不改全局时钟，references/09 模式）。
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    removed: list[str] = []
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        for file_id, meta in list(index["files"].items()):
            if meta.get("cid"):
                continue  # 已被会话引用，不算孤儿
            created = _parse_iso(meta.get("created_at"))
            if created is None or created > cutoff:
                continue  # 缺时间戳的条目保守跳过
            index["files"].pop(file_id, None)
            await asyncio.to_thread(_unlink_quiet, _abs_disk_path(data_dir, meta))
            removed.append(file_id)
        if removed:
            await _save_index(data_dir, index)
    return len(removed)


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 24h 孤儿清理后台任务（main.initialize 启动 / main.terminate 取消）
# ---------------------------------------------------------------------------

_CLEANUP_TASK: asyncio.Task | None = None


async def start_orphan_cleanup(plugin) -> None:
    """启动孤儿清理后台任务（幂等；已在运行则跳过）。"""
    global _CLEANUP_TASK
    if _CLEANUP_TASK is not None and not _CLEANUP_TASK.done():
        return
    _CLEANUP_TASK = asyncio.create_task(_cleanup_loop(plugin))


async def stop_orphan_cleanup() -> None:
    """取消孤儿清理后台任务（terminate 调用；幂等）。"""
    global _CLEANUP_TASK
    task, _CLEANUP_TASK = _CLEANUP_TASK, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _cleanup_loop(plugin, interval: float = CLEANUP_INTERVAL_SECONDS) -> None:
    """每小时清扫一次孤儿附件；异常不中断循环。"""
    while True:
        try:
            removed = await sweep_orphans(plugin.data_dir())
            if removed:
                logger.info(f"[webui] 孤儿附件已清理 {removed} 个（>24h 未引用）")
        except Exception:  # noqa: BLE001 —— 清理失败不影响主流程
            logger.exception("[webui] 孤儿附件清理异常")
        await asyncio.sleep(interval)
