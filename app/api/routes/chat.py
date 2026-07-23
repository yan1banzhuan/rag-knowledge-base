# =============================================================================
# 文件作用与架构位置（RAG 对话路由层）
# =============================================================================
# 本文件负责“对话会话管理”和“RAG 问答”HTTP 接口。它把用户问题、历史消息、知识库
# 检索结果和 LLM 服务连接起来，是前端聊天页面进入后端 RAG 流程的入口。
#
# 本文件共有 8 个函数：
#
#   _filter_cited_sources()  从候选来源中保留回答真正引用的来源
#   create_session()         创建对话会话
#   list_sessions()          分页查询会话
#   get_messages()           查询一个会话的历史消息
#   delete_session()         删除会话
#   chat_completions()       RAG 问答主流程，选择流式或非流式返回
#   _stream_response()       SSE 流式生成、保存回答
#   _get_session_or_404()    查询会话并检查归属权限
#
# 核心调用关系：
#
#   create/list/get/delete session --------------> MySQL ChatSession/ChatMessage
#
#   chat_completions()
#       |
#       +--> _get_session_or_404()       校验会话
#       +--> rewrite_query_if_needed()   多轮问题改写
#       +--> RetrievalService.search()   混合检索知识库
#       +--> LLMService.chat()           非流式回答
#       +--> _stream_response()
#                 +--> LLMService.chat_stream()   流式回答
#       +--> _filter_cited_sources()     精简来源
#       +--> 保存 ChatMessage
#
# 完整 RAG 流程：
#
#   用户问题 + 最近历史
#          |
#          v
#   必要时把“它怎么样？”改写成可独立检索的问题
#          |
#          v
#   知识库检索 -> 相关文本块 -> context
#          |
#          v
#   LLM(messages + user_message + context)
#          |
#          +--> 普通 JSON 响应
#          +--> SSE 分块响应
#          |
#          v
#   过滤引用来源并保存助手消息
# =============================================================================

# re 用于从回答中匹配 [1]、[来源1] 等引用标记。
import re
# json 用于保存来源列表和编码 SSE 事件。
import json
# time.perf_counter 用于记录各阶段耗时。
import time
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db.session import get_db
from app.models.db import User, ChatSession, ChatMessage
from app.models.schemas import ChatSessionCreate, ChatSessionOut, ChatRequest, ChatMessageOut, Resp, PageResp
from app.api.deps import get_current_user, require_permission
from app.services.llm import LLMService
from app.services.retrieval import RetrievalService
from app.services.query_rewriter import rewrite_query_if_needed
from app.core.config import settings
from app.core.logger import logger


def _filter_cited_sources(answer: str, sources: list) -> list:
    """只保留回答中实际引用的来源（匹配 [来源1]、[1] 等格式）"""
    # 没有候选来源时直接返回，避免无意义的正则扫描。
    if not sources:
        return sources
    # set 自动去重：回答多次引用 [1]，来源 1 最终也只返回一次。
    cited_indices = set()
    # 匹配 [来源1] [来源2] 或 [1] [2] 格式
    for m in re.finditer(r"\[来源\s*(\d+)\]|\[(\d+)\]", answer):
        # 正则有两个备选捕获组，只有匹配到的那个非空。
        idx = int(m.group(1) or m.group(2))
        cited_indices.add(idx)
    # 未找到引用标记，说明 LLM 未使用任何来源，返回空列表
    if not cited_indices:
        return []
    # sources 列表下标从 0 开始，引用编号从 1 开始
    # enumerate(..., 1) 让第一个来源编号为 1，与给 LLM 的上下文编号一致。
    return [s for i, s in enumerate(sources, 1) if i in cited_indices]

# prefix="/chat" 与 main.py 的 /api/v1 组合成 /api/v1/chat。
router = APIRouter(prefix="/chat", tags=["对话"])


# 创建会话流程：请求 Schema -> 当前用户 -> ChatSession ORM -> flush/refresh -> Resp。
@router.post("/sessions", response_model=Resp)
async def create_session(
    body: ChatSessionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("chat")),
):
    # user_id 一律使用认证用户，防止客户端替其他用户创建会话。
    # title 为空时给出默认标题；模型提供方和模型名称保存到会话，后续问答复用。
    session = ChatSession(
        user_id=current_user.id,
        kb_id=body.kb_id,
        title=body.title or "新对话",
        llm_provider=body.llm_provider,
        llm_model=body.llm_model,
    )
    # add 登记新增对象；flush 发送 INSERT 并获得 id；refresh 补齐时间字段。
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return Resp(data=ChatSessionOut.model_validate(session))


# 会话列表流程：admin 看全部；普通用户只看自己的；最后按更新时间倒序分页。
@router.get("/sessions", response_model=PageResp)
async def list_sessions(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("chat")),
):
    # 局部导入只在调用列表接口时加载 func。
    from sqlalchemy import func
    if current_user.is_admin:
        q = select(ChatSession)
    else:
        # 会话是用户私有资源，普通用户的查询条件固定包含自己的 user_id。
        q = select(ChatSession).where(ChatSession.user_id == current_user.id)
    # 子查询统计分页前总数，用于前端计算总页数。
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    result = await db.execute(
        q.offset((page - 1) * page_size).limit(page_size).order_by(ChatSession.updated_at.desc())
    )
    sessions = result.scalars().all()
    return PageResp(data=[ChatSessionOut.model_validate(s) for s in sessions], total=total, page=page, page_size=page_size)


# 查询消息前先复用 _get_session_or_404，避免普通用户通过猜测 session_id 读取他人对话。
@router.get("/sessions/{session_id}/messages", response_model=Resp)
async def get_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("chat")),
):
    session = await _get_session_or_404(session_id, current_user, db)
    # 按 created_at 正序返回，使前端可以从最早到最新直接渲染聊天记录。
    result = await db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at)
    )
    messages = result.scalars().all()
    return Resp(data=[ChatMessageOut.model_validate(m) for m in messages])


# ChatSession 与 ChatMessage 配置有关联级联时，删除会话会一并清理消息记录。
@router.delete("/sessions/{session_id}", response_model=Resp)
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("chat")),
):
    session = await _get_session_or_404(session_id, current_user, db)
    # 标记待删除；请求正常结束后由 get_db 提交事务。
    await db.delete(session)
    return Resp(message="对话已删除")


@router.post("/completions")
async def chat_completions(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("chat")),
):
    """RAG 问答，支持流式和非流式"""
    # t_req 记录整个非流式请求耗时；流式生成器内部会另行计时。
    t_req = time.perf_counter()
    logger.info(f"Chat请求开始 | user_id={current_user.id} | session_id={body.session_id} | stream={body.stream} | msg_len={len(body.message)}")

    t_session = time.perf_counter()
    # 先确认会话存在且属于当前用户（管理员除外），并取得会话绑定的 kb/model 配置。
    session = await _get_session_or_404(body.session_id, current_user, db)
    logger.debug(f"Chat阶段1-会话查询: {(time.perf_counter()-t_session)*1000:.1f}ms")

    # 获取历史消息（最近10轮）
    t_hist = time.perf_counter()
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == body.session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(20)
    )
    # SQL 先按时间倒序取最新 20 条，再 reversed 恢复为从旧到新的对话顺序。
    # 一轮通常包含 user + assistant 两条消息，因此 20 条大约对应最近 10 轮。
    history = list(reversed(result.scalars().all()))
    logger.debug(f"Chat阶段2-历史消息查询: {(time.perf_counter()-t_hist)*1000:.1f}ms | history_len={len(history)}")

    # 保存用户消息
    t_save = time.perf_counter()
    # 用户原始问题必须保存；后面的 search_query 只用于检索，不替换聊天记录中的原文。
    user_msg = ChatMessage(session_id=body.session_id, role="user", content=body.message)
    db.add(user_msg)
    await db.flush()
    logger.debug(f"Chat阶段3-保存用户消息: {(time.perf_counter()-t_save)*1000:.1f}ms | user_msg_id={user_msg.id}")

    # Query 改写（多轮对话中按需触发规则/LLM改写，仅用于检索）
    t_rewrite = time.perf_counter()
    # ORM 消息转换成模型接口常用的 {role, content} 字典列表。
    rewrite_history = [{"role": m.role, "content": m.content} for m in history]
    search_query = await rewrite_query_if_needed(
        current_query=body.message,
        history=rewrite_history,
        provider=session.llm_provider,
        db=db,
    )
    rewrite_ms = (time.perf_counter() - t_rewrite) * 1000
    if search_query != body.message:
        logger.info(f"Chat阶段3.5-Query改写: {rewrite_ms:.1f}ms | 原文=[{body.message[:30]}] | 改写=[{search_query[:50]}]")
    else:
        logger.debug(f"Chat阶段3.5-Query改写: 无需改写 ({rewrite_ms:.1f}ms)")

    # 检索相关文档（用改写后的 query）
    # sources 返回给前端用于引用展示；context 是拼接后发送给 LLM 的参考资料。
    sources = []
    context = ""
    t_retrieval = time.perf_counter()
    if session.kb_id:
        t0 = time.perf_counter()
        search_results = await RetrievalService.search(
            kb_id=session.kb_id,
            query=search_query,
            top_k=settings.RETRIEVAL_TOP_K,
            score_threshold=settings.RETRIEVAL_SCORE_THRESHOLD,
            db=db,
        )
        retrieval_ms = (time.perf_counter() - t0) * 1000
        logger.info(f"Chat阶段4-检索: {retrieval_ms:.1f}ms | top_k={settings.RETRIEVAL_TOP_K} | 命中={len(search_results)} 条")
        if search_results:
            context_parts = []
            for i, r in enumerate(search_results, 1):
                # 给每条资料增加稳定编号、文件名和页码，指导 LLM 在回答中写 [1] 等引用。
                context_parts.append(f"[{i}] 来源：{r.filename}（第{r.page_num or '-'}页）\n{r.content}")
                src = r.model_dump()  # 转换为字典，方便后续处理
                src["citation_index"] = i  # 记录原始编号，用于回答后精准匹配
                sources.append(src)
            context = "\n\n".join(context_parts)  # 合并所有文档内容，用双换行隔开
    else:
        logger.debug("Chat阶段4-检索: 跳过（kb_id=None）")

    # 构建对话历史
    messages = []
    # 最多向 LLM 发送最近 10 条历史消息，控制上下文长度和调用成本。
    for msg in history[-10:]:
        messages.append({"role": msg.role, "content": msg.content})

    if body.stream:
        # StreamingResponse 不等待完整回答，而是边迭代异步生成器边向浏览器发送 SSE 数据。
        logger.info(f"Chat阶段5-开始LLM流式调用 | provider={session.llm_provider} | model={session.llm_model} | context_len={len(context)}")
        return StreamingResponse(
            _stream_response(body, session, context, sources, messages, db),
            media_type="text/event-stream",
        )
    else:
        # 非流式模式等待 LLM 一次性返回完整 answer；第二个返回值当前没有使用。
        t_llm = time.perf_counter()
        answer, _ = await LLMService.chat(
            provider=session.llm_provider,
            model=session.llm_model,
            messages=messages,
            user_message=body.message,
            context=context,
            db=db,
        )
        llm_ms = (time.perf_counter() - t_llm) * 1000
        logger.info(f"Chat阶段5-LLM调用(非流式): {llm_ms:.1f}ms | provider={session.llm_provider} | model={session.llm_model} | 回答长度={len(answer)}字")

        # 过滤：只保留回答中实际引用的来源
        t_filter = time.perf_counter()
        cited_sources = _filter_cited_sources(answer, sources)
        logger.debug(f"Chat阶段6-过滤引用来源: {(time.perf_counter()-t_filter)*1000:.1f}ms | 原始来源={len(sources)} | 引用来源={len(cited_sources)}")

        # 数据库字段保存 JSON 字符串；没有引用时保存 None，而不是字符串 "[]"。
        sources_json = json.dumps(cited_sources, ensure_ascii=False) if cited_sources else None
        assistant_msg = ChatMessage(
            session_id=body.session_id,
            role="assistant",
            content=answer,
            sources=sources_json,
        )
        db.add(assistant_msg)
        # flush 获取 assistant_msg.id；最终 commit 由 get_db 在路由正常结束后完成。
        await db.flush()

        total_ms = (time.perf_counter() - t_req) * 1000
        logger.info(f"Chat请求完成 | 总耗时={total_ms:.1f}ms | session_id={body.session_id}")

        # 返回完整回答、实际引用来源和数据库消息 ID。
        return Resp(data={
            "answer": answer,
            "sources": cited_sources,
            "message_id": assistant_msg.id,
        })


async def _stream_response(body, session, context, sources, messages, db):
    """流式生成器"""
    # 生成器每 yield 一次，StreamingResponse 就向客户端发送一个 SSE 事件。
    # 事件格式为 data: <JSON> 加两个换行。
    full_answer = ""
    t_llm = time.perf_counter()
    t_db_save = None
    try:
        async for chunk in LLMService.chat_stream(
            provider=session.llm_provider,
            model=session.llm_model,
            messages=messages,
            user_message=body.message,
            context=context,
            db=db,
        ):
            # 一边累计完整答案用于最终保存，一边把当前增量立即发给前端。
            full_answer += chunk
            yield f"data: {json.dumps({'type': 'chunk', 'content': chunk}, ensure_ascii=False)}\n\n"

        llm_ms = (time.perf_counter() - t_llm) * 1000
        logger.info(f"Chat阶段5-LLM流式生成完成: {llm_ms:.1f}ms | provider={session.llm_provider} | model={session.llm_model} | 回答长度={len(full_answer)}字")

        # 回答生成完毕后，过滤只保留实际引用的来源
        t_filter = time.perf_counter()
        cited_sources = _filter_cited_sources(full_answer, sources)
        logger.debug(f"Chat阶段6-过滤引用来源: {(time.perf_counter()-t_filter)*1000:.1f}ms | 原始来源={len(sources)} | 引用来源={len(cited_sources)}")
        # 回答生成完才知道真正使用了哪些引用，因此来源作为单独事件发送。
        yield f"data: {json.dumps({'type': 'sources', 'sources': cited_sources}, ensure_ascii=False)}\n\n"

        # 保存助手消息
        t_db_save = time.perf_counter()
        sources_json = json.dumps(cited_sources, ensure_ascii=False) if cited_sources else None
        assistant_msg = ChatMessage(
            session_id=body.session_id,
            role="assistant",
            content=full_answer,
            sources=sources_json,
        )
        db.add(assistant_msg)
        # 流式响应在路由函数返回后才持续运行，因此这里显式提交，确保回答及时持久化。
        await db.commit()
        logger.debug(f"Chat阶段7-保存助手消息并提交: {(time.perf_counter()-t_db_save)*1000:.1f}ms | assistant_msg_id={assistant_msg.id}")

        # done 告诉前端流已正常结束，并提供已保存消息的 ID。
        yield f"data: {json.dumps({'type': 'done', 'message_id': assistant_msg.id}, ensure_ascii=False)}\n\n"
        logger.info(f"Chat流式请求完成 | 总耗时={(time.perf_counter()-t_llm)*1000:.1f}ms")
    except Exception as e:
        # 流已经开始后无法再改成普通 HTTP 错误响应，因此把异常包装为 SSE error 事件。
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"


async def _get_session_or_404(session_id: int, user: User, db: AsyncSession) -> ChatSession:
    # 管理员按 session_id 查询任意会话；普通用户把 user_id 条件放进 SQL，避免越权读取。
    if user.is_admin:
        result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    else:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id)
        )
    session = result.scalar_one_or_none()
    if not session:
        # 对“不存在”和“不是当前用户的会话”统一返回 404，减少资源枚举信息泄露。
        raise HTTPException(status_code=404, detail="对话不存在")
    return session
