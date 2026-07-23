# =============================================================================
# 文件作用与架构位置（文本向量化服务）
# =============================================================================
# Embedding 把文本转换为固定长度数字向量，使语义相近的文本在向量空间中距离更近。
# 文档入库和用户检索必须使用兼容的 Embedding 模型，否则向量不可比较。
#
# 本文件共有 9 个函数/类方法：
#
#   _detect_device()      检测并缓存 CUDA/CPU
#   _detect_batch_size()  根据设备和显存选择批大小
#   _get_local_model()    延迟加载并缓存 SentenceTransformer
#   _patch_torch_version_check() 兼容旧 torch 与新版 transformers
#   embed_texts()         批量文本向量化统一入口
#   embed_query()         单个查询向量化
#   preload_model()       应用启动预热模型
#   _local_embed()        本地模型实现
#   _openai_embed()       OpenAI Embedding API 实现
#
#   文档 chunks --------+
#                       +--> embed_texts() -> List[List[float]] -> ChromaDB
#   用户 query ---------+--> embed_query() -> List[float]       -> 相似度查询
#
# 模型和设备放在模块级缓存中，避免每个请求重复加载数 GB 模型权重。
# =============================================================================

# gc 在每批向量生成后触发垃圾回收；os 检查 Hugging Face 本地缓存路径。
import gc
import os
import time
from typing import List
from app.core.config import settings
from app.core.logger import logger

# 下面三个模块变量分别缓存设备、模型对象和模型名称。
# 当配置的模型名称改变时，_get_local_model 会重新加载。
_EMBEDDING_DEVICE: str | None = None
_CACHED_MODEL = None
_CACHED_MODEL_NAME: str = ""


def _detect_device() -> str:
    global _EMBEDDING_DEVICE
    # 已检测过时直接返回，避免每批处理都查询 PyTorch/CUDA。
    if _EMBEDDING_DEVICE is not None:
        return _EMBEDDING_DEVICE
    try:
        import torch
        if torch.cuda.is_available():
            # total_memory 单位是字节，这里转换成 MB 只用于日志和批大小判断。
            vram_mb = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
            logger.info(f"Embedding: CUDA 可用, 显存={vram_mb}MB")
            _EMBEDDING_DEVICE = "cuda"
            return _EMBEDDING_DEVICE
    except Exception:
        # PyTorch 未安装或 CUDA 初始化失败时安全回退 CPU。
        pass
    _EMBEDDING_DEVICE = "cpu"
    return _EMBEDDING_DEVICE


def _detect_batch_size() -> int:
    # 批次越大吞吐越高，但占用显存也越多；这里按显存粗略分档。
    device = _detect_device()
    if device == "cuda":
        try:
            import torch
            vram_mb = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
            if vram_mb < 4000:
                return 8
            elif vram_mb < 8000:
                return 16
            else:
                return 32
        except Exception:
            return 8
    return 4


def _get_local_model():
    global _CACHED_MODEL, _CACHED_MODEL_NAME
    if _CACHED_MODEL is None or _CACHED_MODEL_NAME != settings.EMBEDDING_MODEL:
        # 加载模型前应用兼容补丁，避免旧 torch 被 transformers 的安全版本检查阻断。
        _patch_torch_version_check()

        from sentence_transformers import SentenceTransformer
        from huggingface_hub import try_to_load_from_cache

        model_name = settings.EMBEDDING_MODEL
        device = _detect_device()
        logger.info(f"Embedding 模型首次加载: {model_name} device={device}")

        # 先检查 Hugging Face 本地缓存：命中时强制离线加载，启动更快且不依赖网络。
        config_path = try_to_load_from_cache(model_name, "config.json")
        if config_path and os.path.isfile(config_path):
            local_path = os.path.dirname(config_path)
            _CACHED_MODEL = SentenceTransformer(local_path, device=device, local_files_only=True)
        else:
            # 本地没有模型时按模型名加载，SentenceTransformer 可能从 Hub 下载。
            _CACHED_MODEL = SentenceTransformer(model_name, device=device)

        _CACHED_MODEL_NAME = model_name
        logger.info(f"Embedding 模型加载完成, device={device}")
    return _CACHED_MODEL


def _patch_torch_version_check():
    """绕过 transformers 4.57+ 对 torch>=2.6 的硬校验"""
    try:
        import torch
        from packaging import version as _version
        if _version.parse(torch.__version__) >= _version.parse("2.6"):
            # 已满足要求时不需要修改 transformers 行为。
            return
        import transformers.modeling_utils
        transformers.modeling_utils.check_torch_load_is_safe = lambda: None
        logger.debug("Embedding: torch 版本校验已绕过")
    except Exception:
        # 补丁本身失败时保持原库行为，真正加载模型时会给出具体错误。
        pass


class EmbeddingService:
    @classmethod
    def embed_texts(cls, texts: List[str]) -> List[List[float]]:
        # 空输入直接返回空列表，避免模型/API 收到无意义请求。
        if not texts:
            return []
        if settings.EMBEDDING_PROVIDER == "openai":
            # Provider 配置决定使用远程 API 还是本地 SentenceTransformer。
            return cls._openai_embed(texts)
        return cls._local_embed(texts)

    @classmethod
    def embed_query(cls, query: str) -> List[float]:
        # 复用批量入口保证查询和文档使用完全相同的模型及归一化规则。
        return cls.embed_texts([query])[0]

    @classmethod
    def preload_model(cls):
        """供 main.py 启动预热调用（与 _get_local_model 共用同一缓存）"""
        _get_local_model()
        logger.info(f"Embedding 模型 {settings.EMBEDDING_MODEL} 预加载完成")

    @classmethod
    def _local_embed(cls, texts: List[str]) -> List[List[float]]:
        t_load = time.perf_counter()
        model = _get_local_model()
        load_ms = (time.perf_counter() - t_load) * 1000
        if load_ms > 100:
            logger.info(f"Embedding 模型加载(冷启动): {load_ms:.1f}ms | model={settings.EMBEDDING_MODEL}")
        else:
            logger.debug(f"Embedding 模型获取(复用): {load_ms:.1f}ms | model={settings.EMBEDDING_MODEL}")

        results = []
        BATCH = _detect_batch_size()
        t_encode_total = time.perf_counter()
        for i in range(0, len(texts), BATCH):
            # Python 切片得到当前批次；最后一批可以小于 BATCH。
            batch = texts[i:i + BATCH]
            t_batch = time.perf_counter()

            vecs = model.encode(
                batch,
                # 单位归一化后可直接用余弦距离比较，并保持入库/查询计算一致。
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            batch_ms = (time.perf_counter() - t_batch) * 1000
            logger.debug(f"Embedding 批次 {i//BATCH+1} encode: {batch_ms:.1f}ms | batch_size={len(batch)}")
            results.extend(vecs.tolist())
            # 转成普通 list 后删除 NumPy 数组引用，并主动回收临时对象，降低长批处理峰值内存。
            del vecs
            gc.collect()

        encode_total_ms = (time.perf_counter() - t_encode_total) * 1000
        logger.debug(f"Embedding 总encode耗时: {encode_total_ms:.1f}ms | 总文本数={len(texts)} | batch={BATCH}")
        return results

    @classmethod
    def _openai_embed(cls, texts: List[str]) -> List[List[float]]:
        # 使用同步 OpenAI 客户端；调用方需要注意不要在高并发事件循环中长时间阻塞。
        from openai import OpenAI
        t_api = time.perf_counter()
        client = OpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)
        response = client.embeddings.create(
            model=settings.OPENAI_EMBEDDING_MODEL,
            input=texts,
        )
        api_ms = (time.perf_counter() - t_api) * 1000
        logger.debug(f"Embedding-OpenAI API调用: {api_ms:.1f}ms | texts={len(texts)}")
        # OpenAI 返回 data 项列表，顺序与输入 texts 一致。
        return [item.embedding for item in response.data]
