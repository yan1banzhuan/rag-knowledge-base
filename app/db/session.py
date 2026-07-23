from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import create_engine, select, text
from app.core.config import settings
from app.models.db import Base

# =============================================================================
# 文件作用与架构位置（零基础导读）
# =============================================================================
# 本文件是项目的“数据库连接与会话入口”。db.py 负责说明数据库中有哪些表，
# 而本文件负责建立真实数据库连接、创建会话、管理事务，并在应用启动时完成基础数据
# 初始化。API 路由通常不会自己创建数据库连接，而是通过 Depends(get_db) 使用这里的
# get_db。
#
# 架构位置：
#
#   API route / Service
#          |
#          | Depends(get_db) 或 AsyncSessionLocal()
#          v
#   app/db/session.py              <- 当前文件
#          |
#          | SQLAlchemy engine/session
#          v
#   MySQL database
#          ^
#          |
#   app/models/db.py 提供表结构元数据 Base.metadata
#
# 本文件共有 5 个异步函数：
#
#   get_db()
#       为一次 FastAPI 请求提供 AsyncSession，并负责提交或异常回滚。
#
#   init_db()
#       应用启动初始化入口：创建尚不存在的表，然后调用 _init_admin_user()。
#
#   _init_admin_user()
#       初始化权限、超级管理员角色和默认 admin 用户。
#
#   _init_permissions(session)
#       补齐系统权限，并迁移/清理已废弃的旧权限。
#
#   _init_admin_role(session)
#       在超级管理员角色不存在时创建它，并关联当时已有的全部权限。
#
# 函数调用关系：
#
#   Application startup
#          |
#          v
#      init_db()
#          |
#          +--> Base.metadata.create_all
#          |
#          +--> _init_admin_user()
#                    |
#                    +--> _init_permissions(session)
#                    |
#                    +--> _init_admin_role(session)
#                    |
#                    +--> create admin user if missing
#
# 日常请求和启动初始化是两条不同路径：
#
#   日常请求：Route -> get_db -> yield session -> commit / rollback
#   启动阶段：App startup -> init_db -> create tables and seed base data
#
# 名称以下划线开头（例如 _init_admin_user）表示它主要是本模块内部辅助函数，
# 这是一种 Python 命名约定，并不是强制的访问权限限制。
# =============================================================================

# 异步引擎（日常使用）
# Engine 可以理解为 SQLAlchemy 与数据库之间的“连接管理中心”，它内部维护连接池。
# create_async_engine 不代表此刻永久占用一个连接；实际执行 SQL 时才会从连接池取连接。
async_engine = create_async_engine(
    # 异步数据库连接地址，例如 mysql+aiomysql://user:password@host/database。
    settings.DATABASE_URL,
    # 连接池长期保留的基础连接数量。
    pool_size=10,
    # 基础连接全部繁忙时，最多允许额外创建的临时连接数量。
    max_overflow=20,
    # 连接使用一小时后回收重建，降低数据库主动断开陈旧连接造成的错误概率。
    pool_recycle=3600,
    # 开发环境输出 SQL，便于调试；其他环境关闭，避免日志过多或泄露参数。
    echo=(settings.APP_ENV == "development"),
)

#SQLAlchemy 异步数据库会话的配置，用于创建数据库连接工厂。
#class_=AsyncSession 指定会话类型为异步会话类
#expire_on_commit=False 提交事务后不自动过期对象（保持对象可访问）
AsyncSessionLocal = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)

# 同步引擎（Alembic 迁移用）
# Alembic 或某些同步工具无法直接使用 AsyncSession，因此单独准备同步连接地址。
sync_engine = create_engine(settings.DATABASE_URL_SYNC, echo=False)

# 异步数据库会话管理器
# 用于在异步请求中获取数据库会话
# 会话在请求处理完成后自动提交或回滚
#
# 请求事务流程：
#
#   FastAPI enters dependency
#            |
#            v
#   create AsyncSession
#            |
#            v
#   yield session ------------------> route executes SQL
#            |                              |
#            | route returns normally       | route raises exception
#            v                              v
#        commit()                       rollback()
#            |                              |
#            +--------------+---------------+
#                           v
#                 leave async with block
#                 session is automatically closed
#
# get_db 中存在 yield，所以它是“异步生成器依赖”，不是一次 return 后立即结束的函数。
# yield 之前相当于请求前准备，yield 之后相当于请求结束时的清理阶段。
async def get_db() -> AsyncSession:
    # async with 保证离开代码块时关闭会话，并把数据库连接归还给连接池。
    async with AsyncSessionLocal() as session:
        try:
            # 暂时把 session 交给依赖此函数的路由或其他依赖函数使用。
            yield session
            # 只有下游代码正常结束，才提交本次会话中尚未提交的修改。
            await session.commit()
        except Exception:
            # 任意异常都撤销本次事务中尚未提交的修改，避免保存半完成的数据。
            await session.rollback()
            # 继续抛出原异常，让 FastAPI 的异常处理机制生成正确响应和日志。
            raise


# 应用启动阶段的数据库初始化入口。它只负责协调步骤，具体种子数据初始化由下面的
# 私有辅助函数完成。
async def init_db():
    """初始化数据库表，并执行必要的列迁移"""
    async with async_engine.begin() as conn:
        # begin() 建立一个事务上下文；conn 是异步连接对象。
        #conn.run_sync() 是 SQLAlchemy 异步引擎提供的一个方法，用于 在异步上下文中执行同步代码 。
        
        '''
        Base.metadata.create_all 的作用：
        - 扫描所有继承自 Base 的 SQLAlchemy 模型类
        - 根据模型定义自动创建对应的数据库表
        - 如果表已经存在，则 不会重复创建 （安全操作）
        '''
        await conn.run_sync(Base.metadata.create_all)

    # 初始化 admin 超级管理员账号
    # 表创建完成后才能查询或写入 users、roles、permissions 等表。
    await _init_admin_user()


# 默认管理员与权限体系的总协调函数。这里在函数内部导入模型和工具，是为了减少模块
# 加载阶段的循环依赖风险，并且只在初始化真正执行时加载这些名称。
async def _init_admin_user():
    """若 admin 用户不存在则自动创建，并初始化权限体系"""
    from app.models.db import User, Role, Permission, PermissionCode
    from app.core.security import get_password_hash
    from sqlalchemy.exc import IntegrityError

    async with AsyncSessionLocal() as session:
        # 此处没有使用 get_db，是因为它不是 HTTP 请求依赖，而是应用启动任务。
        # 检查 admin 用户是否已存在
        result = await session.execute(select(User).where(User.username == "admin"))
        admin = result.scalar_one_or_none()

        # 初始化权限数据
        # 先初始化权限，再创建管理员角色，才能在新建角色时关联权限。
        await _init_permissions(session)

        # 创建 admin 角色（超级管理员）
        await _init_admin_role(session)

        # 初始化过程是幂等设计：如果 admin 已经存在，就不重复创建。
        if not admin:
            admin = User(
                username="admin",
                email="admin@rag-system.local",
                hashed_password=get_password_hash("admin123"),
                is_active=True,
            )
            # add() 把对象加入当前会话，此时通常还没有立即发送 INSERT。
            session.add(admin)
            # flush() 把 INSERT 发给数据库但不结束事务，目的是取得自增的 admin.id。
            await session.flush()
            # 自动分配 admin 角色给 admin 用户
            role_result = await session.execute(select(Role).where(Role.name == "超级管理员"))
            admin_role = role_result.scalar_one_or_none()
            if admin_role:
                from app.models.db import UserRole
                # User 与 Role 是多对多关系，所以通过 user_roles 关联表建立关系。
                session.add(UserRole(user_id=admin.id, role_id=admin_role.id))
            # 明确提交初始化事务。成功后权限、角色、用户和关联记录才会持久化。
            await session.commit()
            from app.core.logger import logger
            logger.info("已自动创建 admin 账号（初始密码: admin123，请及时修改）")
        else:
            # 即使 admin 已存在，前面权限迁移和角色初始化产生的修改仍需要提交。
            await session.commit()


# ---------- 权限初始化（幂等操作）----------
from app.models.db import Permission as PModel, Role as RModel, UserRole as URModel

PERMISSIONS_DATA = [
    {"code": "kb_manage",   "name": "知识库管理",    "type": "menu"},
    {"code": "chat",         "name": "问答对话",       "type": "menu"},
    {"code": "stats",        "name": "仪表盘统计",     "type": "menu"},
    {"code": "model_config",  "name": "模型配置",       "type": "menu"},
    {"code": "voice_config",  "name": "语音配置",       "type": "menu"},
    {"code": "user_manage",  "name": "用户管理",       "type": "menu"},
    {"code": "role_manage",  "name": "角色管理",       "type": "menu"},
]


# 权限初始化流程：
#
#   PERMISSIONS_DATA
#          |
#          v
#   each permission exists? --no--> insert permission
#          |
#         yes
#          v
#   inspect deprecated permission codes
#          |
#          +--> migrate affected role to kb_manage when necessary
#          +--> delete deprecated permission
#
# “幂等”表示重复执行应得到稳定结果：已存在的权限不会被重复新增。
async def _init_permissions(session: AsyncSession):
    """幂等初始化权限数据，并清理已废弃的权限"""
    for pdata in PERMISSIONS_DATA:
        # 按稳定且唯一的 code 查询，而不是依赖可能变化的中文 name。
        result = await session.execute(
            select(PModel).where(PModel.code == pdata["code"])
        )
        if result.scalar_one_or_none() is None:
            # **pdata 会把字典展开为 Permission(code=..., name=..., type=...)。
            session.add(PModel(**pdata))
    # 先 flush，确保新权限取得主键，后面的角色关联可以引用权限 ID。
    await session.flush()

    # 清理已废弃的权限（合并到 kb_manage 的细粒度权限）
    deprecated = ["kb_read", "kb_write", "kb_delete", "doc_upload", "doc_delete", "search"]
    for code in deprecated:
        result = await session.execute(select(PModel).where(PModel.code == code))
        old = result.scalar_one_or_none()
        if old:
            # 找出关联了废弃权限的角色，给它们补上 kb_manage
            from app.models.db import RolePermission
            # 找出关联了废弃权限的角色
            roles_with_old = await session.execute(
                select(RolePermission).where(RolePermission.permission_id == old.id)
            )
            for rp in roles_with_old.scalars().all():
                # 检查是否已有 kb_manage
                kb_manage_perm = await session.execute(
                    select(PModel).where(PModel.code == "kb_manage")
                )
                kbm = kb_manage_perm.scalar_one_or_none()
                if kbm:
                    # 避免重复关联 能查询到数据则说明已关联
                    dup = await session.execute(
                        select(RolePermission).where(
                            RolePermission.role_id == rp.role_id,
                            RolePermission.permission_id == kbm.id,
                        )
                    )
                    if dup.scalar_one_or_none() is None:
                        # 角色还没有 kb_manage 时才补充关联，避免生成重复关系。
                        session.add(RolePermission(role_id=rp.role_id, permission_id=kbm.id))
            # 删除废弃权限记录
            # role_permissions 对 permission 的外键配置为级联删除，因此旧关联会一并清理。
            await session.delete(old)
    # 把新增、迁移、删除操作发给数据库；最终 commit 由 _init_admin_user 负责。
    await session.flush()


# 超级管理员角色初始化流程：
#
#   query role named "超级管理员"
#            |
#       +----+----+
#       |         |
#    exists     missing
#       |         |
#       |      create role -> flush -> query all permissions -> create relations
#       |         |
#       +----+----+
#            v
#          flush
#
# 注意：按照当前代码，只有角色第一次创建时才会执行“关联所有权限”的循环。
async def _init_admin_role(session: AsyncSession):
    """幂等初始化超级管理员角色，并关联所有权限"""
    result = await session.execute(select(RModel).where(RModel.name == "超级管理员"))
    role = result.scalar_one_or_none()
    if role is None:
        role = RModel(name="超级管理员", description="系统超级管理员，拥有所有权限", is_admin=True)
        session.add(role)
        # 先取得新角色的自增 ID，才能创建 RolePermission 外键记录。
        await session.flush()
        # 关联所有权限
        all_perms = await session.execute(select(PModel))
        from app.models.db import RolePermission
        for perm in all_perms.scalars().all():
            session.add(RolePermission(role_id=role.id, permission_id=perm.id))
    # flush 只同步 SQL，不提交事务；调用者稍后统一 commit。
    await session.flush()
