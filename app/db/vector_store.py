# =============================================================================
# 文件作用与架构位置（ChromaDB 向量存储适配层）
# =============================================================================
# RAG 系统会把文档文本切成多个 chunk，再把每个 chunk 转成数字向量。关系数据库适合
# 保存用户、知识库和文档元数据，而本文件使用 ChromaDB 保存并检索这些高维向量。
#
# 数据流：
#
#   文档文本块 -> Embedding 模型 -> List[float]
#                                   |
#                                   v
#                          VectorStore.add_documents()
#                                   |
#                                   v
#                     Chroma collection：kb_{知识库ID}
#                                   |
#   用户问题 -> 查询向量 ----------> query() -> 最相似的文本块
#
# VectorStore 共有 7 个类方法：
#
#   get_client()          创建/复用 Chroma 持久化客户端
#   get_collection()      取得某个知识库对应的 collection
#   add_documents()       批量写入文本、向量和元数据
#   query()               相似度检索
#   delete_by_doc_id()    删除某个文档产生的全部向量块
#   delete_collection()   删除整个知识库的向量集合
#   count()               统计集合中的向量记录数
#
# 类方法通过 cls 使用共享客户端，不需要先实例化 VectorStore()。
# =============================================================================

import chromadb
# 重命名为 ChromaSettings，避免和本项目的 settings 对象混淆。
from chromadb.config import Settings as ChromaSettings
from app.core.config import settings
from app.core.logger import logger
from typing import List, Dict, Any, Optional
import time


class VectorStore:
    # 类属性保存全局共享客户端。首次调用 get_client() 前为 None。
    _client: Optional[chromadb.PersistentClient] = None

    # 初始化 ChromaDB 客户端，确保全局唯一实例，持久化客户端封装
    @classmethod
    def get_client(cls) -> chromadb.PersistentClient:
        # 惰性单例：只有真正需要向量库时才初始化，并在后续调用中复用。
        if cls._client is None:
            cls._client = chromadb.PersistentClient(
                # PersistentClient 会把向量数据保存到磁盘，而不是进程退出后丢失。
                path=settings.CHROMA_PERSIST_DIR,
                # 关闭匿名遥测，避免发送使用统计。
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            logger.info(f"ChromaDB 已连接，持久化目录: {settings.CHROMA_PERSIST_DIR}")
        return cls._client

    @classmethod
    def get_collection(cls, kb_id: int) -> chromadb.Collection:
        """每个知识库对应一个 Collection"""
        client = cls.get_client()
        # 用稳定命名把关系数据库 knowledge_bases.id 与 Chroma 集合关联起来。
        name = f"kb_{kb_id}"
        # 已存在则直接取得，不存在则创建，因此调用方不必单独判断初始化状态。
        return client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"}, #HNSW索引，使用余弦相似度
        )

    # 添加文档到集合
    @classmethod
    def add_documents(
        cls,
        kb_id: int,
        ids: List[str],
        embeddings: List[List[float]],
        documents: List[str],
        metadatas: List[Dict[str, Any]],
    ):
        # 四个列表必须按下标一一对应：同一位置代表同一个文本块。
        # ids 用于唯一标识块；embeddings 用于相似度计算；documents 保存原文；
        # metadatas 保存 doc_id、页码等筛选和溯源信息。
        collection = cls.get_collection(kb_id)
        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    @classmethod
    def query(
        cls,
        kb_id: int,
        query_embedding: List[float],
        top_k: int = 5,
        where: Optional[Dict] = None,
    ) -> Dict:
        collection = cls.get_collection(kb_id) #获取集合
        # Chroma 支持一次查询多个向量，所以单个 query_embedding 也要再包一层列表。
        kwargs = {
            "query_embeddings": [query_embedding],
            "n_results": top_k,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where #添加查询条件 ，根据文件类型或标签筛选
        # perf_counter 适合统计耗时，不受系统时间校准影响。
        t_query = time.perf_counter()

        '''
        collection.query(**kwargs)是ChromaDB的向量相似度查询，也叫语义搜索
        1. 传入一个 查询向量 （用户问题的嵌入向量）
        2. ChromaDB 在索引中查找与该向量 最相似 的 top_k 个向量
        3. 返回对应的文本内容和相似度分数
        这是一种 基于语义的模糊匹配 ，不是精确的关键词匹配
        '''
        result = collection.query(**kwargs)
        # 转换为毫秒，方便在日志中观察检索性能。
        query_ms = (time.perf_counter() - t_query) * 1000
        logger.debug(f"ChromaDB向量检索: {query_ms:.1f}ms | kb_id={kb_id} | n_results={top_k}")
        return result

    # 删除文档
    @classmethod
    def delete_by_doc_id(cls, kb_id: int, doc_id: int):
        collection = cls.get_collection(kb_id)
        # 一个文档通常对应多个 chunk；where 会删除 metadata.doc_id 匹配的全部向量。
        collection.delete(where={"doc_id": doc_id})

    # 删除集合
    @classmethod
    def delete_collection(cls, kb_id: int):
        client = cls.get_client()
        try:
            client.delete_collection(f"kb_{kb_id}")
        except Exception:
            # 知识库删除时，即使集合不存在或 Chroma 清理失败，也不阻断数据库主流程。
            pass

    # 统计集合文档数量
    @classmethod
    def count(cls, kb_id: int) -> int:
        try:
            return cls.get_collection(kb_id).count()
        except Exception:
            # 统计失败时返回 0，调用者可将向量库视为空集合。
            return 0
