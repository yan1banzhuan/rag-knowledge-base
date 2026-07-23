from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db.session import get_db
from app.models.db import User, Role
from app.models.schemas import UserRegister, UserLogin, UserOut, Token, Resp
from app.core.security import get_password_hash, verify_password, create_access_token
from app.api.deps import get_current_user

# =============================================================================
# 文件作用与架构位置（零基础导读）
# =============================================================================
# 本文件是 FastAPI 的“认证路由层”，负责把与账号认证有关的 HTTP 地址映射到 Python
# 函数。它协调 Schema 校验、数据库查询、密码安全工具、JWT 生成和当前用户依赖，
# 但不定义 users 表，也不自己实现 bcrypt 或 JWT 算法。
#
# 架构位置：
#
#   Frontend
#      |
#      | POST /auth/register, POST /auth/login,
#      | GET /auth/me/permissions
#      v
#   auth.py route functions                 <- 当前文件
#      |
#      +--> schemas.py       校验请求 / 组织响应
#      +--> session.py       提供数据库会话
#      +--> db.py            User、Role 等 ORM 模型
#      +--> security.py      密码哈希、校验、生成 JWT
#      +--> deps.py          从 JWT 恢复当前登录用户、查询权限
#
# 本文件共有 3 个路由函数：
#
#   register(body, db)
#       注册用户，检查用户名/邮箱是否重复，并保存密码哈希。
#
#   login(body, db)
#       校验用户名和密码，检查账号状态，生成 JWT access token。
#
#   get_my_permissions(db, current_user)
#       返回当前登录用户的角色、菜单权限、知识库权限和知识库列表。
#
# 三者之间没有直接互相调用，但组成完整认证使用流程：
#
#   register
#      |
#      v
#   login -> receive access_token
#      |
#      | Authorization: Bearer <token>
#      v
#   get_current_user dependency in deps.py
#      |
#      v
#   /auth/me/permissions and other protected APIs
#
# APIRouter 只收集本模块路由，之后会由应用入口通过 include_router 挂载到 FastAPI。
# =============================================================================

# prefix="/auth" 表示下面装饰器中的 "/login" 最终地址是 "/auth/login"。
# tags 用于把这些接口归类到 Swagger/OpenAPI 文档的“认证”分组。
router = APIRouter(prefix="/auth", tags=["认证"])


# 注册流程：
#
#   JSON body
#      |
#      v
#   UserRegister validation
#      |
#      v
#   username/email already exists? --yes--> HTTP 400
#      |
#      no
#      v
#   hash plaintext password -> create User -> flush/refresh/commit
#      |
#      v
#   UserOut -> Resp -> JSON response
#
# response_model=Resp 会让 FastAPI 按 Resp 的结构生成文档并序列化响应。
@router.post("/register", response_model=Resp)
async def register(body: UserRegister, db: AsyncSession = Depends(get_db)):
    # body 已经由 FastAPI/Pydantic 校验为 UserRegister；不符合字段约束的请求不会进入函数。
    # db 来自 get_db 依赖，每个请求拥有独立的 AsyncSession。
    # 检查用户名/邮箱是否已存在
    # “|” 在 SQLAlchemy 条件表达式中表示 OR：用户名相同或邮箱相同都视为冲突。
    result = await db.execute(
        select(User).where((User.username == body.username) | (User.email == body.email))
    )
    if result.scalar_one_or_none():
        # HTTPException 会立即终止路由函数，并由 FastAPI 转换成 HTTP 错误响应。
        raise HTTPException(status_code=400, detail="用户名或邮箱已被注册")

    # 数据库只保存哈希结果，不应保存 body.password 明文。
    user = User(
        username=body.username,
        email=body.email,
        hashed_password=get_password_hash(body.password),
    )
    # add() 把新对象加入会话；此时不保证 INSERT 已经发送到数据库。
    db.add(user)
    # flush() 执行待处理 INSERT，从而获得数据库生成的自增 user.id。
    await db.flush()
    # refresh() 从数据库重新读取该行，补齐 created_at 等数据库端生成的字段。
    await db.refresh(user)
    # 提交事务，让用户记录持久化。get_db 在请求正常结束后也会执行统一提交。
    await db.commit()
    # model_validate 依靠 UserOut.from_attributes 从 ORM 属性读取数据；响应中不含密码哈希。
    return Resp(data=UserOut.model_validate(user))


# 登录流程：
#
#   username + plaintext password
#              |
#              v
#        query User by username
#              |
#       +------+------+
#       |             |
#   missing or       found
#   bad password       |
#       |          active? --no--> HTTP 403
#       v             |
#    HTTP 401         yes
#                     v
#             create JWT with sub=user.id
#                     |
#                     v
#              Token response
@router.post("/login", response_model=Token)
async def login(body: UserLogin, db: AsyncSession = Depends(get_db)):
    # 用户名适合做精确查询；User.username 在模型中具有唯一约束和索引。
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    # 使用短路逻辑：用户不存在时不会继续读取 user.hashed_password。
    # verify_password 内部比较明文密码与 bcrypt 哈希，数据库哈希无法直接还原成明文。
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    # 密码正确但账号被管理员禁用时，不允许签发可使用的登录令牌。
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已禁用")

    # JWT 的 sub（subject，主体）字段保存用户 ID；转换为字符串便于统一编码和读取。
    token = create_access_token({"sub": str(user.id)})

    # 加载用户角色和权限（selectinload 兼容异步 session）
    # selectinload 属于“预加载”：主动查询关联数据，避免之后访问 user.roles 时在不可控位置
    # 触发额外的异步数据库 IO。unique() 用于处理关联加载可能产生的重复用户行。
    result = await db.execute(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.id == user.id)
    )
    user = result.unique().scalar_one()

    # token_type 默认是 bearer；客户端后续把 access_token 放入 Authorization 请求头。
    return Token(access_token=token, user=UserOut.model_validate(user))


# 当前用户权限信息流程：
#
#   request with Authorization header
#                 |
#                 v
#   Depends(get_current_user) in deps.py
#                 |
#                 v
#   verify/decode token -> load active User
#                 |
#                 v
#   reload roles and permissions
#        |                 |
#        v                 v
#   menu permission     KB permission IDs
#        |                 |
#        +--------+--------+
#                 v
#      combined response for frontend
#
# 该接口常在前端登录后调用，用于决定显示哪些菜单和允许访问哪些知识库。
@router.get("/me/permissions", response_model=Resp)
async def get_my_permissions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    获取当前用户的角色和权限信息，供前端渲染菜单和权限判断使用。
    """
    # 下划线函数 _get_user_permissions 虽然主要供 deps.py 内部使用，但这里复用同一套
    # 权限聚合与 Redis 缓存逻辑，避免路由自己重新实现权限查询。
    from app.api.deps import _get_user_permissions, get_user_kb_ids

    # 用 selectinload 正确加载角色和权限
    # current_user 已通过身份验证；这里按 ID 再查询一次是为了明确预加载 roles.permissions。
    me_result = await db.execute(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.id == current_user.id)
    )
    current_user = me_result.unique().scalar_one()

    # user_perms 是用户所有角色权限去重后的列表；user_kb_ids 只提取 kb:<id> 类型权限。
    user_perms = await _get_user_permissions(db, current_user.id)
    user_kb_ids = await get_user_kb_ids(db, current_user)

    # 菜单权限码列表
    # 前端可根据这些稳定的 code 决定菜单是否显示，而不是比较中文权限名称。
    menu_perm_codes = [p.code for p in user_perms if p.type == "menu"]

    # 获取当前所有知识库列表（用于角色配置知识库权限时展示）
    # 此列表只保留 id 和 name，避免把完整知识库 ORM 对象直接返回给客户端。
    from app.models.db import KnowledgeBase
    from sqlalchemy import select as sa_select
    kbs_result = await db.execute(sa_select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc()))
    kbs = [
        {"id": kb.id, "name": kb.name}
        for kb in kbs_result.scalars().all()
    ]

    # 最终 data 同时包含身份信息、角色详情、两类权限和可供配置的知识库选项。
    return Resp(data={
        "user": UserOut.model_validate(current_user),
        "is_admin": current_user.is_admin,
        "roles": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "is_admin": r.is_admin,
                "permission_ids": [p.id for p in r.permissions],
            }
            for r in current_user.roles
        ],
        "menu_permission_codes": menu_perm_codes,
        "kb_permission_ids": user_kb_ids,
        "all_kbs": kbs,
    })
