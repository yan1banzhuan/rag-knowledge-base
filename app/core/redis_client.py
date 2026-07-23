# =============================================================================
# 文件作用与架构位置（Redis 缓存适配层）
# =============================================================================
# 本文件把 redis.asyncio 的底层操作包装成项目统一的异步缓存函数。上层业务只关心
# cache_get/cache_set 等简单接口，不需要反复处理连接池、JSON 和 Redis 不可用异常。
#
# 函数关系：
#
#   get_redis() --------------------------> 创建/复用连接池
#       ^
#       |
#       +-- cache_get() <--- cache_get_json()  字符串 -> json.loads -> dict/list
#       +-- cache_set() <--- cache_set_json()  dict/list -> json.dumps -> 字符串
#       +-- cache_delete()
#       +-- cache_delete_pattern()
#
#   close_redis() ------------------------> 应用关闭时断开连接池
#
# 降级策略：
#
#   Redis 正常      -> 读写缓存
#   Redis 被禁用    -> 返回 None/False，业务继续查询数据库或重新计算
#   Redis 连接失败  -> 记录警告并跳过缓存，不让非核心缓存拖垮主业务
# =============================================================================

# json 用于在 Redis 字符串与 Python 字典/列表之间转换。
import json
# redis.asyncio 提供不会阻塞 FastAPI 事件循环的异步 Redis 客户端。
import redis.asyncio as aioredis
from app.core.config import settings
from app.core.logger import logger

# 模块级变量保存共享连接池。None 表示尚未建立或已经关闭。
_redis_pool = None


async def get_redis() -> aioredis.Redis | None:
    # 取得 Redis 客户端；首次调用时延迟创建并测试连接，失败时返回 None。
    # global 允许函数给模块级 _redis_pool 重新赋值。
    global _redis_pool
    # 配置明确关闭 Redis 时不尝试网络连接，上层会自动走无缓存路径。
    if not settings.REDIS_ENABLED:
        return None
    # 只有第一次使用时才创建连接池，这种方式称为“惰性初始化”。
    if _redis_pool is None:
        try:
            # 连接池复用 TCP 连接，避免每次缓存操作都重新建立网络连接。
            _redis_pool = aioredis.ConnectionPool.from_url(
                settings.REDIS_URL,
                max_connections=10,
                # Redis 原始响应是 bytes；开启后自动解码为 Python str。
                decode_responses=True,
            )
            r = aioredis.Redis(connection_pool=_redis_pool)
            # ping 主动验证连接参数和 Redis 服务是否可用。
            await r.ping()
            logger.info(f"Redis 已连接: {settings.REDIS_URL}")
        except Exception as e:
            # 缓存是性能优化而非核心数据源，因此连接失败只降级，不向上抛出异常。
            logger.warning(f"Redis 连接失败，缓存功能将跳过: {e}")
            return None
    # 每次返回一个轻量客户端对象，但它们共享同一个底层连接池。
    return aioredis.Redis(connection_pool=_redis_pool)


async def close_redis():
    # 断开共享 Redis 连接池，通常在 FastAPI 应用关闭阶段调用。
    global _redis_pool
    if _redis_pool:
        await _redis_pool.disconnect()
        # 置回 None，使未来再次调用 get_redis() 时可以重新初始化。
        _redis_pool = None
        logger.info("Redis 连接已关闭")


async def cache_get(key: str) -> str | None:
    # 读取字符串缓存；未命中、Redis 不可用或读取失败时统一返回 None。
    r = await get_redis()
    if r is None:
        return None
    try:
        return await r.get(key)
    except Exception:
        # 不让缓存读取异常影响主业务。调用者会把 None 当作缓存未命中。
        return None


async def cache_set(key: str, value: str, ttl: int = 300) -> bool:
    # 写入带过期时间的字符串缓存，成功返回 True，失败返回 False。
    r = await get_redis()
    if r is None:
        return False
    try:
        # ex 的单位是秒。TTL 到期后 Redis 自动删除该键，避免长期返回过时数据。
        await r.set(key, value, ex=ttl)
        return True
    except Exception:
        return False


async def cache_delete(key: str):
    # 删除一个缓存键；键不存在或 Redis 异常时静默结束。
    r = await get_redis()
    if r is None:
        return
    try:
        await r.delete(key)
    except Exception:
        pass


async def cache_delete_pattern(pattern: str):
    # 按通配模式扫描并批量删除缓存，例如 user_perms:*。
    r = await get_redis()
    if r is None:
        return
    try:
        keys = []
        # scan_iter 分批扫描，不像 KEYS 命令那样一次阻塞 Redis 处理全部键。
        async for key in r.scan_iter(match=pattern):
            keys.append(key)
        if keys:
            # *keys 把列表展开为多个位置参数，一次命令批量删除。
            await r.delete(*keys)
    except Exception:
        pass


async def cache_get_json(key: str) -> dict | list | None:
    # 读取 JSON 缓存并还原为 dict/list；不存在或 JSON 损坏时返回 None。
    raw = await cache_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 缓存内容不可解析时把它视作未命中，上层可以重新生成正确内容。
        return None


async def cache_set_json(key: str, data: dict | list, ttl: int = 300) -> bool:
    # 将字典或列表序列化为 JSON 后写入 Redis。
    # ensure_ascii=False 让中文直接保存为可读字符，而不是 \uXXXX 转义序列。
    return await cache_set(key, json.dumps(data, ensure_ascii=False), ttl)
