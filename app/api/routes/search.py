# =============================================================================
# 文件作用与架构位置（独立检索测试接口）
# =============================================================================
# 本文件只有 1 个 search() 路由。它绕过聊天和 LLM 生成步骤，直接调用 RetrievalService
# 返回相关文档片段，适合调试知识库检索效果、阈值、文件类型和标签过滤。
#
#   POST /api/v1/search
#       |
#       v
#   SearchRequest 校验
#       |
#       v
#   身份认证 + kb_manage 权限
#       |
#       v
#   RetrievalService.search()
#       |
#       v
#   SearchResponse(query, results, total)
#
# 它位于 API 路由层；实际向量检索、BM25、融合和重排逻辑都在 services/retrieval.py。
# =============================================================================

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.models.db import User
from app.models.schemas import SearchRequest, SearchResponse
from app.api.deps import get_current_user, require_permission
from app.services.retrieval import RetrievalService

router = APIRouter(prefix="/search", tags=["检索"])


@router.post("", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    """测试检索接口，直接返回相关文档片段"""
    # body 中的参数原样传给服务层；db 用于 BM25 文档读取、配置查询等异步数据库操作。
    results = await RetrievalService.search(
        kb_id=body.kb_id,
        query=body.query,
        top_k=body.top_k,
        score_threshold=body.score_threshold,
        file_type=body.file_type,
        tags=body.tags,
        db=db,
    )
    # total 是实际通过阈值和过滤条件后返回的结果数量，不一定等于 top_k。
    return SearchResponse(query=body.query, results=results, total=len(results))
