# =============================================================================
# 文件作用与架构位置（仪表盘统计路由）
# =============================================================================
# 本文件聚合知识库、文档、文本块和聊天数据，向前端 Dashboard 提供概览与趋势。它只
# 读取数据库，不生成新的业务记录。
#
# 本文件共有 6 个函数：
#
#   _get_all_kb_ids()  取得全部知识库 ID，供多个统计接口复用
#   stats_overview()   总知识库数、文档数、文件体积和文本块数
#   stats_by_kb()      每个知识库的状态、类型和大小明细
#   stats_parse()      文档解析成功/失败/等待数量与比例
#   stats_chat_daily() 按天统计会话和消息数量
#   _format_size()     把字节数格式化为 B/KB/MB/GB
#
#   GET /stats/* -> require_permission("stats") -> SQL 聚合/分组 -> JSON
#
# 当前设计中，具有 stats 菜单权限的用户看到的是全局数据，而不是仅自己可访问的知识库。
# =============================================================================

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.session import get_db
from app.models.db import User, KnowledgeBase, Document, DocumentStatus, ChatSession, ChatMessage
from app.api.deps import require_permission
from datetime import datetime, timedelta
from collections import defaultdict

router = APIRouter(prefix="/stats", tags=["统计"])


async def _get_all_kb_ids(db: AsyncSession) -> list[int]:
    """获取所有知识库的 ID 列表（仪表盘显示所有数据）"""
    # 只查询 id 列比加载完整 KnowledgeBase ORM 对象更轻量。
    result = await db.execute(select(KnowledgeBase.id))
    # 每一行形如 (17,)，row[0] 取出整数 ID。
    return [row[0] for row in result.fetchall()]


@router.get("/overview")
async def stats_overview(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("stats")),
):
    # 仪表盘显示所有知识库数据（不受权限过滤）
    visible_kb_ids = await _get_all_kb_ids(db)

    if not visible_kb_ids:
        # 提前返回避免执行 IN 空列表和多次无意义聚合。
        return {
            "total_kbs": 0,
            "total_docs": 0,
            "total_size_bytes": 0,
            "total_size_display": "0 B",
            "total_chunks": 0,
        }

    # 一条 SQL 同时统计文档数量和文件总字节数；coalesce 把 SQL NULL 转为 0。
    row = await db.execute(
        select(
            func.count(Document.id),
            func.coalesce(func.sum(Document.file_size), 0),
        )
        .where(Document.kb_id.in_(visible_kb_ids))
    )
    # 聚合查询固定产生一行，r[0] 是文档数，r[1] 是总大小。
    r = row.one()

    # chunk_count 是每个文档分块数量的缓存字段，求和得到总文本块数。
    total_chunks = await db.scalar(
        select(func.coalesce(func.sum(Document.chunk_count), 0))
        .where(Document.kb_id.in_(visible_kb_ids))
    ) or 0

    # 同时返回原始字节数和适合界面展示的字符串，兼顾计算和显示需求。
    return {
        "total_kbs": len(visible_kb_ids),
        "total_docs": r[0] or 0,
        "total_size_bytes": r[1] or 0,
        "total_size_display": _format_size(r[1] or 0),
        "total_chunks": total_chunks,
    }


@router.get("/kbs")
async def stats_by_kb(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("stats")),
):
    # 仪表盘显示所有知识库数据（不受权限过滤）
    visible_kb_ids = await _get_all_kb_ids(db)
    if not visible_kb_ids:
        return []

    kbs = await db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.id.in_(visible_kb_ids))
        .order_by(KnowledgeBase.created_at.desc())
    )
    kbs = kbs.scalars().all()

    result = []
    # 当前实现逐个知识库查询其文档，写法直观；知识库很多时会形成 N+1 次查询。
    for kb in kbs:
        docs_q = await db.execute(select(Document).where(Document.kb_id == kb.id))
        docs = docs_q.scalars().all()

        # Python 层聚合每个知识库的文件大小、状态数量和文件类型明细。
        total_size = sum(d.file_size or 0 for d in docs)
        # defaultdict 在键首次出现时自动提供默认值，省去手动初始化。
        status_map = defaultdict(int)
        type_map: dict = defaultdict(lambda: {"count": 0, "size": 0})

        for d in docs:
            # Document.status 是枚举，.value 得到 completed/failed 等字符串。
            status_map[d.status.value] += 1
            type_map[d.file_type]["count"] += 1
            type_map[d.file_type]["size"] += d.file_size or 0

        # 一个知识库转换成一个普通字典，最终列表直接序列化为 JSON。
        result.append({
            "kb_id": kb.id,
            "kb_name": kb.name,
            "doc_count": len(docs),
            "total_size_bytes": total_size,
            "total_size_display": _format_size(total_size),
            "success_count": status_map.get("completed", 0),
            "failed_count": status_map.get("failed", 0),
            "pending_count": status_map.get("pending", 0),
            "file_type_breakdown": {
                ft: {
                    "count": v["count"],
                    "size": v["size"],
                    "size_display": _format_size(v["size"]),
                }
                for ft, v in type_map.items()
            },
        })
    return result


@router.get("/parse")
async def stats_parse(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("stats")),
):
    # 仪表盘显示所有知识库数据（不受权限过滤）
    visible_kb_ids = await _get_all_kb_ids(db)
    if not visible_kb_ids:
        return {
            "total": 0, "success": 0, "failed": 0, "pending": 0,
            "success_rate": 0.0, "failed_rate": 0.0,
        }

    # 分别用 COUNT + WHERE 计算全部、成功、失败和等待数量。
    total = await db.scalar(
        select(func.count()).select_from(Document).where(Document.kb_id.in_(visible_kb_ids))
    ) or 0
    success = await db.scalar(
        select(func.count()).select_from(Document)
        .where(Document.kb_id.in_(visible_kb_ids))
        .where(Document.status == DocumentStatus.completed)
    ) or 0
    failed = await db.scalar(
        select(func.count()).select_from(Document)
        .where(Document.kb_id.in_(visible_kb_ids))
        .where(Document.status == DocumentStatus.failed)
    ) or 0
    pending = await db.scalar(
        select(func.count()).select_from(Document)
        .where(Document.kb_id.in_(visible_kb_ids))
        .where(Document.status == DocumentStatus.pending)
    ) or 0

    # total 为 0 时不做除法，防止 ZeroDivisionError；round(..., 4) 保留四位小数。
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "pending": pending,
        "success_rate": round(success / total, 4) if total else 0,
        "failed_rate": round(failed / total, 4) if total else 0,
    }


@router.get("/chat/daily")
async def stats_chat_daily(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("stats")),
):
    # 例如 days=30 表示从当前本地时间往前 30 天。
    since = datetime.now() - timedelta(days=days)

    # 按会话创建日期分组。LEFT OUTER JOIN 让没有消息的会话仍能进入统计。
    rows = await db.execute(
        select(
            func.date(ChatSession.created_at).label("date"),
            func.count(func.distinct(ChatSession.id)).label("sessions"),
            func.count(ChatMessage.id).label("messages"),
        )
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id, isouter=True)
        .where(ChatSession.created_at >= since)
        .group_by(func.date(ChatSession.created_at))
        .order_by(func.date(ChatSession.created_at))
    )

    # SQL 结果行转换成前端图表容易使用的字典列表。
    return [
        {
            "date": str(r.date),
            "sessions": r.sessions or 0,
            "messages": r.messages or 0,
        }
        for r in rows.all()
    ]


def _format_size(size_bytes: int) -> str:
    # 依次选择最合适单位，小于 1024 字节时不显示小数。
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    else:
        # 当前最大显示单位为 GB；更大的值仍按 GB 显示。
        return f"{size_bytes / 1024 ** 3:.1f} GB"
