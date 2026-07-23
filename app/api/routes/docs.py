# =============================================================================
# 文件作用与架构位置（文档管理路由层）
# =============================================================================
# 本文件负责知识库中文档的上传、列表、详情、删除、重新处理和原文件下载。它是“HTTP
# 上传请求”进入“文件落盘 -> 数据库登记 -> 后台解析 -> 向量入库”流程的起点。
#
# 本文件共有 7 个函数：
#
#   upload_document()     校验并保存上传文件，启动后台处理
#   list_documents()      按知识库分页查询文档
#   get_document()        查询单个文档元数据
#   delete_document()     删除数据库记录、原文件、向量和 BM25 缓存
#   reprocess_document()  把文档重新提交后台处理
#   _get_doc_or_404()     共享的文档查询辅助函数
#   serve_doc_file()      返回原始文件供预览或下载
#
# 上传主流程：
#
#   multipart/form-data
#       |
#       v
#   校验知识库和资源权限
#       |
#   检查重名、扩展名、功能开关和文件大小
#       |
#       v
#   文件写入 uploads/{kb_id}/...
#       |
#       v
#   documents 表新增 processing 记录并立即 commit
#       |
#       v
#   asyncio.create_task(DocumentService.process_document)
#       |
#       +--> 解析文件 -> 文本分块 -> Embedding -> ChromaDB -> completed/failed
#
# 为什么后台处理前要先 commit？后台任务会创建新的数据库会话并按 doc.id 查询记录；只有
# 先提交，另一个会话才能稳定看到刚创建的 Document。
# =============================================================================

# os 处理目录、路径、文件存在判断和删除；uuid 生成不易冲突的服务器文件名。
import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import asyncio
from app.db.session import get_db
from app.models.db import User, KnowledgeBase, Document, DocumentStatus
from app.models.schemas import DocumentOut, Resp, PageResp
from app.api.deps import get_current_user, get_current_user_optional, require_permission
from app.core.config import settings
from app.core.logger import logger
from app.services.document import DocumentService
from typing import Optional

# /docs 与 main.py 的 /api/v1 组合成最终前缀 /api/v1/docs。
router = APIRouter(prefix="/docs", tags=["文档管理"])

# 三组集合用于 O(1) 扩展名成员判断。扩展名不包含开头的点号。
ALLOWED_TYPES = {"pdf", "docx", "xlsx", "xls", "txt", "md", "csv"}
ALLOWED_IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
ALLOWED_AUDIO_TYPES = {"mp3", "wav", "m4a", "aac", "ogg", "wma", "flac", "pcm"}


@router.post("/upload", response_model=Resp)
async def upload_document(
    # Form/File 表示接口使用 multipart/form-data，而不是普通 JSON 请求体。
    kb_id: int = Form(..., description="知识库ID"),
    tags: Optional[str] = Form(None, description="文档标签，逗号分隔"),
    file: UploadFile = File(..., description="文档文件"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    # 验证知识库权限：所有者、或公开、或角色授权
    # 当前实际判断代码包含所有者、管理员、角色授权；没有单独使用 kb.is_public 放行。
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = kb_result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb.owner_id != current_user.id and not current_user.is_admin:
        # 局部导入避免模块顶层加载不必要依赖。
        from app.api.deps import get_user_kb_ids
        user_kb_ids = await get_user_kb_ids(db, current_user)
        if kb.id not in user_kb_ids:
            raise HTTPException(status_code=403, detail="无权向该知识库上传文档")

    # 同知识库内不允许重复文件名
    existing = await db.execute(
        select(Document).where(Document.kb_id == kb_id, Document.filename == file.filename)
    )
    if existing.scalar_one_or_none():
        # 409 Conflict 表示请求本身合法，但与知识库中现有资源发生冲突。
        raise HTTPException(status_code=409, detail=f"知识库内已存在同名文件「{file.filename}」")

    # 校验文件类型
    # 有点号时取最后一段作为扩展名，例如 report.final.pdf -> pdf；没有点则得到空字符串。
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    is_image = ext in ALLOWED_IMAGE_TYPES
    is_audio = ext in ALLOWED_AUDIO_TYPES
    if is_audio and not settings.VOICE_ENABLED:
        # 即使扩展名在允许集合中，功能开关关闭时仍拒绝音频。
        raise HTTPException(status_code=400, detail="当前未启用语音识别功能")
    # 普通文件和图片先二选一；音频再覆盖为音频允许集合。
    allowed = ALLOWED_IMAGE_TYPES if is_image else ALLOWED_TYPES
    if is_audio:
        allowed = ALLOWED_AUDIO_TYPES
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    # 读取文件内容
    # UploadFile.read() 是异步读取；读取后 content 是 bytes，尚未写入磁盘。
    content = await file.read()
    content_size = len(content)

    # 校验文件大小（图片/语音使用各自的限制）
    # 配置单位是 MB；乘 1024 * 1024 转换为字节后与 len(content) 比较。
    if is_audio:
        if content_size > settings.MAX_AUDIO_SIZE_MB * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"音频文件超过 {settings.MAX_AUDIO_SIZE_MB}MB 限制")
    elif is_image:
        if content_size > settings.MAX_IMAGE_SIZE_MB * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"图片超过 {settings.MAX_IMAGE_SIZE_MB}MB 限制")
    else:
        if content_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"文件超过 {settings.MAX_FILE_SIZE_MB}MB 限制")

    # 保存原始文件
    # 每个知识库使用独立子目录，便于管理和减少同名冲突范围。
    save_dir = os.path.join(settings.UPLOAD_DIR, str(kb_id))
    os.makedirs(save_dir, exist_ok=True)
    # uuid 前缀让服务器物理文件名唯一，同时保留原文件名便于识别。
    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    file_path = os.path.join(save_dir, unique_name)
    with open(file_path, "wb") as f:
        # wb 表示二进制写入，适用于 PDF、图片、音频等所有上传类型。
        f.write(content)

    # 创建文档记录，状态直接设为 processing，立即返回给前端
    doc = Document(
        kb_id=kb_id,
        filename=file.filename,
        file_path=file_path,
        file_type=ext,
        file_size=len(content),
        tags=tags,
        status=DocumentStatus.processing,
    )
    db.add(doc)
    # flush 获得 doc.id，refresh 补齐时间等字段；显式 commit 让后台会话可读取此记录。
    await db.flush()
    await db.refresh(doc)
    await db.commit()

    # 真正的后台异步处理：解析 + 向量化 + 入库（不阻塞请求）
    # create_task 只调度任务，不等待其完成，所以前端能马上收到 processing 状态。
    asyncio.create_task(DocumentService.process_document(doc.id))

    return Resp(data=DocumentOut.model_validate(doc), message="文件上传成功，正在后台处理")


@router.get("", response_model=PageResp)
async def list_documents(
    kb_id: int,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    # 先检查知识库是否存在和是否可访问，不能仅凭 kb_id 直接列出文档。
    from app.api.deps import get_user_kb_ids
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = kb_result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb.owner_id != current_user.id and not current_user.is_admin:
        user_kb_ids = await get_user_kb_ids(db, current_user)
        if kb.id not in user_kb_ids:
            raise HTTPException(status_code=403, detail="无权访问该知识库")

    # 构造基础查询；status 可选，例如只查看 completed 或 failed 文档。
    q = select(Document).where(Document.kb_id == kb_id)
    if status:
        q = q.where(Document.status == status)
    # 分页前统计总数，再按创建时间倒序取当前页。
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    result = await db.execute(
        q.offset((page - 1) * page_size).limit(page_size).order_by(Document.created_at.desc())
    )
    docs = result.scalars().all()
    return PageResp(data=[DocumentOut.model_validate(d) for d in docs], total=total, page=page, page_size=page_size)


@router.get("/{doc_id}", response_model=Resp)
async def get_document(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    # _get_doc_or_404 只负责文档存在性；随后还要根据 doc.kb_id 检查知识库访问权。
    doc = await _get_doc_or_404(doc_id, db)
    from app.api.deps import get_user_kb_ids
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == doc.kb_id))
    kb = kb_result.scalar_one_or_none()
    if kb and kb.owner_id != current_user.id and not current_user.is_admin:
        user_kb_ids = await get_user_kb_ids(db, current_user)
        if kb.id not in user_kb_ids:
            raise HTTPException(status_code=403, detail="无权访问该文档")
    # DocumentOut 只返回安全元数据，不暴露服务器内部 file_path 和文本块正文。
    return Resp(data=DocumentOut.model_validate(doc))


@router.delete("/{doc_id}", response_model=Resp)
async def delete_document(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    doc = await _get_doc_or_404(doc_id, db)

    # 获取知识库信息用于权限校验
    kb_result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == doc.kb_id))
    kb = kb_result.scalar_one_or_none()

    # 超级管理员可删除任意知识库的文档
    # 有该知识库权限的用户（通过角色被授予 kb:{id} 权限）也可删除
    # 仅 KB 所有者或超级管理员无权限
    if not current_user.is_admin:
        if kb.owner_id != current_user.id:
            from app.api.deps import get_user_kb_ids
            user_kb_ids = await get_user_kb_ids(db, current_user)
            if doc.kb_id not in user_kb_ids:
                raise HTTPException(status_code=403, detail="无权删除该文档")

    # 删除向量
    from app.db.vector_store import VectorStore
    # 一个文档可能产生多个向量块，按 metadata.doc_id 一次删除全部。
    VectorStore.delete_by_doc_id(doc.kb_id, doc_id)

    # 删除原始文件
    # 先判断存在，避免重复删除时 FileNotFoundError 中断数据库清理。
    if os.path.exists(doc.file_path):
        os.remove(doc.file_path)

    # 更新知识库文档数
    if kb and kb.doc_count > 0:
        # 防止计数减成负数；列表/详情接口还会实时统计真实数量进行校正。
        kb.doc_count -= 1

    # 标记删除 Document ORM 对象，最终由 get_db 提交事务。
    await db.delete(doc)

    from app.services.document import _invalidate_bm25_cache
    await _invalidate_bm25_cache(doc.kb_id)

    return Resp(message="文档已删除")

# 重新处理失败的文档
@router.post("/{doc_id}/reprocess", response_model=Resp)
async def reprocess_document(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    """重新处理失败的文档"""
    # 当前实现只执行菜单权限和文档存在性检查，没有再次检查该文档所属知识库的资源权限。
    doc = await _get_doc_or_404(doc_id, db)
    # 清除旧错误并把状态改为处理中，让前端立即显示新状态。
    doc.status = DocumentStatus.processing
    doc.error_msg = None
    await db.commit()
    # 与上传相同，先提交状态，再让独立后台任务重新解析和向量化。
    asyncio.create_task(DocumentService.process_document(doc_id))
    return Resp(message="已重新提交处理")


async def _get_doc_or_404(doc_id: int, db: AsyncSession) -> Document:
    # 共享“按 ID 查询 + 404”逻辑，避免详情、删除、重处理、文件下载重复编写。
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


@router.get("/{doc_id}/file")
async def serve_doc_file(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_optional),
):
    """直接返回原始文档/图片文件，供前端预览和下载"""
    # get_current_user_optional 允许没有 Token；有 Token 时优先按登录用户检查知识库权限。
    # 当前逻辑在 current_user is None 时不会执行资源权限检查，因此无 Token 请求只要知道
    # doc_id 且文件存在也会继续返回文件。
    doc = await _get_doc_or_404(doc_id, db)

    if current_user is not None:
        from app.api.deps import get_user_kb_ids
        kb_result = await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == doc.kb_id)
        )
        kb = kb_result.scalar_one_or_none()
        if kb and kb.owner_id != current_user.id and not current_user.is_admin:
            user_kb_ids = await get_user_kb_ids(db, current_user)
            if kb.id not in user_kb_ids:
                raise HTTPException(status_code=403, detail="无权查看此文档")

    if not os.path.exists(doc.file_path):
        # 数据库记录存在但磁盘文件丢失时返回明确错误。
        raise HTTPException(status_code=404, detail="文件不存在，请重新上传")

    # mimetypes 根据文件名后缀推测 Content-Type，浏览器据此决定预览或下载方式。
    import mimetypes
    mime_type, _ = mimetypes.guess_type(doc.filename)
    if mime_type is None:
        image_exts = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
        if doc.file_type.lower() in image_exts:
            mime_type = f"image/{doc.file_type.lower()}"
        else:
            # 无法识别时使用通用二进制类型，浏览器通常按下载文件处理。
            mime_type = "application/octet-stream"

    # 延迟导入 FileResponse；它会以流式文件响应方式读取磁盘，不把整个文件再次装入内存。
    from starlette.responses import FileResponse
    return FileResponse(
        path=doc.file_path,
        filename=doc.filename,
        media_type=mime_type,
    )
