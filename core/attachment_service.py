"""附件服务：上传保存（宿主附件区）、db 注册、类型白名单、大小限制与索引（v0.2）。

契约（对接契约 §附件元数据 / §REST upload、files；开发计划 §0.2 附件节）：
- **必须** ``Path(upload.filename).name`` 防目录穿越；
- 类型白名单：图片 png/jpg/jpeg/webp/gif（扩展名判定）；文本按配置
  ``attachment_text_whitelist``（实时读，热生效）；其余拒收 -> 415；
- **落点 v0.2 起改为宿主附件区** ``get_astrbot_data_path()/attachments/``
  （``data/`` 属宿主数据区，不违反「禁写插件目录」规则），等同官方 WebChat
  行为：保存后 ``db.insert_attachment(path, type, mime_type)`` 注册取得
  ``attachment_id``（签名按 sqlite.py:1073-1084 实源：位置参数 path/type/
  mime_type，返回带 attachment_id 的 Attachment PO）；
- 插件索引沿用现有机制（``<data_dir>/uploads/index.json`` 单文件原子写）：
  f_id ↔ attachment_id ↔ 展示名/大小/kind/preview/truncated；**v0.1.x 旧条目
  的相对路径继续兼容**（相对路径按插件数据目录解析，旧 uploads 文件继续经
  files/<file_id> 服务）；服务器本地绝对路径**永不出现在任何响应中**；
- 图片走 ``upload.save(path, max_bytes=upload_image_max_mb)``，超限宿主抛
  ``UploadTooLargeError`` -> 413 拒绝，**图片永不截断**（v4.29.4 api/web.py
  实证 save 签名：超限时移除半截文件并抛错，返回写入字节数）；
- 文本不按 max_bytes 拒收（否则无法"截断"）：落盘套 16MB 硬上限（契约外防
  磁盘滥用，超出 413），preview 超前 500 字符截断；**文本内容不再注入
  prompt**（v0.2 起文本附件作为 file part 交宿主管道，「全文注入」降级为
  已知限制，README 注明）；
- **bot 侧媒体帧**（宿主发出 image/file/record/video 时已自行落盘
  ``data/attachments/`` 且宿主不注册 db，webchat_event.py:114-183 实源）：
  ``register_host_attachment`` 解析文件名 -> ``db.insert_attachment`` 注册 ->
  写插件索引 -> 返回消息 part（与上传通道共用同一索引）；
- 生命周期：upload 即登记（未引用）；被 send 引用后绑定 cid；删除会话级联
  清索引与文件；未被引用孤儿 24h 后由 asyncio 后台任务清理（main.terminate
  时取消该任务）；
- 图片 preview：PIL 生成 <=256px webp 缩略 data URL（真机宿主有 Pillow；无
  PIL 时降级为 None，前端按契约回退 ``files/<file_id>``）。
"""

import asyncio
import base64
import io
import json
import mimetypes
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


UPLOAD_DIR_NAME = "uploads"  # 索引文件沿用旧目录（兼容 v0.1.x 旧条目与归档附件服务）
INDEX_FILE_NAME = "index.json"
INDEX_VERSION = 1
HOST_ATTACHMENTS_DIR_NAME = "attachments"  # 宿主附件区：data/attachments/

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


class HostStorageUnavailableError(Exception):
    """宿主数据区路径不可用（桩环境 / 宿主 API 缺失）。"""


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


def host_attachments_dir() -> Path:
    """宿主附件区：``get_astrbot_data_path()/attachments/``（不存在则创建）。

    Raises:
        HostStorageUnavailableError: 宿主路径 API 不可用（桩环境）。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    except Exception as exc:  # noqa: BLE001 —— 桩环境/宿主缺失
        raise HostStorageUnavailableError(f"宿主数据路径不可用: {exc}") from exc
    path = Path(get_astrbot_data_path()) / HOST_ATTACHMENTS_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def index_file(data_dir: Path) -> Path:
    """附件索引文件：``<data_dir>/uploads/index.json``（原子写；沿用旧位置兼容旧条目）。"""
    return Path(data_dir) / UPLOAD_DIR_NAME / INDEX_FILE_NAME


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


# 索引读-改-写串行化锁：store_upload/register_*/bind_file/delete_*/sweep_orphans
# 的 load-modify-save 全程持锁，防止并发丢条目
_INDEX_LOCK = asyncio.Lock()


def resolve_disk_path(data_dir: Path, meta: dict) -> Path:
    """索引条目 -> 磁盘路径。

    v0.2 新条目存绝对路径（宿主附件区 data/attachments/）；v0.1.x 旧条目存
    相对路径（uploads/<yyyyMM>/<file_id>_<name>，按插件数据目录解析）。绝对
    路径仅内部使用，**永不出现在任何响应中**。
    """
    raw = str(meta.get("path", ""))
    path = Path(raw)
    if path.is_absolute():
        return path
    return Path(data_dir) / raw


# 兼容别名（旧调用方语义；实现同 resolve_disk_path）
def _abs_disk_path(data_dir: Path, meta: dict) -> Path:
    return resolve_disk_path(data_dir, meta)


def public_meta(meta: dict) -> dict:
    """索引条目 -> 契约附件元数据（剥离 path/cid/attachment_id/created_at 内部字段）。

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

    v0.2 语义：仅用于 preview 展示（全文注入 prompt 已废止，交宿主管道）。

    Returns:
        ``(content, truncated)``：truncated 表示磁盘内容超过 max_kb。
    """
    raw = path.read_bytes()
    limit = max(1, int(max_kb)) * 1024
    truncated = len(raw) > limit
    data = raw[:limit] if truncated else raw
    return _decode_text(data), truncated


async def insert_db_attachment(context, disk_path: Path, attach_type: str, mime_type: str):
    """宿主 db 附件注册（``db.insert_attachment``，sqlite.py:1073-1084 实源）。

    Args:
        attach_type: ``"image"`` / ``"record"`` / ``"video"`` / ``"file"``
            （宿主语义；文本文件按 file 交宿主管道）。

    Returns:
        Attachment PO（含 attachment_id）；宿主异常原样上抛（调用方决定兜底）。
    """
    db = context.get_db()
    return await db.insert_attachment(
        str(disk_path),
        attach_type,
        mime_type,
    )


async def store_upload(
    upload: PluginUploadFile,
    context,
    config: dict,
    data_dir: Path,
    attachments_dir: Path | None = None,
) -> dict:
    """保存并登记一个上传文件，返回契约附件元数据（v0.2：宿主附件区 + db 注册）。

    流程：防穿越安全化文件名 -> classify（unknown -> 415）-> 落盘宿主附件区
    ``data/attachments/<file_id>_<name>``（图片 max_bytes=图片上限，超限 413；
    文本 16MB 硬上限）-> ``db.insert_attachment`` 注册取得 attachment_id ->
    抽取 preview -> 写插件索引（未引用态）。

    Args:
        attachments_dir: 测试注入用落盘目录覆盖（None 用宿主附件区）。

    Raises:
        InvalidFilenameError: 文件名缺失/非法（-> 400）。
        UnsupportedMediaTypeError: 类型不在白名单（-> 415）。
        UploadTooLargeError: 图片超限或文本超 16MB 硬上限（-> 413）。
        HostStorageUnavailableError: 宿主附件区不可用。
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
    target_dir = Path(attachments_dir) if attachments_dir is not None else host_attachments_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    disk_path = target_dir / f"{file_id}_{safe_name}"

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

    if kind == "image":
        mime = IMAGE_MIME.get(Path(safe_name).suffix.lower().lstrip("."), "application/octet-stream")
        preview = await asyncio.to_thread(_image_preview, disk_path)
        truncated = False
        db_type = "image"
    else:
        # 同步文件 IO（可达 16MB）放线程池，避免阻塞事件循环拖慢挂着的 SSE 流
        content, truncated = await asyncio.to_thread(
            extract_text_content,
            disk_path,
            _config_int(config, "upload_text_max_kb"),
        )
        preview = content[:PREVIEW_TEXT_CHARS]
        mime = TEXT_MIME.get(Path(safe_name).suffix.lower().lstrip("."), "text/plain")
        db_type = "file"  # 文本文件按宿主 file 语义注册（v0.2 交宿主管道）

    attachment = await insert_db_attachment(context, disk_path, db_type, mime)
    attachment_id = str(getattr(attachment, "attachment_id", "") or "")

    meta = {
        "file_id": file_id,
        "attachment_id": attachment_id,
        "kind": kind,
        "name": safe_name,
        "size": int(size),
        "mime": mime,
        "preview": preview,
        "truncated": truncated,
        # 内部字段：绝对路径（宿主附件区；绝不进响应）、引用 cid、登记时间
        "path": disk_path.resolve().as_posix(),
        "cid": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        index["files"][file_id] = meta
        await _save_index(data_dir, index)
    logger.info(f"[webui] 附件已登记 file_id={file_id} attachment_id={attachment_id} kind={kind} size={size}")
    return public_meta(meta)


def _probe_host_attachment(stored_name: str) -> tuple[Path, str, int, str, bool]:
    """bot 媒体帧文件的同步磁盘探测（经 ``asyncio.to_thread`` 执行，勿在事件循环直接调用）。

    附件区就绪（mkdir）+ 文件存在判定 + 大小 stat + resolve + mime 猜测一次打包，
    供排水循环逐帧调用而不阻塞事件循环。

    Returns:
        ``(disk_path, path_posix, size, mime_type, exists)``；mime 缺省回退
        ``application/octet-stream``。
    """
    host_dir = host_attachments_dir()  # mkdir 就绪（宿主路径 API 不可用时抛 HostStorageUnavailableError）
    disk_path = host_dir / stored_name
    exists = disk_path.is_file()
    mime_type, _ = mimetypes.guess_type(str(disk_path))
    size = int(disk_path.stat().st_size) if exists else 0
    return disk_path, disk_path.resolve().as_posix(), size, mime_type or "application/octet-stream", exists


async def register_host_attachment(
    context,
    frame_type: str,
    stored_name: str,
    display_name: str | None = None,
    *,
    data_dir: Path | None = None,
    cid: str | None = None,
) -> dict | None:
    """注册宿主已落盘的 bot 媒体帧文件（bot 侧 image/file/record/video 帧）。

    宿主发媒体帧时已把文件写入 ``data/attachments/<stored_name>`` 且不注册 db
    （webchat_event.py:114-183 实源）；本函数补 ``db.insert_attachment`` 注册 +
    插件索引（与上传通道共用同一索引），返回消息 part
    ``{"type", "attachment_id", "filename"}``。

    本函数经 pipeline_bridge 排水循环逐帧调用：同步磁盘 IO（附件区就绪
    mkdir / is_file / stat / resolve / mime 猜测）打包进 ``asyncio.to_thread``
    （对齐同模块 ``store_upload`` 既有做法），事件循环内不做阻塞 IO。

    Returns:
        消息 part；文件不存在返回 None（调用方跳过，不炸流）。
    """
    if not stored_name:
        return None
    disk_path, path_posix, size, mime_type, exists = await asyncio.to_thread(
        _probe_host_attachment, stored_name
    )
    if not exists:
        logger.warning(f"[webui] bot 媒体帧文件缺失（跳过注册）{disk_path}")
        return None
    attachment = await insert_db_attachment(context, disk_path, frame_type, mime_type)
    attachment_id = str(getattr(attachment, "attachment_id", "") or "")
    filename = display_name or disk_path.name

    meta = {
        "file_id": f"f_{uuid.uuid4().hex}",
        "attachment_id": attachment_id,
        "kind": frame_type,
        "name": filename,
        "size": size,
        "mime": mime_type,
        "preview": None,
        "truncated": False,
        "path": path_posix,
        "cid": cid,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if data_dir is not None:
        async with _INDEX_LOCK:
            index = await _load_index(data_dir)
            index["files"][meta["file_id"]] = meta
            await _save_index(data_dir, index)
    return {
        "type": frame_type,
        "attachment_id": attachment_id,
        "filename": filename,
        **({"stored_filename": disk_path.name} if filename != disk_path.name else {}),
    }


async def find_by_attachment_id(data_dir: Path, attachment_id: str) -> dict | None:
    """按宿主 attachment_id 反查索引条目（history 读取媒体 parts 反查用）。"""
    if not attachment_id:
        return None
    index = await _load_index(data_dir)
    for meta in index["files"].values():
        if str(meta.get("attachment_id") or "") == str(attachment_id):
            return dict(meta)
    return None


async def get_file(data_dir: Path, file_id: str) -> dict | None:
    """按 file_id 取索引条目（含内部字段 path/cid/attachment_id；不存在返回 None）。"""
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
    await asyncio.to_thread(_unlink_quiet, resolve_disk_path(data_dir, meta))
    return True


async def delete_attachments_by_ids(data_dir: Path, attachment_ids: list[str]) -> int:
    """按宿主 attachment_id 集合清理插件索引条目与磁盘文件（级联删除用）。"""
    wanted = {str(aid) for aid in attachment_ids if aid}
    if not wanted:
        return 0
    removed: list[dict] = []
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        for file_id, meta in list(index["files"].items()):
            if str(meta.get("attachment_id") or "") in wanted:
                index["files"].pop(file_id, None)
                removed.append(meta)
        if removed:
            await _save_index(data_dir, index)
    for meta in removed:
        await asyncio.to_thread(_unlink_quiet, resolve_disk_path(data_dir, meta))
    return len(removed)


async def delete_conversation_files(data_dir: Path, cid: str) -> int:
    """删除会话时级联清理其绑定的全部附件（磁盘 + 索引），返回删除数。"""
    removed: list[dict] = []
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        for file_id, meta in list(index["files"].items()):
            if meta.get("cid") == cid:
                index["files"].pop(file_id, None)
                removed.append(meta)
        if removed:
            await _save_index(data_dir, index)
    for meta in removed:
        await asyncio.to_thread(_unlink_quiet, resolve_disk_path(data_dir, meta))
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
    removed: list[dict] = []
    async with _INDEX_LOCK:
        index = await _load_index(data_dir)
        for file_id, meta in list(index["files"].items()):
            if meta.get("cid"):
                continue  # 已被会话引用，不算孤儿
            created = _parse_iso(meta.get("created_at"))
            if created is None or created > cutoff:
                continue  # 缺时间戳的条目保守跳过
            index["files"].pop(file_id, None)
            removed.append(meta)
        if removed:
            await _save_index(data_dir, index)
    for meta in removed:
        await asyncio.to_thread(_unlink_quiet, resolve_disk_path(data_dir, meta))
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
