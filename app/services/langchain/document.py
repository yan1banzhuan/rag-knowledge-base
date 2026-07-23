# =============================================================================
# 文件作用与架构位置（LangChain 文档处理适配层）
# =============================================================================
# 本文件提供一套基于 LangChain Loader/Document 的替代文档处理接口。当前主文档流程使用
# app.parsers + DocumentService；仓库中未发现其他模块调用本类，因此它更像兼容或实验路径。
#
# 类中有 6 个静态方法：加载文件、分块、准备存储字典、创建 LangChain Document、
# 提取正文、提取元数据。
#
#   文件 -> LangChain Loader -> List[Document] -> TextSplitter -> chunks
#                                                        |
#                                                        v
#                                             补充 doc/kb/文件元数据
# =============================================================================

from typing import List, Dict, Any, Optional
from langchain.document_loaders import PyPDFLoader, TextLoader, UnstructuredWordDocumentLoader, UnstructuredExcelLoader, CSVLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from app.core.logger import logger
from app.services.langchain.embeddings import LangChainEmbeddingService
from langchain.schema import Document as LangChainDocument


class LangChainDocumentService:
    """LangChain文档处理服务"""

    @staticmethod
    def load_document(file_path: str, file_type: str) -> List[LangChainDocument]:
        """加载文档并返回LangChain Document对象"""
        # 根据扩展名选择 LangChain 对应 Loader；loader.load() 才真正读取文件。
        loader = None

        if file_type == "pdf":
            loader = PyPDFLoader(file_path)
        elif file_type == "txt":
            loader = TextLoader(file_path, encoding="utf-8")
        elif file_type == "docx":
            loader = UnstructuredWordDocumentLoader(file_path)
        elif file_type in ["xlsx", "xls"]:
            loader = UnstructuredExcelLoader(file_path)
        elif file_type == "csv":
            loader = CSVLoader(file_path)
        else:
            raise ValueError(f"不支持的文件类型: {file_type}")

        # 不同 Loader 都统一返回 LangChainDocument 列表。
        documents = loader.load()
        logger.info(f"加载文档: {file_path}, 分块数: {len(documents)}")
        return documents

    @staticmethod
    def split_documents(
        documents: List[LangChainDocument],
        chunk_size: int = 1000,
        chunk_overlap: int = 200
    ) -> List[LangChainDocument]:
        """分割文档为文本块"""
        # 递归分隔器按段落、换行、中文标点、空格逐级寻找合适切点。
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]
        )

        split_docs = text_splitter.split_documents(documents)
        logger.info(f"文档分割完成，生成 {len(split_docs)} 个文本块")
        return split_docs

    @staticmethod
    def prepare_documents_for_storage(
        documents: List[LangChainDocument],
        doc_id: int,
        filename: str,
        file_type: str,
        kb_id: int,
        tags: Optional[List[str]] = None
    ) -> List[Dict]:
        """准备文档元数据用于存储"""
        prepared_docs = []

        for i, doc in enumerate(documents):
            # 原 metadata 可能是 None，统一转为空字典。
            metadata = doc.metadata or {}
            prepared_docs.append({
                "content": doc.page_content,
                "metadata": {
                    "doc_id": doc_id,
                    "filename": filename,
                    "file_type": file_type,
                    "chunk_index": i,
                    # Loader 常把页码存为 page；这里转换为项目使用的 page_num 名称。
                    "page_num": metadata.get("page", None),
                    "kb_id": kb_id,
                    "tags": tags or []
                }
            })

        return prepared_docs

    @staticmethod
    def create_langchain_documents(
        texts: List[str],
        metadatas: List[Dict[str, Any]]
    ) -> List[LangChainDocument]:
        """创建LangChain Document对象"""
        # zip 按位置配对文本和元数据；长度不一致时以较短列表为准。
        return [
            LangChainDocument(page_content=text, metadata=metadata)
            for text, metadata in zip(texts, metadatas)
        ]

    @staticmethod
    def extract_text_from_document(doc: LangChainDocument) -> str:
        """从文档中提取文本内容"""
        return doc.page_content

    @staticmethod
    def get_document_metadata(doc: LangChainDocument) -> Dict[str, Any]:
        """获取文档元数据"""
        return doc.metadata or {}
