# =============================================================================
# 文件作用与架构位置（文档入库流水线核心服务）
# =============================================================================
# 本文件把已经上传并登记为 processing 的 Document 转换为可检索数据。它同时协调文件
# 解析、文本分块、Embedding、ChromaDB、MySQL DocumentChunk、语音 ASR 和缓存失效。
#
# 本文件有 12 个函数/方法和 1 个服务类：
#
#   _get_process_pool()       创建并复用后台线程池
#   _is_markdown_content()    通过内容判断是否像 Markdown
#   _chunk_markdown()         Markdown 标题感知分块
#   _sync_process_doc()       普通文档同步处理主流程
#   _sync_process_audio_doc() ASR 文字的同步分块入库流程
#   _run_async_in_thread()    在线程中运行 async 协程
#   _sync_voice_asr()         线程中的语音识别包装
#   _do_voice_asr()           ASGI 事件循环调用 ASR 的 async 包装
#   DocumentService._get_voice_config() 选择已启用 ASR 配置
#   DocumentService.process_document()  路由调用的总入口
#   _invalidate_bm25_cache()  文档变化后删除关键词索引缓存
#   _mark_failed_async()      将失败状态和错误写回数据库
#
# 普通文档入库：
#
#   Document(file_path, processing)
#       |
#       v
#   get_parser -> ParsedDocument -> 按页面/Block/Markdown 标题分块
#       |
#       v
#   [chunk text] -> EmbeddingService -> [vector]
#       |                                  |
#       |                                  v
#       +--> MySQL document_chunks      ChromaDB kb_{id}
#       |
#       v
#   Document.status=completed + chunk_count + KB.doc_count
#       |
#       v
#   删除 BM25 缓存，下次检索重新构建
#
# 音频流程在解析前多一步 VoiceASRService，把音频先变成 asr_text。
#
# 为什么使用线程池？解析、模型推理和同步数据库/文件库操作可能阻塞；把它们放到工作线程
# 后，FastAPI 主事件循环仍能响应其他 HTTP 请求。
# =============================================================================

# uuid 用于生成 Chroma chunk 唯一 ID；os 用于 CPU 数量和路径相关操作。
import uuid
import os
from concurrent.futures import ThreadPoolExecutor
import asyncio
from sqlalchemy import select
from app.models.db import Document, DocumentChunk, KnowledgeBase, DocumentStatus
from app.db.session import AsyncSessionLocal
from app.db.vector_store import VectorStore
from app.services.embedding import EmbeddingService
from app.core.config import settings
from app.core.logger import logger
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 虽然变量名叫 process_pool，实际对象是 ThreadPoolExecutor（线程池而非进程池）。
_process_pool = None


def _get_process_pool():
    """获取线程池（CPU 核数 - 1 个 worker，最少 2 个）"""
    global _process_pool
    if _process_pool is None:
        # 默认保留约一个 CPU 核给主进程，但至少创建 2 个 worker。
        workers = max(2, (os.cpu_count() or 4) - 1)
        _process_pool = ThreadPoolExecutor(max_workers=workers)
        logger.info(f"文档处理线程池已创建，workers={workers}")
    return _process_pool


# =============================================================================
# 线程池中执行的同步函数（所有 CPU/IO 密集型操作在这里）
# =============================================================================

def _is_markdown_content(text: str) -> bool:
    """
    通过内容嗅探判断文本是否包含 Markdown 标题结构。
    用于 file_type 未能正确标记时的兜底检测。
    """
    import re
    # 只检查前 30 行，足够识别文档结构且避免扫描整份长文本。
    lines = text.strip().split("\n")
    head = lines[:30]  # 取前 30 行判断
    heading_count = sum(1 for line in head if re.match(r"^#{1,4}\s", line))
    if heading_count >= 2:
        return True
    if heading_count == 1:
        # 只有一个标题时，再要求至少两个列表项，降低普通文本以 # 开头造成的误判。
        list_count = sum(
            1 for line in head
            if re.match(r"^\s*[-*+]\s", line) or re.match(r"^\s*\d+[\.\)]\s", line)
        )
        return list_count >= 2
    return False


def _chunk_markdown(text: str) -> list:
    """
    Markdown 文档专用分块策略。

    第一级：MarkdownHeaderTextSplitter — 按 # / ## / ### / #### 标题层级切分，
            保留标题层级元数据（h1/h2/h3/h4）。
    第二级：超大块（> CHUNK_SIZE）再用 RecursiveCharacterTextSplitter 二次切分，
            标题元数据继承给子块。

    返回 [(content: str, heading_meta: dict), ...]
    """
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

    # 一级：按标题层级切分
    # 第一层尽量让每个 chunk 对应一个标题章节，并把标题层级保存在 metadata。
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "h1"),
            ("##", "h2"),
            ("###", "h3"),
            ("####", "h4"),
        ],
        strip_headers=False,  # 标题文本保留在 chunk 内容中
    )
    header_docs = header_splitter.split_text(text)

    # 二级：超大块二次切分
    # 标题章节仍可能过长，第二层按通用分隔符递归拆分。
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.CHUNK_SIZE,
        chunk_overlap=min(settings.CHUNK_OVERLAP, 50),
        separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
    )

    result = []
    for doc in header_docs:
        content = doc.page_content.strip()
        if not content:
            continue
        if len(content) <= settings.CHUNK_SIZE:
            # 小章节直接保留完整结构。
            result.append((content, dict(doc.metadata)))
        else:
            sub_splits = text_splitter.split_text(content)
            for sub in sub_splits:
                sub = sub.strip()
                if sub:
                    result.append((sub, dict(doc.metadata)))
    return result


def _sync_process_doc(doc_id: int):
    """
    同步函数，运行在线程池中，执行文档解析 → 分块 → 向量化 → ChromaDB 写入。
    完成后返回 (True, chunk_count) 或 (False, error_msg)。
    """
    from sqlalchemy.orm import Session
    from app.db.session import sync_engine
    from app.models.db import Document, DocumentChunk, KnowledgeBase, DocumentStatus
    from app.parsers import get_parser
    from app.services.embedding import EmbeddingService
    from app.db.vector_store import VectorStore
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    import uuid as _uuid

    # 线程内不能复用 FastAPI 的 AsyncSession，因此创建独立同步 Session。
    session = Session(sync_engine)
    try:
        doc = session.execute(
            select(Document).where(Document.id == doc_id)
        ).scalar_one_or_none()

        if not doc:
            return False, "文档不存在"

        logger.info(f"[线程池] 开始处理文档 [{doc_id}]: {doc.filename}")

        # 1. 解析文档
        # get_parser 根据 file_type 选择 PDF/Word/Excel/Text/Image 等解析器。
        parser = get_parser(doc.file_type)
        parsed = parser.parse(doc.file_path)

        if not parsed.pages:
            return False, "文档解析结果为空"

        # 2. 分块 — 按文档类型路由（Markdown → 标题层级切分，其他 → block-aware 切分）
        is_md = doc.file_type.lower() == "md"
        if not is_md:
            is_md = _is_markdown_content(parsed.full_text)

        md_heading_metas = []

        if is_md:
            # Markdown：按标题层级切分，保留标题元数据
            raw_text = parsed.full_text
            if not raw_text.strip():
                return False, "文档内容为空"
            md_result = _chunk_markdown(raw_text)
            # 统一形成 (内容, 页码, 来源类型) 三元组；Markdown 当前统一使用页码 1。
            all_chunks = [(content, 1, "markdown") for content, _ in md_result]
            md_heading_metas = [meta for _, meta in md_result]
            logger.info(f"[线程池] Markdown 文档 [{doc_id}]，按标题切分为 {len(all_chunks)} 块")
        else:
            # 非 Markdown：block-aware 分块（表格/OCR 原子 chunk，纯文本递归切分）
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=settings.CHUNK_SIZE,
                chunk_overlap=settings.CHUNK_OVERLAP,
                separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
            )

            all_chunks = []  # (content, page_num, source_type)
            for page in parsed.pages:
                if page.blocks:
                    # 新解析器：按 ContentBlock 类型差异化处理
                    for block in page.blocks:
                        if not block.content.strip():
                            continue
                        if block.type == "table":
                            # 表格作为原子 chunk，不跨表切分
                            all_chunks.append((block.content.strip(), page.page_num, "table"))
                        elif block.type == "image_text":
                            # OCR 文字作为原子 chunk
                            all_chunks.append((block.content.strip(), page.page_num, "image_text"))
                        else:
                            # 纯文本：正常递归切分
                            splits = splitter.split_text(block.content)
                            for split in splits:
                                if split.strip():
                                    all_chunks.append((split.strip(), page.page_num, "text"))
                else:
                    # 旧解析器（无 blocks，向后兼容）
                    if not page.content.strip():
                        continue
                    splits = splitter.split_text(page.content)
                    for split in splits:
                        if split.strip():
                            all_chunks.append((split.strip(), page.page_num, "text"))

        if not all_chunks:
            return False, "文档分块结果为空"

        logger.info(f"[线程池] 文档 [{doc_id}] 分块完成，共 {len(all_chunks)} 块")

        # 3. 向量化
        # texts 的顺序必须与 all_chunks 一致，后面通过 zip 把每个向量配回原文本块。
        texts = [c[0] for c in all_chunks]
        embeddings = EmbeddingService.embed_texts(texts)

        # 4. 构建 ChromaDB 数据
        chroma_ids = []
        chroma_metadatas = []
        db_chunks = []

        for i, ((content, page_num, source_type), embedding) in enumerate(zip(all_chunks, embeddings)):
            # ID 包含文档、块序号和随机后缀，减少重新处理或并发任务时发生冲突。
            chroma_id = f"doc{doc_id}_chunk{i}_{_uuid.uuid4().hex[:8]}"
            chroma_ids.append(chroma_id)
            chroma_metadatas.append({
                "doc_id": doc_id,
                "kb_id": doc.kb_id,
                "filename": doc.filename,
                "file_type": doc.file_type,
                "page_num": page_num or 0,
                "chunk_index": i,
                "source_type": source_type,
                "tags": doc.tags or "",
            })
            # Markdown：合并标题层级元数据（h1/h2/h3/h4）
            if is_md and i < len(md_heading_metas):
                # 把 h1/h2 等标题加入 Chroma metadata，便于未来过滤或显示章节来源。
                chroma_metadatas[-1].update({
                    k: v for k, v in md_heading_metas[i].items() if v
                })
            db_chunks.append(DocumentChunk(
                doc_id=doc_id,
                kb_id=doc.kb_id,
                chroma_id=chroma_id,
                content=content,
                chunk_index=i,
                page_num=page_num,
                # 当前 token_count 实际保存字符长度，并非模型 tokenizer 的真实 Token 数。
                token_count=len(content),
            ))

        # 5. 写入 ChromaDB
        VectorStore.add_documents(
            kb_id=doc.kb_id,
            ids=chroma_ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=chroma_metadatas,
        )

        # 6. 写入 MySQL chunks
        # Chroma 和 MySQL 不共享事务：Chroma 已成功但后续数据库提交失败时，可能留下孤立向量。
        for chunk in db_chunks:
            session.add(chunk)

        # 更新文档状态
        # 全部数据准备完毕后才把文档标记为完成。
        doc.status = DocumentStatus.completed
        doc.chunk_count = len(all_chunks)

        # 更新知识库文档数
        kb = session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == doc.kb_id)
        ).scalar_one_or_none()
        if kb:
            # 每次成功处理都会 +1；重新处理已有成功文档时，现有逻辑可能重复增加计数。
            kb.doc_count = (kb.doc_count or 0) + 1

        session.commit()
        logger.info(f"[线程池] 文档 [{doc_id}] 处理完成，{len(all_chunks)} 个向量已入库")
        return True, len(all_chunks)

    except Exception as e:
        # 回滚本同步 MySQL 会话，并把用户可见错误写回 Document。
        session.rollback()
        err_msg = str(e)
        if "未识别到文字内容" in err_msg or "图片文件无效" in err_msg or "图片中未识别到文字" in err_msg:
            err_msg = "图片中未识别到文字内容，无法入库，请上传包含文字的图片"
        logger.error(f"[线程池] 文档 [{doc_id}] 处理失败: {err_msg}")

        doc = session.execute(
            select(Document).where(Document.id == doc_id)
        ).scalar_one_or_none()
        if doc:
            doc.status = DocumentStatus.failed
            doc.error_msg = err_msg[:500]
            session.commit()

        return False, err_msg

    finally:
        # 无论成功或失败都释放数据库连接。
        session.close()


def _sync_process_audio_doc(doc_id: int, asr_text: str):
    """处理 ASR 识别文本（分块+向量化），运行在线程池中"""
    from sqlalchemy.orm import Session
    from app.db.session import sync_engine
    from app.models.db import Document, KnowledgeBase, DocumentStatus, DocumentChunk
    from app.services.embedding import EmbeddingService
    from app.db.vector_store import VectorStore
    from app.parsers.voice import VoiceParser
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    import uuid as _uuid

    # 该函数不再读取音频本身，只把已识别的 asr_text 按普通文本分块和向量化。
    session = Session(sync_engine)
    try:
        doc = session.execute(
            select(Document).where(Document.id == doc_id)
        ).scalar_one_or_none()
        if not doc:
            return

        parsed = VoiceParser(asr_text=asr_text).parse(doc.file_path)
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
            separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
        )

        all_chunks = []
        for page in parsed.pages:
            if not page.content.strip():
                continue
            splits = splitter.split_text(page.content)
            for split in splits:
                if split.strip():
                    all_chunks.append((split.strip(), page.page_num or 0, "text"))

        if not all_chunks:
            raise ValueError("文档分块结果为空")

        texts = [c[0] for c in all_chunks]
        embeddings = EmbeddingService.embed_texts(texts)

        chroma_ids = []
        chroma_metadatas = []
        for i, ((content, page_num, source_type), embedding) in enumerate(zip(all_chunks, embeddings)):
            chroma_id = f"doc{doc_id}_chunk{i}_{_uuid.uuid4().hex[:8]}"
            chroma_ids.append(chroma_id)
            chroma_metadatas.append({
                "doc_id": doc_id,
                "kb_id": doc.kb_id,
                "filename": doc.filename,
                "file_type": doc.file_type,
                "page_num": page_num,
                "chunk_index": i,
                "source_type": source_type,
                "tags": doc.tags or "",
            })

        VectorStore.add_documents(
            kb_id=doc.kb_id,
            ids=chroma_ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=chroma_metadatas,
        )

        for i, (content, page_num, _) in enumerate(all_chunks):
            # chroma_ids 与 all_chunks 使用同一索引，确保两种存储可以互相定位。
            session.add(DocumentChunk(
                doc_id=doc_id,
                kb_id=doc.kb_id,
                chroma_id=chroma_ids[i],
                content=content,
                chunk_index=i,
                page_num=page_num,
                token_count=len(content),
            ))

        doc.status = DocumentStatus.completed
        doc.chunk_count = len(all_chunks)
        kb = session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == doc.kb_id)
        ).scalar_one_or_none()
        if kb:
            kb.doc_count = (kb.doc_count or 0) + 1

        session.commit()
        logger.info(f"[线程池] 语音文档 [{doc_id}] 处理完成，{len(all_chunks)} 个向量已入库")

    except Exception as e:
        session.rollback()
        logger.error(f"[线程池] 语音文档 [{doc_id}] 处理失败: {e}")
        doc = session.execute(
            select(Document).where(Document.id == doc_id)
        ).scalar_one_or_none()
        if doc:
            doc.status = DocumentStatus.failed
            doc.error_msg = str(e)[:500]
            session.commit()
    finally:
        session.close()


# =============================================================================
# 语音识别（线程池执行，支撑 async 语音服务）
# =============================================================================

def _run_async_in_thread(coro):
    """在当前线程中运行协程（用于 ThreadPoolExecutor 中调用 async 函数）"""
    try:
        # 工作线程通常没有事件循环；若有则尝试复用。
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 已有事件循环，直接运行
            future = asyncio.ensure_future(coro)
            return loop.run_until_complete(future)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        # 没有事件循环，创建新的
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _sync_voice_asr(doc_id: int, provider: str, api_key: str, api_secret: str, extra: dict) -> str:
    """在线程池中执行同步 ASR（内部通过新事件循环调用 async voice service）"""
    from app.services.voice_asr import VoiceASRService
    from sqlalchemy.orm import Session
    from app.db.session import sync_engine
    from app.models.db import Document
    from sqlalchemy import select

    # 从 DB 读取文件路径
    # 先用短生命周期同步会话取得磁盘文件路径，然后立即关闭数据库连接。
    session = Session(sync_engine)
    try:
        doc = session.execute(select(Document).where(Document.id == doc_id)).scalar_one_or_none()
        file_path = doc.file_path if doc else ""
    finally:
        session.close()

    if not file_path:
        raise RuntimeError(f"文档 {doc_id} 文件路径不存在")

    # 构造协程，在新线程的事件循环中运行
    async def _do():
        # provider 和凭证已在 async 主入口中校验，这里只发起实际识别。
        return await VoiceASRService.recognize(
            file_path=file_path,
            provider=provider,
            api_key=api_key,
            api_secret=api_secret,
            extra_params=extra,
        )

    return _run_async_in_thread(_do())


# =============================================================================
# ASGI 事件循环中调用的 async 入口
# =============================================================================

async def _do_voice_asr(doc_id: int, provider: str, api_key: str, api_secret: str, extra: dict) -> str:
    """在事件循环中调用，在线程池执行同步 ASR"""
    loop = asyncio.get_running_loop()
    # 使用单 worker 临时线程池运行同步包装器，避免 ASR 网络/轮询细节阻塞当前事件循环。
    return await loop.run_in_executor(
        ThreadPoolExecutor(max_workers=1),
        _sync_voice_asr,
        doc_id, provider, api_key, api_secret, extra
    )


# =============================================================================
# 文档处理服务（async 入口，供路由层调用）
# =============================================================================

class DocumentService:

    @staticmethod
    async def _get_voice_config(doc_id: int) -> dict:
        """查询已启用的语音配置"""
        from app.models.db import VoiceConfig
        # doc_id 当前没有参与查询；函数选择“已启用且默认优先”的第一条全局 VoiceConfig。
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(VoiceConfig)
                .where(VoiceConfig.is_enabled == True)
                .order_by(VoiceConfig.is_default.desc())
                .limit(1)
            )
            cfg = result.scalar_one_or_none()
        # 会话关闭后 cfg 成为脱离会话的 ORM 对象，但已加载的标量字段仍可读取。
        return cfg

    @staticmethod
    async def process_document(doc_id: int):
        """
        文档处理入口（async，供 asyncio.create_task 调用）。
        流程：查询文档信息 → 路由到线程池执行 CPU 密集型任务。
        事件循环始终保持空闲，不阻塞其他请求。
        """
        # 本函数由 asyncio.create_task 调度，因此这里能取得当前 ASGI 事件循环。
        loop = asyncio.get_running_loop()
        pool = _get_process_pool()

        # 查询文档基本信息
        # 只读取路由判断所需的轻量字段，离开上下文后立即释放连接。
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Document).where(Document.id == doc_id))
            doc = result.scalar_one_or_none()
            if not doc:
                return
            kb_id = doc.kb_id
            file_type = doc.file_type
            file_path = doc.file_path

        AUDIO_TYPES = {"mp3", "wav", "m4a", "aac", "ogg", "wma", "flac", "pcm"}

        if file_type.lower() in AUDIO_TYPES:
            # 语音文件：先 ASR，再进线程池处理
            # 音频必须先找到完整且启用的 Provider 配置。
            cfg = await DocumentService._get_voice_config(doc_id)
            if not cfg:
                await _mark_failed_async(doc_id, "未找到已启用的语音识别配置，请前往「语音配置」页面添加并启用")
                return

            import json as _json
            # extra_params 保存百度 app_id 或阿里 app_key 等 Provider 特有字段。
            extra = _json.loads(cfg.extra_params) if cfg.extra_params else {}

            # 验证配置完整性
            missing = []
            if not cfg.api_key: missing.append("API Key")
            if not cfg.api_secret: missing.append("Secret Key")
            if cfg.provider == "baidu" and not extra.get("app_id"): missing.append("App ID")
            if cfg.provider == "aliyun" and not extra.get("app_key"): missing.append("AppKey")
            if missing:
                await _mark_failed_async(doc_id, f"{cfg.provider} ASR 配置不完整，缺少: {', '.join(missing)}")
                return

            try:
                logger.info(f"[voice] 开始 ASR 识别文档 [{doc_id}]，provider={cfg.provider}")
                asr_text = await _do_voice_asr(doc_id, cfg.provider, cfg.api_key, cfg.api_secret, extra)
                logger.info(f"[voice] ASR 识别完成，文档 [{doc_id}]，结果长度={len(asr_text)}")
            except Exception as e:
                await _mark_failed_async(doc_id, f"语音识别失败: {e}")
                return

            # ASR 文本进线程池处理（分块+向量化）
            try:
                await loop.run_in_executor(pool, _sync_process_audio_doc, doc_id, asr_text)
                # 工作线程操作持久化 Chroma 后，重建主线程客户端以刷新可见状态。
                VectorStore._client = None
                VectorStore.get_client()
                await _invalidate_bm25_cache(kb_id)
            except Exception as e:
                await _mark_failed_async(doc_id, str(e))
        else:
            try:
                # run_in_executor 返回可等待对象；事件循环等待结果期间仍可调度其他请求。
                await loop.run_in_executor(pool, _sync_process_doc, doc_id)
                VectorStore._client = None
                VectorStore.get_client()
                await _invalidate_bm25_cache(kb_id)
            except Exception as e:
                await _mark_failed_async(doc_id, str(e))


async def _invalidate_bm25_cache(kb_id: int):
    """文档入库后失效 BM25 缓存"""
    try:
        from app.core.redis_client import cache_delete
        await cache_delete(f"kb:{kb_id}:bm25")
        logger.debug(f"BM25 缓存已失效: kb_id={kb_id}")
    except Exception:
        # Redis 只做缓存，失效失败不改变文档已经成功入库的事实。
        pass


async def _mark_failed_async(doc_id: int, err_msg: str):
    """异步方式将文档状态标记为失败"""
    if "未识别到文字内容" in err_msg or "图片文件无效" in err_msg or "图片中未识别到文字" in err_msg:
        err_msg = "图片中未识别到文字内容，无法入库，请上传包含文字的图片"
    try:
        # 使用独立异步会话写入失败状态，不依赖调用方原来的请求会话。
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Document).where(Document.id == doc_id))
            doc = result.scalar_one_or_none()
            if doc:
                doc.status = DocumentStatus.failed
                # 数据库字段长度有限，只保存前 500 个字符。
                doc.error_msg = err_msg[:500]
                await db.commit()
    except Exception:
        # 最后的错误记录也失败时静默结束，避免后台 Task 产生未处理异常。
        pass
