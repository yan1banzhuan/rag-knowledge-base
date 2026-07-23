# =============================================================================
# 文件作用与架构位置（Cross-Encoder 精排服务）
# =============================================================================
# 初步向量/BM25 检索速度快，但排序不一定足够准确。Reranker 同时阅读 query 和每个候选
# 文本，计算更精确的相关性分数，再由 RetrievalService 重新排序。
#
# 本文件有 3 个函数/方法：
#
#   _get_reranker()  延迟加载并缓存 FlagReranker 模型
#   preload()        应用启动时预热模型
#   rerank()         候选 [(id, text)] -> [0~1 分数]
#
#   向量/BM25 召回较多候选 -> Reranker 精排 -> 截取最终 top_k
# =============================================================================

# math.exp 用于 sigmoid；time 用于统计模型推理耗时。
import math
import time
from typing import List, Tuple
from app.core.config import settings
from app.core.logger import logger

try:
    # FlagEmbedding 是可选依赖；导入失败时应用其他功能仍可启动。
    from FlagEmbedding import FlagReranker
    _FLAG_EMBEDDING_AVAILABLE = True
except ImportError:
    FlagReranker = None
    _FLAG_EMBEDDING_AVAILABLE = False

_cached_reranker = None
_cached_reranker_model: str = ""


def _get_reranker():
    global _cached_reranker, _cached_reranker_model
    if not _FLAG_EMBEDDING_AVAILABLE:
        # 只有真正启用/调用精排时才给出明确安装提示。
        raise RuntimeError("FlagEmbedding 未安装，请执行: pip install FlagEmbedding")
    model_name = settings.RERANK_MODEL
    if _cached_reranker is None or _cached_reranker_model != model_name:
        logger.info(f"Reranker 模型首次加载: {model_name}")
        # 优先尝试 local_files_only，模型已在缓存时直接加载
        try:
            # local_files_only 避免已有缓存时仍访问网络。
            _cached_reranker = FlagReranker(model_name, use_fp16=False, local_files_only=True)
            logger.info("Reranker 模型从本地缓存加载完成")
        except Exception:
            # 本地未命中或离线加载失败时，再允许从模型 Hub 下载。
            logger.info("Reranker 本地缓存未命中，尝试从 Hub 下载...")
            _cached_reranker = FlagReranker(model_name, use_fp16=False)
            logger.info("Reranker 模型下载并加载完成")
        _cached_reranker_model = model_name
    return _cached_reranker


class RerankerService:

    @classmethod
    def preload(cls):
        # 主动调用缓存加载器，避免第一次用户检索承担冷启动延迟。
        _get_reranker()
        logger.info(f"Reranker 模型 {settings.RERANK_MODEL} 预加载完成")

    @classmethod
    def rerank(
        cls,
        query: str,
        candidates: List[Tuple[str, str]],
    ) -> List[float]:
        """
        对候选文档列表进行 Cross-Encoder 重排序。

        Args:
            query: 用户查询
            candidates: [(chroma_id, doc_text), ...]

        Returns:
            与 candidates 等长的相关性分数列表
        """
        if not candidates:
            return []

        reranker = _get_reranker()
        # Cross-Encoder 输入是一组 [query, document] 对；chroma_id 不参与模型计算。
        pairs = [[query, text] for _, text in candidates]
        t0 = time.perf_counter()
        logits = reranker.compute_score(pairs, batch_size=8)
        elapsed = (time.perf_counter() - t0) * 1000

        if isinstance(logits, float):
            # 只有一个候选时某些版本返回单个 float，统一包装成列表。
            logits = [logits]
        # sigmoid 把任意实数 logit 映射到 0~1，便于与阈值和其他分数理解。
        scores = [1.0 / (1.0 + math.exp(-s)) for s in logits]
        logger.debug(f"Reranker 精排: {elapsed:.1f}ms | candidates={len(candidates)} | "
                     f"scores={[round(s, 4) for s in scores]}")
        return scores
