# =============================================================================
# 文件作用与架构位置（统一大语言模型调用层）
# =============================================================================
# 不同厂商的 SDK、认证方式、模型名和流式接口各不相同。本文件向聊天路由提供统一的
# LLMService.chat() / chat_stream()，再在内部适配 OpenAI、DeepSeek、DashScope、千帆、
# Ollama 和 LM Studio。
#
# 本文件共有 13 个函数/方法：
#
#   _build_messages()       组装 system、历史、参考资料和当前问题
#   _get_provider_cfg()     从 Redis/数据库读取 Provider 覆盖配置
#   LLMService.chat()       非流式统一入口和 Provider 分派
#   LLMService.chat_stream()流式统一入口和 Provider 分派
#   其余 9 个函数          各 Provider 的非流式/流式具体实现
#
# 调用流程：
#
#   Chat 路由传入 history + user_message + RAG context
#       |
#       v
#   _build_messages()
#       +--> 有 context：使用严格知识库 Prompt，并把资料放进当前 user 消息
#       +--> 无 context：使用普通 AI 助手 Prompt
#       |
#       v
#   _get_provider_cfg()：DB 配置优先，.env 作为回退
#       |
#       v
#   按 provider 调用云 API 或本地模型
#       |
#       +--> chat() 返回 (完整文本, 附加信息字典)
#       +--> chat_stream() 逐段 yield 文本
#
# JWT、权限和来源过滤不在这里处理；本文件只负责构造模型请求和取得模型输出。
# =============================================================================

# time 用于 Provider 调用耗时；AsyncGenerator 描述异步流式字符串生成器。
import time
from typing import List, Dict, Optional, AsyncGenerator
from app.core.config import settings
from app.core.logger import logger

# RAG System Prompt
# 当检索 context 非空时，约束模型只根据资料回答并标注来源。
RAG_SYSTEM_PROMPT = """你是一个严谨的知识库问答助手。你的回答必须严格遵守以下规则：

【核心约束】
1. 只能使用下面提供的参考资料回答用户问题。
2. 参考资料中没有的信息，必须回答"文档中未找到相关信息"。
3. 严禁添加、编造、推断任何数字、规则、定义、条款。

【回答规范】
4. 回答必须简洁，直接针对问题给出答案，不需要解释原因或补充背景。
5. 引用来源格式：[来源X]
6. 如果参考资料不完全支撑问题，先给出已知信息，再补充："（注：文档中未找到XX相关内容）"。
"""

# 没绑定知识库或未检索到 context 时，使用普通对话约束。
NO_CONTEXT_SYSTEM_PROMPT = """你是专业AI助手，请准确回答用户问题。对于不确定的信息，请明确说明。"""


def _build_messages(
    messages: List[Dict],
    user_message: str,
    context: str,
) -> List[Dict]:
    # context 的真假决定 system prompt 类型。
    system_prompt = RAG_SYSTEM_PROMPT if context else NO_CONTEXT_SYSTEM_PROMPT

    # 所有请求都以 system 消息开头，告诉模型其角色和规则。
    result = [{"role": "system", "content": system_prompt}]

    # 历史消息
    # 历史消息已经是 {role, content} 格式，按原顺序接在 system 后面。
    result.extend(messages)

    # 当前用户消息（含检索上下文）
    if context:
        # 把检索结果和问题放在同一个当前 user 消息中，明确区分参考资料与提问。
        content = f"参考资料：\n{context}\n\n用户问题：{user_message}"
    else:
        content = user_message

    result.append({"role": "user", "content": content})
    return result


async def _get_provider_cfg(provider: str, db=None) -> dict:
    """从 Redis 缓存或 DB 读取 Provider 配置"""
    if db is None:
        # 没有数据库会话时无法读 ModelConfig，调用方之后直接使用 settings 回退值。
        return {}

    from app.core.redis_client import cache_get_json, cache_set_json

    cache_key = f"provider_cfg:{provider}"
    # 每个 Provider 独立缓存，例如 provider_cfg:openai。
    cached = await cache_get_json(cache_key)
    if cached:
        logger.debug(f"LLM Provider配置缓存命中: {provider}")
        return cached

    t_db = time.perf_counter()
    try:
        from sqlalchemy import select
        from app.models.db import ModelConfig
        result = await db.execute(
            select(ModelConfig).where(
                ModelConfig.provider == provider,
                ModelConfig.is_enabled == True,
            )
        )
        cfg = result.scalar_one_or_none()
        db_ms = (time.perf_counter() - t_db) * 1000
        logger.debug(f"LLM Provider配置DB查询: {db_ms:.1f}ms | provider={provider} | found={cfg is not None}")
        if cfg is None:
            # DB 没有启用配置时不缓存空结果，后面每次会继续查询并回退 .env。
            return {}

        data = {
            "api_key": cfg.api_key,
            "api_secret": cfg.api_secret,
            "base_url": cfg.base_url,
            "model_name": cfg.model_name,
        }
        # Redis 只保存普通 JSON 字典，不保存 SQLAlchemy ORM 对象。
        await cache_set_json(cache_key, data, ttl=settings.PROVIDER_CACHE_TTL)
        return data
    except Exception:
        # 配置查询失败时降级为空字典，Provider 实现仍可尝试 settings 中的值。
        db_ms = (time.perf_counter() - t_db) * 1000
        logger.warning(f"LLM Provider配置DB查询失败: {db_ms:.1f}ms | provider={provider}")
        return {}


class LLMService:

    @staticmethod
    async def chat(
        provider: str,
        model: Optional[str],
        messages: List[Dict],
        user_message: str,
        context: str = "",
        db=None,
    ):
        # 先统一构造模型消息，再取得数据库覆盖配置。
        full_messages = _build_messages(messages, user_message, context)
        cfg = await _get_provider_cfg(provider, db)

        if provider in ("openai", "deepseek"):
            # DeepSeek 支持 OpenAI 兼容协议，因此复用同一实现。
            return await _openai_chat(provider, model, full_messages, stream=False, cfg=cfg)
        elif provider == "dashscope":
            return await _dashscope_chat(model, full_messages, cfg=cfg)
        elif provider == "qianfan":
            return await _qianfan_chat(model, full_messages, cfg=cfg)
        elif provider == "ollama":
            return await _ollama_chat(model, full_messages, stream=False, cfg=cfg)
        elif provider == "lmstudio":
            return await _lmstudio_chat(model, full_messages, stream=False, cfg=cfg)
        else:
            # Provider 白名单之外的值应尽早明确失败。
            raise ValueError(f"不支持的 LLM Provider: {provider}")

    @staticmethod
    async def chat_stream(
        provider: str,
        model: Optional[str],
        messages: List[Dict],
        user_message: str,
        context: str = "",
        db=None,
    ) -> AsyncGenerator[str, None]:
        full_messages = _build_messages(messages, user_message, context)
        cfg = await _get_provider_cfg(provider, db)

        if provider in ("openai", "deepseek"):
            # async for 消费厂商流，再把每个文本增量原样 yield 给上层 SSE 生成器。
            async for chunk in _openai_chat_stream(provider, model, full_messages, cfg=cfg):
                yield chunk
        elif provider == "dashscope":
            async for chunk in _dashscope_chat_stream(model, full_messages, cfg=cfg):
                yield chunk
        elif provider == "ollama":
            async for chunk in _ollama_chat_stream(model, full_messages, cfg=cfg):
                yield chunk
        elif provider == "lmstudio":
            async for chunk in _lmstudio_chat_stream(model, full_messages, cfg=cfg):
                yield chunk
        else:
            # 非流式降级：一次性返回
            # 千帆等没有专用流实现的 Provider 会等待完整结果，然后只 yield 一次。
            answer, _ = await LLMService.chat(provider, model, messages, user_message, context, db)
            yield answer


# ===== Provider 实现 =====

async def _openai_chat(provider: str, model: Optional[str], messages: List[Dict], stream: bool, cfg: dict = None):
    # stream 参数为统一签名保留；本函数当前固定 stream=False，流式使用下一个函数。
    from openai import AsyncOpenAI
    cfg = cfg or {}
    if provider == "deepseek":
        # 优先顺序：会话显式 model -> DB model_name -> .env 默认模型。
        api_key = cfg.get("api_key") or settings.DEEPSEEK_API_KEY
        base_url = cfg.get("base_url") or settings.DEEPSEEK_BASE_URL
        model = model or cfg.get("model_name") or settings.DEEPSEEK_MODEL
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        api_key = cfg.get("api_key") or settings.OPENAI_API_KEY
        base_url = cfg.get("base_url") or settings.OPENAI_BASE_URL
        model = model or cfg.get("model_name") or settings.OPENAI_MODEL
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    t_api = time.perf_counter()
    response = await client.chat.completions.create(model=model, messages=messages, stream=False)
    api_ms = (time.perf_counter() - t_api) * 1000
    logger.debug(f"LLM-OpenAI API调用: {api_ms:.1f}ms | provider={provider} | model={model} | msg_count={len(messages)}")
    # 当前只取第一个 choice；第二个返回值保留给未来 usage 等附加信息。
    content = response.choices[0].message.content
    return content, {}


async def _openai_chat_stream(provider: str, model: Optional[str], messages: List[Dict], cfg: dict = None) -> AsyncGenerator[str, None]:
    from openai import AsyncOpenAI
    cfg = cfg or {}
    if provider == "deepseek":
        api_key = cfg.get("api_key") or settings.DEEPSEEK_API_KEY
        base_url = cfg.get("base_url") or settings.DEEPSEEK_BASE_URL
        model = model or cfg.get("model_name") or settings.DEEPSEEK_MODEL
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        api_key = cfg.get("api_key") or settings.OPENAI_API_KEY
        base_url = cfg.get("base_url") or settings.OPENAI_BASE_URL
        model = model or cfg.get("model_name") or settings.OPENAI_MODEL
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    t_api = time.perf_counter()
    # 创建流对象的耗时近似表示获得首次响应前的等待时间。
    stream = await client.chat.completions.create(model=model, messages=messages, stream=True)
    api_ms = (time.perf_counter() - t_api) * 1000
    logger.debug(f"LLM-OpenAI-Stream 首次响应: {api_ms:.1f}ms | provider={provider} | model={model}")
    async for chunk in stream:
        # delta.content 可能为 None（例如角色或结束事件），只发送非空文本。
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


async def _dashscope_chat(model: Optional[str], messages: List[Dict], cfg: dict = None):
    import dashscope
    from dashscope import Generation
    cfg = cfg or {}
    dashscope.api_key = cfg.get("api_key") or settings.DASHSCOPE_API_KEY
    model = model or cfg.get("model_name") or settings.DASHSCOPE_MODEL
    t_api = time.perf_counter()
    # Generation.call 是同步 SDK 调用，虽然外层函数是 async，执行期间仍可能阻塞事件循环。
    response = Generation.call(model=model, messages=messages, result_format="message")
    api_ms = (time.perf_counter() - t_api) * 1000
    logger.debug(f"LLM-DashScope API调用: {api_ms:.1f}ms | model={model} | msg_count={len(messages)}")
    content = response.output.choices[0].message.content
    return content, {}


async def _dashscope_chat_stream(model: Optional[str], messages: List[Dict], cfg: dict = None) -> AsyncGenerator[str, None]:
    import dashscope
    from dashscope import Generation
    cfg = cfg or {}
    dashscope.api_key = cfg.get("api_key") or settings.DASHSCOPE_API_KEY
    model = model or cfg.get("model_name") or settings.DASHSCOPE_MODEL
    t_api = time.perf_counter()
    # incremental_output=True 让每次 response.content 是新增片段而不是完整累计文本。
    responses = Generation.call(model=model, messages=messages, result_format="message", stream=True, incremental_output=True)
    api_ms = (time.perf_counter() - t_api) * 1000
    logger.debug(f"LLM-DashScope-Stream 首次响应: {api_ms:.1f}ms | model={model}")
    # 这里使用同步 for 迭代厂商生成器，同样可能占用当前事件循环线程。
    for response in responses:
        delta = response.output.choices[0].message.content
        if delta:
            yield delta


async def _qianfan_chat(model: Optional[str], messages: List[Dict], cfg: dict = None):
    import qianfan
    cfg = cfg or {}
    chat_comp = qianfan.ChatCompletion(
        ak=cfg.get("api_key") or settings.QIANFAN_ACCESS_KEY,
        sk=cfg.get("api_secret") or settings.QIANFAN_SECRET_KEY,
    )
    model = model or cfg.get("model_name") or settings.QIANFAN_MODEL
    t_api = time.perf_counter()
    # ado 是千帆 SDK 的异步调用接口。
    resp = await chat_comp.ado(model=model, messages=messages)
    api_ms = (time.perf_counter() - t_api) * 1000
    logger.debug(f"LLM-Qianfan API调用: {api_ms:.1f}ms | model={model} | msg_count={len(messages)}")
    return resp.body["result"], {}


async def _ollama_chat(model: Optional[str], messages: List[Dict], stream: bool, cfg: dict = None):
    # stream 参数为统一签名保留；本函数当前执行完整响应。
    import ollama
    cfg = cfg or {}
    model = model or cfg.get("model_name") or settings.OLLAMA_MODEL
    base_url = cfg.get("base_url") or settings.OLLAMA_BASE_URL
    client = ollama.AsyncClient(host=base_url)
    t_api = time.perf_counter()
    response = await client.chat(model=model, messages=messages)
    api_ms = (time.perf_counter() - t_api) * 1000
    logger.debug(f"LLM-Ollama API调用: {api_ms:.1f}ms | model={model} | base_url={base_url} | msg_count={len(messages)}")
    return response.message.content, {}


async def _ollama_chat_stream(model: Optional[str], messages: List[Dict], cfg: dict = None) -> AsyncGenerator[str, None]:
    import ollama
    cfg = cfg or {}
    model = model or cfg.get("model_name") or settings.OLLAMA_MODEL
    base_url = cfg.get("base_url") or settings.OLLAMA_BASE_URL
    client = ollama.AsyncClient(host=base_url)
    t_api = time.perf_counter()
    async for chunk in await client.chat(model=model, messages=messages, stream=True):
        # 当前实现希望只记录一次首响应耗时，但首次满足条件后把 t_api 设为 None；
        # 下一轮仍会执行 time.perf_counter() - t_api，可能触发 TypeError。这里只说明现有行为。
        api_ms = (time.perf_counter() - t_api) * 1000
        if api_ms < 1000 and chunk.message.content:
            logger.debug(f"LLM-Ollama-Stream 首次响应: {api_ms:.1f}ms | model={model} | base_url={base_url}")
            t_api = None
        delta = chunk.message.content
        if delta:
            yield delta


async def _lmstudio_chat(model: Optional[str], messages: List[Dict], stream: bool, cfg: dict = None):
    # LM Studio 暴露 OpenAI 兼容接口，本地服务通常不校验 Key，但 SDK 要求提供非空字符串。
    from openai import AsyncOpenAI
    cfg = cfg or {}
    base_url = cfg.get("base_url") or settings.LMSTUDIO_BASE_URL
    model = model or cfg.get("model_name") or settings.LMSTUDIO_MODEL
    client = AsyncOpenAI(base_url=base_url, api_key="lm-studio")
    t_api = time.perf_counter()
    response = await client.chat.completions.create(model=model, messages=messages, stream=False)
    api_ms = (time.perf_counter() - t_api) * 1000
    logger.debug(f"LLM-LMStudio API调用: {api_ms:.1f}ms | model={model} | base_url={base_url} | msg_count={len(messages)}")
    return response.choices[0].message.content, {}


async def _lmstudio_chat_stream(model: Optional[str], messages: List[Dict], cfg: dict = None) -> AsyncGenerator[str, None]:
    from openai import AsyncOpenAI
    cfg = cfg or {}
    base_url = cfg.get("base_url") or settings.LMSTUDIO_BASE_URL
    model = model or cfg.get("model_name") or settings.LMSTUDIO_MODEL
    client = AsyncOpenAI(base_url=base_url, api_key="lm-studio")
    t_api = time.perf_counter()
    stream = await client.chat.completions.create(model=model, messages=messages, stream=True)
    api_ms = (time.perf_counter() - t_api) * 1000
    logger.debug(f"LLM-LMStudio-Stream 首次响应: {api_ms:.1f}ms | model={model} | base_url={base_url}")
    async for chunk in stream:
        # 与 OpenAI 流格式相同，读取 choices[0].delta.content。
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
