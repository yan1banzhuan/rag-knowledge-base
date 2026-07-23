# =============================================================================
# 文件作用与架构位置（LangChain Embeddings 适配层）
# =============================================================================
# 该类把项目配置转换成 LangChain Embeddings 接口，供 LangChainVectorStore 使用。
# 当前主流程使用 services/embedding.py；本文件属于 LangChain 兼容路径。
#
#   get_embeddings()  创建并缓存 OpenAIEmbeddings/HuggingFaceEmbeddings
#   embed_texts()      文档列表向量化
#   embed_query()      查询向量化
# =============================================================================

from typing import List, Optional
from app.core.config import settings
from app.core.logger import logger
from langchain.embeddings import HuggingFaceEmbeddings, OpenAIEmbeddings
from langchain.embeddings.base import Embeddings


class LangChainEmbeddingService:
    # 类属性缓存 LangChain Embeddings 对象，避免重复加载本地模型。
    _embeddings: Optional[Embeddings] = None

    @classmethod
    def get_embeddings(cls) -> Embeddings:
        """获取LangChain嵌入实例"""
        if cls._embeddings is None:
            if settings.EMBEDDING_PROVIDER == "openai":
                cls._embeddings = OpenAIEmbeddings(
                    openai_api_key=settings.OPENAI_API_KEY,
                    openai_api_base=settings.OPENAI_BASE_URL,
                    model=settings.OPENAI_EMBEDDING_MODEL,
                )
            else:
                # 本兼容实现固定使用 CPU，并对向量归一化。
                cls._embeddings = HuggingFaceEmbeddings(
                    model_name=settings.EMBEDDING_MODEL,
                    model_kwargs={"device": "cpu"},
                    encode_kwargs={"normalize_embeddings": True},
                )
            logger.info(f"加载LangChain嵌入模型: {settings.EMBEDDING_MODEL}")
        return cls._embeddings

    @classmethod
    def embed_texts(cls, texts: List[str]) -> List[List[float]]:
        """嵌入文本列表"""
        if not texts:
            return []
        embeddings = cls.get_embeddings()
        return embeddings.embed_documents(texts)

    @classmethod
    def embed_query(cls, query: str) -> List[float]:
        """嵌入查询文本"""
        embeddings = cls.get_embeddings()
        return embeddings.embed_query(query)
