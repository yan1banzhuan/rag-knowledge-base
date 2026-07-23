# =============================================================================
# 文件作用与架构位置（大模型 Provider 配置路由）
# =============================================================================
# 本文件向管理界面提供 LLM Provider 的配置列表、连通性测试、保存和重置接口。数据库
# 配置优先于 .env；删除数据库配置后，服务层会重新回退到环境变量默认值。
#
# 本文件共有 13 个函数：
#
#   _mask()                   对 API Key/Secret 脱敏
#   _is_configured()          判断必要凭证是否填写
#   _build_out()              构造安全的响应模型
#   list_models()             列出全部受支持 Provider
#   test_model_connection()   连通性测试路由
#   _test_connection()        按 Provider 分派测试函数
#   _test_openai_like()       测试 OpenAI/DeepSeek 兼容接口
#   _test_dashscope()         测试阿里云 DashScope
#   _test_qianfan()           测试百度千帆
#   _test_ollama()            测试本地 Ollama
#   _test_lmstudio()          测试本地 LM Studio
#   upsert_model_config()     新增或更新数据库配置
#   delete_model_config()     删除数据库配置并恢复 .env 回退
#
#   前端模型配置页
#          |
#          +--> GET  /models            安全展示配置
#          +--> POST /models/{p}/test   实际网络测试
#          +--> PUT  /models/{p}        保存并清缓存
#          +--> DELETE /models/{p}      删除并清缓存
#
# API 凭证只会脱敏返回，完整值仍只保存在数据库或服务器环境变量中。
# =============================================================================

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.db import User, ModelConfig
from app.models.schemas import ModelConfigOut, ModelConfigUpdate, Resp
from app.api.deps import get_current_user, require_permission
from app.core.config import settings
from app.core.logger import logger

router = APIRouter(prefix="/models", tags=["模型管理"])

# 这是前端和后端共同认可的 Provider 白名单，防止任意路径值进入配置流程。
PROVIDERS = ["openai", "deepseek", "dashscope", "qianfan", "ollama", "lmstudio"]


def _mask(val: str) -> str:
    # 短值完全隐藏；长值只显示前后 4 位，便于管理员辨认但不泄露完整密钥。
    if not val or len(val) <= 8:
        return "****"
    return val[:4] + "****" + val[-4:]


def _is_configured(provider: str, cfg: ModelConfig | None) -> bool:
    """判断 provider 是否已填写必要凭证（不测试连通性）"""
    if provider in ("ollama", "lmstudio"):
        # 本地服务通常不使用云 API Key；这里只判断“配置条件”，不代表服务当前在线。
        return True  # 本地模型无需 key，始终算"已配置"
    if provider in ("openai", "deepseek", "dashscope"):
        # 优先数据库 cfg；数据库没有值时回退 settings 中的 .env 配置。
        key = (cfg and cfg.api_key) or (
            settings.OPENAI_API_KEY if provider == "openai" else
            settings.DEEPSEEK_API_KEY if provider == "deepseek" else
            settings.DASHSCOPE_API_KEY
        )
        return bool(key)
    if provider == "qianfan":
        ak = (cfg and cfg.api_key) or settings.QIANFAN_ACCESS_KEY
        sk = (cfg and cfg.api_secret) or settings.QIANFAN_SECRET_KEY
        return bool(ak and sk)
    return False


def _build_out(provider: str, cfg: ModelConfig | None) -> ModelConfigOut:
    """构建脱敏后的 ModelConfigOut，is_available 默认 False（需主动测试）"""
    if cfg is None:
        # 没有数据库行时仍返回该 Provider，方便前端展示完整配置卡片。
        return ModelConfigOut(
            provider=provider,
            is_enabled=True,
            is_configured=_is_configured(provider, None),
            is_available=False,
        )
    # api_key/api_secret 必须经过 _mask，不能把数据库原值直接序列化到浏览器。
    return ModelConfigOut(
        id=cfg.id,
        provider=cfg.provider,
        api_key=_mask(cfg.api_key) if cfg.api_key else None,
        api_secret=_mask(cfg.api_secret) if cfg.api_secret else None,
        base_url=cfg.base_url,
        model_name=cfg.model_name,
        is_enabled=cfg.is_enabled,
        is_configured=_is_configured(provider, cfg),
        is_available=False,
    )


@router.get("", response_model=Resp)
async def list_models(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("model_config")),
):
    """列出所有 Provider 配置（is_available 默认 False，需调用 test 接口验证）"""
    result = await db.execute(select(ModelConfig))
    # 列表转换为 {provider: ModelConfig}，之后按固定 PROVIDERS 顺序输出。
    db_configs = {c.provider: c for c in result.scalars().all()}
    data = [_build_out(p, db_configs.get(p)) for p in PROVIDERS]
    return Resp(data=data)


@router.post("/{provider}/test", response_model=Resp)
async def test_model_connection(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("model_config")),
):
    """测试指定 Provider 的连通性，成功返回 is_available=True"""
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支持的 Provider: {provider}")

    result = await db.execute(select(ModelConfig).where(ModelConfig.provider == provider))
    cfg = result.scalar_one_or_none()

    # 测试函数返回 (是否成功, 给用户看的消息)。测试不会修改配置。
    success, msg = await _test_connection(provider, cfg)
    return Resp(
        data={"provider": provider, "is_available": success, "message": msg},
        message=msg,
    )


async def _test_connection(provider: str, cfg: ModelConfig | None) -> tuple[bool, str]:
    """实际发起轻量请求测试连通性"""
    try:
        # 分派器把统一入口转到各厂商不同的认证和健康检查协议。
        if provider in ("openai", "deepseek"):
            return await _test_openai_like(provider, cfg)
        elif provider == "dashscope":
            return await _test_dashscope(cfg)
        elif provider == "qianfan":
            return await _test_qianfan(cfg)
        elif provider == "ollama":
            return await _test_ollama(cfg)
        elif provider == "lmstudio":
            return await _test_lmstudio(cfg)
        return False, "未知 Provider"
    except Exception as e:
        # DNS、超时、JSON 解析等异常统一转成 False，避免测试接口直接返回 500。
        logger.warning(f"[models] {provider} 连接测试异常: {e}")
        return False, str(e)


async def _test_openai_like(provider: str, cfg: ModelConfig | None) -> tuple[bool, str]:
    # OpenAI 官方 SDK 也能连接遵循 OpenAI 协议的 DeepSeek 服务。
    from openai import AsyncOpenAI
    if provider == "deepseek":
        api_key = (cfg and cfg.api_key) or settings.DEEPSEEK_API_KEY
        base_url = (cfg and cfg.base_url) or settings.DEEPSEEK_BASE_URL
    else:
        api_key = (cfg and cfg.api_key) or settings.OPENAI_API_KEY
        base_url = (cfg and cfg.base_url) or settings.OPENAI_BASE_URL

    if not api_key:
        return False, "未配置 API Key"

    # 用模型列表接口做轻量测试，10 秒超时避免配置页长时间卡住。
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=10)
    models = await client.models.list()
    count = len(list(models))
    return True, f"连接成功，共 {count} 个可用模型"


async def _test_dashscope(cfg: ModelConfig | None) -> tuple[bool, str]:
    # httpx.AsyncClient 执行异步 HTTP 请求，不阻塞其他 FastAPI 请求。
    import httpx
    api_key = (cfg and cfg.api_key) or settings.DASHSCOPE_API_KEY
    if not api_key:
        return False, "未配置 API Key"
    # 使用 DashScope 的模型列表接口做轻量验证
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    # 401 → key 无效；405/400 → 接口存在但请求方式不对（说明 key 有效能到达服务器）
    if resp.status_code == 401:
        return False, "API Key 无效（401 Unauthorized）"
    return True, f"连接成功（HTTP {resp.status_code}）"


async def _test_qianfan(cfg: ModelConfig | None) -> tuple[bool, str]:
    import httpx
    ak = (cfg and cfg.api_key) or settings.QIANFAN_ACCESS_KEY
    sk = (cfg and cfg.api_secret) or settings.QIANFAN_SECRET_KEY
    if not ak or not sk:
        return False, "未配置 Access Key 或 Secret Key"
    # 获取千帆 access_token
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://aip.baidubce.com/oauth/2.0/token",
            params={"grant_type": "client_credentials", "client_id": ak, "client_secret": sk},
        )
    # 千帆使用 AK/SK 先换取 access_token；能取得 token 即说明凭证可用。
    data = resp.json()
    if "access_token" in data:
        return True, "连接成功，Token 获取正常"
    return False, data.get("error_description", "认证失败")


async def _test_ollama(cfg: ModelConfig | None) -> tuple[bool, str]:
    import httpx
    base_url = (cfg and cfg.base_url) or settings.OLLAMA_BASE_URL
    async with httpx.AsyncClient(timeout=10) as client:
        # rstrip('/') 避免 base_url 末尾斜杠与 /api/tags 拼成双斜杠。
        resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
    if resp.status_code == 200:
        models = resp.json().get("models", [])
        names = [m["name"] for m in models[:5]]
        return True, f"连接成功，本地模型: {', '.join(names) or '（无）'}"
    return False, f"连接失败（HTTP {resp.status_code}）"


async def _test_lmstudio(cfg: ModelConfig | None) -> tuple[bool, str]:
    import httpx
    base_url = (cfg and cfg.base_url) or settings.LMSTUDIO_BASE_URL
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/models")
    if resp.status_code == 200:
        models = resp.json().get("data", [])
        names = [m.get("id", "unknown") for m in models[:5]]
        return True, f"连接成功，可用模型: {', '.join(names) or '（无）'}"
    return False, f"连接失败（HTTP {resp.status_code}）"


@router.put("/{provider}", response_model=Resp)
async def upsert_model_config(
    provider: str,
    body: ModelConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("model_config")),
):
    """新增或更新指定 Provider 的配置"""
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支持的 Provider: {provider}")

    result = await db.execute(select(ModelConfig).where(ModelConfig.provider == provider))
    cfg = result.scalar_one_or_none()

    if cfg is None:
        # upsert = update + insert：没有记录就创建，有记录就在原对象上修改。
        cfg = ModelConfig(provider=provider)
        db.add(cfg)

    if body.api_key is not None:
        # None 表示“本次不修改”；空白字符串表示“清除数据库值并允许回退环境变量”。
        cfg.api_key = body.api_key if body.api_key.strip() else None
    if body.api_secret is not None:
        cfg.api_secret = body.api_secret if body.api_secret.strip() else None
    if body.base_url is not None:
        cfg.base_url = body.base_url if body.base_url.strip() else None
    if body.model_name is not None:
        cfg.model_name = body.model_name if body.model_name.strip() else None
    if body.is_enabled is not None:
        cfg.is_enabled = body.is_enabled

    await db.flush()
    await db.refresh(cfg)

    # LLMService 会缓存最终 Provider 配置，保存后必须删除缓存让新值立即生效。
    from app.core.redis_client import cache_delete
    await cache_delete(f"provider_cfg:{provider}")

    return Resp(data=_build_out(provider, cfg), message="配置已保存")


@router.delete("/{provider}", response_model=Resp)
async def delete_model_config(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("model_config")),
):
    """删除 DB 中的 Provider 配置（恢复为 .env 配置）"""
    result = await db.execute(select(ModelConfig).where(ModelConfig.provider == provider))
    cfg = result.scalar_one_or_none()
    if cfg:
        # 只删除数据库覆盖项，不删除 .env；最终服务配置会自然回退到 settings。
        await db.delete(cfg)

    # 无论数据库行是否存在都清缓存，避免继续使用旧的合并配置。
    from app.core.redis_client import cache_delete
    await cache_delete(f"provider_cfg:{provider}")

    return Resp(message="已重置为环境变量配置")
