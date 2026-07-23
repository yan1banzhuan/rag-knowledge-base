# =============================================================================
# 文件作用与架构位置（混合检索与精排服务）
# =============================================================================
# 本文件是 RAG 的“找资料”核心。它同时使用语义向量检索和关键词 BM25 检索，把两边分数
# 融合后可选地交给 Cross-Encoder Reranker 精排，最终返回 SearchResult 给聊天或搜索接口。
#
# 本文件共有 9 个函数/方法：
#
#   RetrievalService.search()                从 query 开始，先计算 Embedding
#   RetrievalService.search_with_embedding() 复用调用方已有查询向量
#   RetrievalService._search_with_embedding()完整混合检索主流程
#   _build_results()                         内部结果 -> SearchResult
#   _bm25_search()                           关键词召回和归一化
#   _get_bm25_corpus()                       Redis/DB 获取分词语料
#   _batch_lookup_chunks()                   补全纯 BM25 命中的文本和元数据
#   _build_where()                           构造 Chroma metadata 过滤条件
#   _compute_dynamic_weights()               根据问题特征调整融合权重
#
# 完整流程：
#
#                         query
#                    +------+------+
#                    |             |
#                    v             v
#             Embedding 向量     jieba 分词
#                    |             |
#                    v             v
#             ChromaDB 语义召回   BM25 关键词召回
#                    |             |
#                    +------分数融合+
#                           |
#                      粗排候选扩大
#                           |
#                   Cross-Encoder Reranker
#                           |
#                 低分时按策略降级/混合
#                           |
#                      最终 top_k 资料
#
# BM25 语料缓存在 Redis；文档入库或删除后 DocumentService 会删除缓存以触发重建。
# =============================================================================

from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.vector_store import VectorStore
from app.models.db import DocumentChunk, Document
from app.models.schemas import SearchResult
from app.services.embedding import EmbeddingService
from app.core.config import settings
from app.core.logger import logger
import jieba
import time


class RetrievalService:

    @staticmethod
    async def search(
        kb_id: int,
        query: str,
        top_k: int = None,
        score_threshold: float = None,
        file_type: Optional[str] = None,
        tags: Optional[str] = None,
        db: AsyncSession = None,
    ) -> List[SearchResult]:
        # None 或 0 会使用默认 top_k；score_threshold 明确允许传 0.0。
        top_k = top_k or settings.RETRIEVAL_TOP_K
        score_threshold = score_threshold if score_threshold is not None else settings.RETRIEVAL_SCORE_THRESHOLD

        # 1. Query Embedding
        # embed_query 是同步模型/API 调用，当前直接在 async 函数中执行，期间可能占用事件循环。
        t0 = time.perf_counter()
        query_embedding = EmbeddingService.embed_query(query)
        embed_ms = (time.perf_counter() - t0) * 1000
        logger.debug(f"检索-Query embedding: {embed_ms:.1f}ms")
        return await RetrievalService._search_with_embedding(
            kb_id=kb_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
            score_threshold=score_threshold,
            file_type=file_type,
            tags=tags,
            db=db,
        )

    @staticmethod
    async def search_with_embedding(
        kb_id: int,
        query: str,
        query_embedding: List[float],
        top_k: int = None,
        score_threshold: float = None,
        file_type: Optional[str] = None,
        tags: Optional[str] = None,
        db: AsyncSession = None,
    ) -> List[SearchResult]:
        # 调用方已经计算过向量时使用此入口，避免重复 Embedding。
        top_k = top_k or settings.RETRIEVAL_TOP_K
        score_threshold = score_threshold if score_threshold is not None else settings.RETRIEVAL_SCORE_THRESHOLD
        return await RetrievalService._search_with_embedding(
            kb_id=kb_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
            score_threshold=score_threshold,
            file_type=file_type,
            tags=tags,
            db=db,
        )

    @staticmethod
    async def _search_with_embedding(
        kb_id: int,
        query: str,
        query_embedding: List[float],
        top_k: int,
        score_threshold: float,
        file_type: Optional[str] = None,
        tags: Optional[str] = None,
        db: AsyncSession = None,
    ) -> List[SearchResult]:

        # 2. ChromaDB 向量检索
        t1 = time.perf_counter()
        # collection 已按 kb_id 隔离；where 只需附加文件类型/标签筛选。
        where_filter = _build_where(kb_id, file_type, tags)
        vector_results = VectorStore.query(
            kb_id=kb_id,
            query_embedding=query_embedding,
            # 先召回最终数量的 2 倍，为融合和重排保留更多候选。
            top_k=top_k * 2,
            where=where_filter,
        )
        chroma_ms = (time.perf_counter() - t1) * 1000
        vec_count = len(vector_results.get("ids", [[]])[0]) if vector_results else 0
        logger.debug(f"检索-ChromaDB查询: {chroma_ms:.1f}ms | 命中向量={vec_count} 条")

        # 3. 解析向量检索结果
        t_parse = time.perf_counter()
        vec_scores: Dict[str, float] = {}
        vec_docs: Dict[str, dict] = {}

        if vector_results["ids"] and vector_results["ids"][0]:
            for chroma_id, distance, doc_text, metadata in zip(
                vector_results["ids"][0],
                vector_results["distances"][0],
                vector_results["documents"][0],
                vector_results["metadatas"][0],
            ):
                # collection 使用 cosine 距离；距离越小越相似，转换后分数越大越好。
                similarity = 1.0 - distance
                vec_scores[chroma_id] = similarity
                vec_docs[chroma_id] = {"text": doc_text, "metadata": metadata}
        parse_ms = (time.perf_counter() - t_parse) * 1000
        logger.debug(f"检索-解析向量结果: {parse_ms:.1f}ms | vec_scores={len(vec_scores)}")

        # 4. BM25 检索
        t_bm25 = time.perf_counter()
        bm25_scores = await _bm25_search(kb_id, query, top_k * 2, db)
        bm25_ms = (time.perf_counter() - t_bm25) * 1000
        logger.debug(f"检索-BM25检索: {bm25_ms:.1f}ms | bm25命中={len(bm25_scores)} 条")

        # 5. 合并所有候选
        # 集合并集确保只被其中一种检索方式命中的 chunk 也不会丢失。
        all_ids = set(vec_scores.keys()) | set(bm25_scores.keys())

        # 6. 计算加权混合分数，按此排序取 Top-K
        t_fusion = time.perf_counter()

        vec_w, bm25_w = _compute_dynamic_weights(
            vec_scores=vec_scores,
            bm25_scores=bm25_scores,
            query=query,
            base_vec_w=settings.VECTOR_WEIGHT,
            base_bm25_w=settings.BM25_WEIGHT,
        )

        combined_scores: Dict[str, float] = {}
        for chroma_id in all_ids:
            # 某侧未命中时分数按 0 处理。
            vec_s = vec_scores.get(chroma_id, 0.0)
            bm25_s = bm25_scores.get(chroma_id, 0.0)
            combined_scores[chroma_id] = vec_s * vec_w + bm25_s * bm25_w

        # reverse=True 让相关性最高的候选排在最前。
        sorted_ids = sorted(combined_scores.keys(), key=lambda x: combined_scores[x], reverse=True)
        fusion_ms = (time.perf_counter() - t_fusion) * 1000
        logger.debug(f"检索-分数融合: {fusion_ms:.1f}ms | 候选数={len(all_ids)} | vec_weight={vec_w} | bm25_weight={bm25_w}")

        # 7. 收集候选文档（含纯 BM25 命中的 fallback DB 补全）
        rerank_multiplier = settings.RERANK_MULTIPLIER if settings.RERANK_ENABLED else 1
        # 精排通常先取 top_k 的若干倍；候选不足时不超过实际长度。
        coarse_top_n = min(top_k * rerank_multiplier, len(sorted_ids))
        coarse_ids = sorted_ids[:coarse_top_n]

        # 纯 BM25 命中没有 Chroma 返回的 document/metadata，需要从 MySQL DocumentChunk 补全。
        missing_ids = [cid for cid in coarse_ids if cid not in vec_docs]
        fallback_map = await _batch_lookup_chunks(missing_ids, db) if missing_ids and db else {}

        # 8. Cross-Encoder 重排序（精排阶段）
        t_rerank = time.perf_counter()
        if settings.RERANK_ENABLED and coarse_ids and len(coarse_ids) > 1:
            from app.services.reranker import RerankerService

            candidates = []
            for cid in coarse_ids:
                info = vec_docs.get(cid) or fallback_map.get(cid)
                if info:
                    candidates.append((cid, info["text"]))

            # Reranker 只需要 (ID, 文本)，返回与 candidates 相同顺序的分数。
            rerank_scores = RerankerService.rerank(query, candidates)

            rerank_map: Dict[str, float] = {}
            for (cand_id, _), rs in zip(candidates, rerank_scores):
                rerank_map[cand_id] = rs

            # 精排阶段不再混合粗排分数，直接按 Cross-Encoder 分数排序。
            final_scores = rerank_map
            final_sorted = sorted(final_scores.keys(), key=lambda x: final_scores[x], reverse=True)[:top_k]
            rerank_ms = (time.perf_counter() - t_rerank) * 1000
            logger.info(f"检索-Reranker精排: {rerank_ms:.1f}ms | 粗排候选={len(coarse_ids)} | 精排后={len(final_sorted)}")
        else:
            final_scores = combined_scores
            final_sorted = coarse_ids[:top_k]
            if settings.RERANK_ENABLED and coarse_ids:
                logger.debug(f"检索-Reranker跳过: 候选数={len(coarse_ids)}（<=1 条无需重排）")

        # 9. 构建返回结果（Reranker 三层降级策略）
        use_reranked = settings.RERANK_ENABLED and len(coarse_ids) > 1
        results: List[SearchResult] = []

        if use_reranked:
            top_score = final_scores.get(final_sorted[0], 0) if final_sorted else 0

            if top_score < 0.1:
                # 精排整体置信度极低时，认为模型不可靠，完全回退融合粗排。
                logger.info(f"检索-Reranker降级: top_score={top_score:.4f}<0.1, 回退粗排")
                results = _build_results(coarse_ids[:top_k], combined_scores, score_threshold, vec_docs, fallback_map)
            elif top_score < 0.3:
                # 中低置信度时先放精排结果，再用粗排补足未出现的文档。
                logger.info(f"检索-Reranker混合: top_score={top_score:.4f}∈[0.1,0.3), 混合结果")
                reranked = _build_results(final_sorted[:top_k], final_scores, 0.0, vec_docs, fallback_map)
                coarse = _build_results(coarse_ids[:top_k], combined_scores, score_threshold, vec_docs, fallback_map)
                # 这里按 doc_id 去重，因此同一文档的多个 chunk 在混合补足阶段只保留一个。
                seen = {r.doc_id for r in reranked}
                for r in coarse:
                    if r.doc_id not in seen:
                        reranked.append(r)
                        seen.add(r.doc_id)
                results = reranked[:top_k]
            else:
                results = _build_results(final_sorted[:top_k], final_scores, 0.0, vec_docs, fallback_map)
        else:
            results = _build_results(coarse_ids[:top_k], combined_scores, score_threshold, vec_docs, fallback_map)

        logger.debug(f"检索完成: kb={kb_id}, query={query[:30]}, 命中={len(results)}")
        return results


def _build_results(
    ids: List[str],
    scores: Dict[str, float],
    threshold: float,
    vec_docs: Dict[str, dict],
    fallback_map: Dict[str, dict],
) -> List[SearchResult]:
    # 统一完成阈值过滤、文本查找、元数据默认值和 Pydantic 输出转换。
    results = []
    for chroma_id in ids:
        score = scores.get(chroma_id, 0.0)
        if score < threshold:
            # 低于阈值的候选不返回；Reranker 高置信路径传入的 threshold 是 0.0。
            continue
        info = vec_docs.get(chroma_id) or fallback_map.get(chroma_id)
        if not info:
            # Chroma 和数据库都找不到正文时无法构建可用来源，跳过孤立 ID。
            continue
        meta = info["metadata"]
        results.append(SearchResult(
            doc_id=meta.get("doc_id", 0),
            filename=meta.get("filename", ""),
            file_type=meta.get("file_type", ""),
            chunk_index=meta.get("chunk_index", 0),
            page_num=meta.get("page_num") or None,
            content=info["text"],
            score=round(score, 4),
            tags=meta.get("tags") or None,
        ))
    return results


async def _bm25_search(
    kb_id: int, query: str, top_k: int, db: AsyncSession
) -> Dict[str, float]:
    """基于 BM25 的关键词检索（Redis 缓存分词语料加速）"""
    if not db:
        # BM25 语料来自 MySQL，没有会话时只能降级纯向量检索。
        return {}

    try:
        from rank_bm25 import BM25Okapi

        tokenized_corpus, chroma_ids = await _get_bm25_corpus(kb_id, db)
        # 数据块太少时 BM25 统计意义有限，当前实现至少要求 3 个 chunk。
        if not tokenized_corpus or len(tokenized_corpus) <= 2:
            return {}

        # jieba 把中文句子切成关键词序列；语料已提前按相同方式分词。
        query_tokens = list(jieba.cut(query))
        bm25 = BM25Okapi(tokenized_corpus)
        scores = bm25.get_scores(query_tokens)

        # 除以最高分把 BM25 分数压到大致 0~1，便于和向量相似度加权。
        max_score = max(scores) if max(scores) > 0 else 1.0
        bm25_result = {}
        for i, cid in enumerate(chroma_ids):
            if scores[i] > 0:
                bm25_result[cid] = scores[i] / max_score

        sorted_ids = sorted(bm25_result.keys(), key=lambda x: bm25_result[x], reverse=True)[:top_k]
        return {k: bm25_result[k] for k in sorted_ids}

    except Exception as e:
        # BM25 是增强召回，失败后返回空字典，动态权重会把向量侧权重提升为 1。
        logger.warning(f"BM25 检索失败，降级为纯向量检索: {e}")
        return {}


async def _get_bm25_corpus(kb_id: int, db: AsyncSession):
    """获取 BM25 语料（优先从 Redis 缓存读取）"""
    from app.core.redis_client import cache_get_json, cache_set_json

    cache_key = f"kb:{kb_id}:bm25"

    # 缓存内容包含与顺序一一对应的 ids 和已分词 corpus。
    cached = await cache_get_json(cache_key)
    if cached and "ids" in cached and "corpus" in cached:
        logger.debug(f"BM25 缓存命中: kb_id={kb_id}, chunks={len(cached['ids'])}")
        return cached["corpus"], cached["ids"]

    t_db = time.perf_counter()
    # 缓存未命中时从 document_chunks 读取全部 chunk ID 和正文。
    result = await db.execute(
        select(DocumentChunk.chroma_id, DocumentChunk.content)
        .where(DocumentChunk.kb_id == kb_id)
    )
    rows = result.all()
    db_ms = (time.perf_counter() - t_db) * 1000

    if not rows or len(rows) <= 2:
        return [], []

    t_jieba = time.perf_counter()
    chroma_ids = [row.chroma_id for row in rows]
    # 分词是相对耗时步骤，完成后按 TTL 缓存，避免每次检索重复执行。
    tokenized_corpus = [list(jieba.cut(row.content)) for row in rows]
    jieba_ms = (time.perf_counter() - t_jieba) * 1000
    logger.info(f"BM25 缓存未命中，重新构建: db={db_ms:.1f}ms jieba={jieba_ms:.1f}ms chunks={len(rows)}")

    await cache_set_json(cache_key, {"ids": chroma_ids, "corpus": tokenized_corpus}, ttl=settings.BM25_CACHE_TTL)

    return tokenized_corpus, chroma_ids


async def _batch_lookup_chunks(
    chroma_ids: List[str], db: AsyncSession
) -> Dict[str, dict]:
    """批量查询 BM25-only 结果的 chunk 元数据（含文档名/类型）"""
    if not chroma_ids or db is None:
        return {}
    try:
        # 一次 IN + JOIN 查询补全多个 chunk，避免逐个 ID 查询造成 N+1。
        result = await db.execute(
            select(
                DocumentChunk.chroma_id,
                DocumentChunk.content,
                DocumentChunk.doc_id,
                DocumentChunk.chunk_index,
                DocumentChunk.page_num,
                Document.filename,
                Document.file_type,
                Document.tags,
            )
            .join(Document, DocumentChunk.doc_id == Document.id)
            .where(DocumentChunk.chroma_id.in_(chroma_ids))
        )
        rows = result.all()
        fallback = {}
        for row in rows:
            fallback[row.chroma_id] = {
                "text": row.content,
                "metadata": {
                    "doc_id": row.doc_id,
                    "filename": row.filename or "",
                    "file_type": row.file_type or "",
                    "chunk_index": row.chunk_index or 0,
                    "page_num": row.page_num,
                    "tags": row.tags or "",
                },
            }
        logger.debug(f"BM25 fallback 批量查DB: {len(chroma_ids)} IDs → {len(fallback)} 命中")
        return fallback
    except Exception as e:
        logger.warning(f"BM25 fallback DB查询失败: {e}")
        return {}


def _build_where(kb_id: int, file_type: Optional[str], tags: Optional[str]) -> Optional[Dict]:
    # kb_id 参数当前未直接使用，因为调用方已经选择了 kb_{kb_id} collection。
    conditions = []
    if file_type:
        conditions.append({"file_type": {"$eq": file_type}})
    if tags:
        conditions.append({"tags": {"$contains": tags}})

    if not conditions:
        # None 表示不向 Chroma 添加 metadata 过滤。
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _compute_dynamic_weights(
    vec_scores: Dict[str, float],
    bm25_scores: Dict[str, float],
    query: str,
    base_vec_w: float,
    base_bm25_w: float,
) -> tuple:
    """
    动态计算向量与 BM25 的融合权重 — 独立叠加修正，避免互斥盲区。

    策略（独立施加修正，不互斥）：
    1. 单侧无结果 → 另一方权重升至 1.0（兜底）
    2. 查询含精确引用词 → BM25 加分
    3. 查询含具体数量词 → BM25 加分
    4. 查询为疑问/语义型 → 向量加分
    5. 双方结果重叠度极低 → 均衡权重
    """
    import re

    # ── 兜底：单侧无结果 ──
    if len(vec_scores) == 0 and len(bm25_scores) > 0:
        return 0.0, 1.0
    if len(bm25_scores) == 0 and len(vec_scores) > 0:
        return 1.0, 0.0
    if len(vec_scores) == 0 and len(bm25_scores) == 0:
        return base_vec_w, base_bm25_w

    # 从配置基础权重开始，后续规则分别增减。
    vec_w, bm25_w = base_vec_w, base_bm25_w
    adjustments = []

    # ── 策略 2：精确引用词 → BM25 加分 ──
    exact_ref_patterns = [
        r"第[一二三四五六七八九十\d]+[条条款节段落]",
        r"\d+\s*[条条款节]",
        r"第\s*\d+\s*[页行款]",
    ]
    has_exact_ref = any(re.search(p, query) for p in exact_ref_patterns)
    if has_exact_ref:
        # 条款号、页码等字面信息更适合关键词精确匹配。
        vec_w -= 0.20
        bm25_w += 0.20
        adjustments.append("精确引用词→BM25+0.2")

    # ── 策略 3：具体数量查询 → BM25 加分 ──
    quantity_patterns = [
        r"\d+\s*次", r"\d+\s*元", r"\d+\s*分钟",
        r"\d+\s*小时", r"\d+\s*天", r"\d+\s*%",
    ]
    has_quantity = any(re.search(p, query) for p in quantity_patterns)
    if has_quantity:
        # 具体数字与单位容易在语义向量中弱化，因此提高 BM25。
        vec_w -= 0.10
        bm25_w += 0.10
        adjustments.append("数量查询→BM25+0.1")

    # ── 策略 4：疑问/语义型 → 向量加分 ──
    semantic_indicators = ("怎么", "如何", "为什么", "什么", "多少", "吗", "呢", "？", "?")
    is_semantic = any(w in query for w in semantic_indicators)
    if is_semantic and not has_exact_ref and not has_quantity:
        # 普通解释型问题更依赖整体语义，且不给与前两类规则同时叠加。
        vec_w += 0.15
        bm25_w -= 0.15
        adjustments.append("语义疑问→向量+0.15")

    # ── 策略 5：重叠度极低 → 均衡 ──
    vec_ids = set(vec_scores.keys())
    bm_ids = set(bm25_scores.keys())
    overlap = len(vec_ids & bm_ids)
    total_unique = len(vec_ids | bm_ids)
    if overlap <= 1 and total_unique >= 3:
        # 两种召回几乎不重叠时，向 0.5/0.5 靠拢，减少单侧偏见。
        vec_w = (vec_w + 0.5) / 2
        bm25_w = (bm25_w + 0.5) / 2
        adjustments.append("低重叠→均衡")

    # ── 钳制到安全区间 ──
    # 保证有结果的两种检索至少保留 0.15 权重，避免完全关闭某一侧。
    vec_w = max(0.15, min(0.85, vec_w))
    bm25_w = max(0.15, min(0.85, bm25_w))

    if adjustments:
        logger.debug(f"动态权重: {', '.join(adjustments)} | 最终 vec={vec_w:.2f} bm25={bm25_w:.2f}")

    return vec_w, bm25_w
