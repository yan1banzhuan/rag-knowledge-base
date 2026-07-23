from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db.session import get_db
from app.core.security import decode_access_token
from app.models.db import User, Role, Permission, PermissionCode
from fastapi.security.http import HTTPBearer, HTTPBase
from fastapi.security.utils import get_authorization_scheme_param


# =============================================================================
# 文件作用与架构位置（零基础导读）
# =============================================================================
# deps 是 dependencies（依赖）的缩写。本文件是项目的“认证与授权依赖层”，把多个
# API 都需要重复执行的步骤封装成 FastAPI Depends 依赖：读取 Token、恢复当前用户、
# 检查角色/权限、判断知识库访问权，以及维护权限缓存。
#
# “认证 Authentication”和“授权 Authorization”含义不同：
#
#   认证：你是谁？        Token -> user
#   授权：你能做什么？    user -> roles -> permissions -> allow/deny
#
# 架构位置：
#
#   Protected API route
#           |
#           | Depends(get_current_user)
#           | or Depends(require_permission("chat"))
#           v
#   app/api/deps.py                         <- 当前文件
#           |
#           +--> security.py  decode JWT
#           +--> session.py   obtain AsyncSession
#           +--> db.py        query User/Role/Permission
#           +--> Redis        read/write permission cache
#
# 本文件包含以下类、方法和函数：
#
#   HTTPBearer.__init__()
#       配置自定义 Bearer 认证依赖及是否自动抛错。
#
#   HTTPBearer.__call__()
#       从 HTTP Authorization 请求头提取认证方案与 Token。
#
#   get_current_user()
#       FastAPI 常用依赖：取得凭证并调用 _verify_user，必须登录才能继续。
#
#   _verify_user()
#       解码 JWT、读取 sub 用户 ID、查询用户并确认账号仍然有效。
#
#   require_permission()
#       权限依赖工厂。接收所需权限名称，返回真正执行检查的内部函数 dep()。
#
#   dep()
#       require_permission 内部创建的异步依赖，执行管理员放行及 ALL/OR 权限判断。
#
#   require_kb_access()
#       检查用户是否拥有某个指定知识库的 kb:<id> 权限。
#
#   _get_user_permissions()
#       聚合用户所有角色权限；优先读取 Redis，未命中时查数据库并写缓存。
#
#   get_user_kb_ids()
#       从权限编码 kb:<id> 中解析用户可访问的知识库 ID。
#
#   invalidate_user_permission_cache()
#       删除单个用户的权限缓存。
#
#   invalidate_all_permission_cache()
#       删除所有用户的权限缓存，常在角色权限发生变化后调用。
#
#   get_current_user_optional()
#       可选认证依赖：Header 或 query 参数有 Token 就验证，没有则返回 None。
#
# 一次典型受保护请求的流程：
#
#   HTTP request
#       |
#       | Authorization: Bearer <JWT>
#       v
#   HTTPBearer.__call__
#       |
#       v
#   get_current_user
#       |
#       v
#   _verify_user -> decode_access_token -> query active User
#       |
#       v
#   require_permission.dep (if route requires permission)
#       |
#       +--> admin? ------------------------------> allow
#       |
#       +--> _get_user_permissions
#                 |
#                 +--> Redis hit ----------------> permissions
#                 +--> Redis miss -> database --> cache -> permissions
#       |
#       +--> required permission satisfied? -----> allow / HTTP 403
#
# 身份失败通常返回 HTTP 401；身份有效但权限不足通常返回 HTTP 403。
# =============================================================================


# 这里重新定义了项目自己的 HTTPBearer 类。虽然上面从 FastAPI 导入了同名类，
# 但从此处开始，模块内的 HTTPBearer 名称指向下面这个自定义实现。
# 自定义的原因是可以精确控制缺少 Token 时返回的中文错误信息。
class HTTPBearer(HTTPBase):
    # __init__ 在创建 bearer = HTTPBearer() 时执行一次，而不是每个请求都执行一次。
    def __init__(self, auto_error: bool = True):
        # scheme="bearer" 用于 OpenAPI 安全方案描述；auto_error 控制缺少凭证时是否抛错。
        super().__init__(scheme="bearer", auto_error=auto_error)

    # 实例可以像函数一样被 FastAPI 调用，是因为类实现了 __call__。
    # 返回值中的 credentials 是去掉认证方案后的实际 Token 字符串。
    async def __call__(self, request: Request) -> HTTPAuthorizationCredentials | None:
        # 请求头标准形式为：Authorization: Bearer eyJhbGciOi...
        authorization = request.headers.get("Authorization")
        # 工具函数把请求头拆成 scheme="Bearer" 和 credentials="eyJ..."。
        scheme, credentials = get_authorization_scheme_param(authorization)
        # 三者任一为空都说明请求没有提供完整认证信息。
        if not (authorization and scheme and credentials):
            if self.auto_error:
                # auto_error=True 时立即阻止请求进入后续依赖和路由。
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未提供认证 Token")
            # 可选认证场景可使用 auto_error=False，并自行处理 None。
            return None
        # HTTPAuthorizationCredentials 只是结构化保存 scheme 和 credentials，尚未验证 JWT。
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)


# 创建一个可复用依赖实例。Depends(bearer) 会在每个请求中调用 bearer.__call__。
bearer = HTTPBearer()


# “必须登录”的标准依赖。很多路由通过 current_user: User = Depends(get_current_user)
# 声明：只有成功恢复当前用户后，路由函数才会执行。
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    # 默认 bearer 已会在无凭证时抛错，这个判断属于额外防御，确保类型和行为清晰。
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未提供认证 Token")
    # credentials.credentials 才是 JWT 本体；验证细节集中交给 _verify_user。
    return await _verify_user(credentials.credentials, db)


# Token 验证与用户恢复的共享实现，供 Header 认证和 query token 认证共同调用。
#
#   JWT -> payload -> payload["sub"] -> users.id -> active User
#
# 只相信 Token 签名还不够：用户可能在签发 Token 后被删除或禁用，因此仍需查询数据库。
async def _verify_user(token: str, db: AsyncSession) -> User:
    # decode_access_token 会验证签名和过期时间，任何 JWTError 都转换成 None。
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期")
    # sub 是 JWT 的 subject 字段，登录时 auth.py 把 user.id 写入这里。
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")
    # 同时预加载角色和角色权限，供后续 current_user.is_admin 和权限判断直接使用。
    result = await db.execute(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.id == int(user_id))
    )
    user = result.unique().scalar_one_or_none()
    # 即使 Token 尚未过期，账号已删除或被禁用也必须拒绝访问。
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
    return user


# 权限依赖“工厂”：调用它时不会立刻检查某个用户，而是根据参数创建并返回 dep 函数。
#
#   require_permission("chat")
#                |
#                v
#        returns configured dep function
#                |
#                v
#   FastAPI calls dep for every matching request
#
# *perm_codes 可接收任意数量的位置参数，并在函数内部形成元组。
def require_permission(*perm_codes: str, require_all: bool = False):
    """
    权限依赖工厂函数。
    用法:
      @router.get("/...", dependencies=[Depends(require_permission("kb_manage"))])
      @router.get("/...", dependencies=[Depends(require_permission("kb_manage", "chat", require_all=False))])
                                   ^ 默认 OR：有任一权限即放行
    """
    # dep 是闭包：即使 require_permission 已经执行结束，它仍然记得外层的 perm_codes 和
    # require_all。这使同一个通用函数可以生成许多不同权限要求的依赖。
    async def dep(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        # 超级管理员拥有所有权限
        # User.is_admin 会检查用户角色中是否存在 is_admin=True 的角色。
        if current_user.is_admin:
            return current_user

        # 加载用户角色和权限
        # set 可以去重，并支持交集、差集和 isdisjoint 等集合运算。
        perm_codes_set = set(perm_codes)
        user_perms = await _get_user_permissions(db, current_user.id)
        user_perm_codes = {p.code for p in user_perms}

        # kb_manage 隐含所有知识库子权限：有 kb_manage 菜单权限，或者有任何 kb:{id} 权限
        # 当前兼容逻辑把知识库管理权限展开为一组旧的细粒度权限名称，方便旧路由继续工作。
        kb_codes = {p.code for p in user_perms if p.type == "kb" and p.code.startswith("kb:")}
        if "kb_manage" in user_perm_codes or kb_codes:
            user_perm_codes.update(["kb_manage", "kb_read", "kb_write", "kb_delete", "doc_upload", "doc_delete"])

        # 超级管理员角色也拥有所有权限
        # 这与 current_user.is_admin 语义相同，属于再次防御性判断。
        has_admin_role = any(r.is_admin for r in current_user.roles)
        if has_admin_role:
            return current_user

        if require_all:
            # ALL 模式：所需权限集合减去用户权限集合，剩余元素就是缺少的权限。
            missing = perm_codes_set - user_perm_codes
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"无权访问该资源（缺少权限: {', '.join(missing)}）",
                )
        else:
            # 任一权限即可
            #判断两个集合是否 完全不相交 （没有任何交集）
            # OR 模式：只要两个集合存在至少一个共同元素就放行；完全无交集才拒绝。
            if perm_codes_set and perm_codes_set.isdisjoint(user_perm_codes):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"无权访问该资源（缺少权限: {', '.join(perm_codes_set - user_perm_codes)}）",
                )
        # 返回 current_user 使路由既完成权限检查，也能直接取得当前用户对象。
        return current_user
    # 注意这里返回函数对象 dep，而不是执行 dep()；真正执行由 FastAPI 依赖系统完成。
    return dep


# 指定知识库访问检查。与 require_permission 的通用菜单权限不同，它判断形如 kb:17 的
# 资源级权限。调用者需要显式传入 kb_id、数据库会话和已经认证的 User。
async def require_kb_access(kb_id: int, db: AsyncSession, user: User) -> bool:
    """
    检查用户是否有权访问指定知识库。
    超级管理员或有对应 KB 权限的用户可访问。
    """
    if user.is_admin:
        return True

    # 检查用户是否有 admin 角色
    if any(r.is_admin for r in user.roles):
        return True

    # 检查该知识库是否在用户的知识库权限列表中
    # 例如 kb_id=17 时，目标权限编码是 "kb:17"。
    user_perms = await _get_user_permissions(db, user.id)
    kb_perm_codes = {p.code for p in user_perms if p.code.startswith("kb:")}
    if f"kb:{kb_id}" in kb_perm_codes:
        return True

    # 当前函数只返回布尔值，不直接抛出 403；调用它的业务代码决定如何处理 False。
    return False


# 权限聚合与缓存是本文件的核心辅助流程：
#
#   key = user_perms:<user_id>
#             |
#             v
#       Redis has data? --yes--> rebuild lightweight Permission objects -> return
#             |
#             no
#             v
#   query User + roles + permissions
#             |
#             v
#   remove duplicates -> serialize to dictionaries -> cache with TTL -> return
#
# Redis 中不能直接可靠保存 SQLAlchemy ORM 对象，因此缓存的是普通 JSON 字典；命中后
# 再构造仅供读取字段的 Permission 对象。TTL 到期后缓存自动失效并重新查询数据库。
async def _get_user_permissions(db: AsyncSession, user_id: int) -> list[Permission]:
    """获取用户所有权限（含角色继承的），优先从 Redis 缓存读取"""
    from app.core.redis_client import cache_get_json, cache_set_json
    from app.core.config import settings as app_settings

    # 每个用户使用独立 key，避免不同用户的权限数据互相覆盖。
    cache_key = f"user_perms:{user_id}"
    cached = await cache_get_json(cache_key)
    if cached:
        perms = []
        for p_data in cached:
            # 此处对象主要作为带有 id/code/name/type 属性的数据载体，并未加入数据库会话。
            perm = Permission(id=p_data["id"], code=p_data["code"], name=p_data["name"], type=p_data["type"])
            perms.append(perm)
        return perms

    # 缓存未命中时，通过 selectinload 批量加载用户的全部角色和每个角色的权限。
    user_result = await db.execute(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.id == user_id)
    )
    user = user_result.unique().scalar_one_or_none()
    if not user:
        return []
    # perms 保存最终返回对象；perm_data 保存可 JSON 序列化的缓存形式。
    perms = []
    perm_data = []
    for role in user.roles:
        for perm in role.permissions:
            # 同一个权限可能由多个角色提供，需要去重后再返回和缓存。
            if perm not in perms:
                perms.append(perm)
                perm_data.append({"id": perm.id, "code": perm.code, "name": perm.name, "type": perm.type})

    # TTL 来自配置；缓存减少每个请求都连接多张权限表的查询成本。
    await cache_set_json(cache_key, perm_data, ttl=app_settings.PERMISSION_CACHE_TTL)
    return perms


# 把资源级权限编码转换成整数 ID：
#
#   Permission(type="kb", code="kb:17") -> 17
#
# 不符合 kb:<integer> 格式的权限会被跳过，而不会让整个请求失败。
async def get_user_kb_ids(db: AsyncSession, user: User) -> list[int]:
    """获取用户有权限访问的知识库 ID 列表（仅限 kb 类型的权限）"""
    user_perms = await _get_user_permissions(db, user.id)
    kb_ids = []
    for p in user_perms:
        if p.type == "kb" and p.code.startswith("kb:"):
            try:
                # split(":")[1] 取得冒号右侧文本，再转换为整数。
                kb_ids.append(int(p.code.split(":")[1]))
            except (ValueError, IndexError):
                # ValueError：右侧不是整数；IndexError：编码中没有冒号后的部分。
                pass
    return kb_ids


# 修改某个用户的角色后调用，使下一次权限查询不再读取该用户的旧缓存。
async def invalidate_user_permission_cache(user_id: int):
    try:
        from app.core.redis_client import cache_delete
        await cache_delete(f"user_perms:{user_id}")
    except Exception:
        # 缓存失效失败不阻断主要数据库操作；代价是旧缓存可能持续到 TTL 到期。
        pass


# 角色权限变化可能同时影响许多用户，因此按模式删除全部 user_perms:* 缓存。
async def invalidate_all_permission_cache():
    try:
        from app.core.redis_client import cache_delete_pattern
        await cache_delete_pattern("user_perms:*")
    except Exception:
        # Redis 暂时不可用时保持业务可继续运行，缓存会在服务恢复或 TTL 到期后更新。
        pass


# 可选身份认证适合文件预览/下载等场景：浏览器有时不方便为直接打开的链接设置自定义
# Authorization Header，因此允许把 Token 放在 query 参数中。没有 Token 时返回 None，
# 调用路由再决定资源是否允许匿名访问。
#
#   request
#      |
#      +--> Authorization header exists --> verify header token
#      |
#      +--> otherwise query ?token= exists -> verify query token
#      |
#      +--> neither exists -------------> None
async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """
    用于文件类接口：优先从 Header Authorization 获取 token，
    未带则尝试从 query token 参数获取，均无则返回 None。
    """
    # 先读取 query token，但实际判断顺序仍优先使用 Authorization Header。
    token = request.query_params.get("token")
    authorization = request.headers.get("Authorization")
    scheme, credentials = get_authorization_scheme_param(authorization)

    if authorization and scheme and credentials:
        # Header 优先，避免同时提供两种 Token 时产生歧义。
        return await _verify_user(credentials, db)
    elif token:
        return await _verify_user(token, db)
    else:
        # “可选”只表示可以不提供 Token；一旦提供无效 Token，_verify_user 仍会抛出 401。
        return None
