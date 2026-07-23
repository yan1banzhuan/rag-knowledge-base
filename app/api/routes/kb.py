# =============================================================================
# 文件作用与架构位置（零基础导读）
# =============================================================================
# 本文件是“知识库（Knowledge Base）模块的 HTTP 路由层”。它把前端发来的 HTTP
# 请求转换为 Python 函数调用，再协调权限检查、数据库读写、向量库清理和响应序列化。
#
# 它为什么存在？
#
#   前端只知道 HTTP 地址，例如 POST /api/v1/kb；数据库只认识 SQL 和 ORM 对象。
#   本文件位于二者之间，负责把“创建、查看、修改、删除知识库”这些业务动作翻译成
#   对数据库及其他存储系统的具体操作。
#
# 在项目分层架构中的位置：
#
#   Vue 前端页面
#       |
#       | HTTP / JSON
#       v
#   main.py
#       | 把本 router 统一挂载到 /api/v1
#       v
#   app/api/routes/kb.py                  <- 当前文件：路由协调层
#       |
#       +--> app/api/deps.py              身份认证、菜单权限、知识库授权
#       +--> app/models/schemas.py        校验请求数据、规定响应数据形状
#       +--> app/models/db.py             SQLAlchemy ORM 数据模型
#       +--> app/db/session.py            创建数据库会话、提交或回滚事务
#       +--> app/db/vector_store.py       管理知识库对应的向量集合
#       +--> app/services/document.py     清理 BM25 检索缓存
#
# 本文件共有 6 个函数：
#
#   1. create_kb()       POST   /kb          创建知识库
#   2. list_kbs()        GET    /kb          分页查询当前用户可见的知识库
#   3. get_kb()          GET    /kb/{id}     查询单个知识库
#   4. update_kb()       PUT    /kb/{id}     修改知识库
#   5. delete_kb()       DELETE /kb/{id}     删除知识库及关联检索数据
#   6. _get_kb_or_404()  内部辅助函数        查询知识库并检查资源级访问权限
#
# 函数之间的关系：
#
#   create_kb()                         直接创建，不需要调用内部查询辅助函数
#
#   list_kbs() -----------------------> get_user_kb_ids()
#       |                                取得角色授权的知识库 ID
#       +--> 统计每个知识库的真实文档数
#
#   get_kb() -----+
#   update_kb() --+--> _get_kb_or_404() --> get_user_kb_ids()（读操作需要时）
#   delete_kb() --+        |
#                          +--> 不存在：404
#                          +--> 没有资源权限：403
#                          +--> 允许访问：返回 KnowledgeBase ORM 对象
#
# 所有 5 个对外路由都先经过 require_permission("kb_manage")：
#
#   HTTP 请求
#       |
#       v
#   验证 JWT，恢复 current_user          身份不合法 -> 401
#       |
#       v
#   检查 kb_manage 菜单权限              权限不足   -> 403
#       |
#       v
#   执行本文件中的路由函数
#
# 注意：菜单权限只是第一层检查。get_kb、update_kb、delete_kb 还会调用
# _get_kb_or_404() 做第二层“具体知识库资源”检查。
# =============================================================================

# FastAPI 路由、依赖注入以及 HTTP 异常工具。
from fastapi import APIRouter, Depends, HTTPException
# SQLAlchemy 的异步数据库会话类型；真正的会话对象由 get_db() 提供。
from sqlalchemy.ext.asyncio import AsyncSession
# select 用于构造 SELECT 查询；func 用于调用数据库 COUNT 等聚合函数。
from sqlalchemy import select, func
# FastAPI 数据库依赖。请求正常结束后提交事务，发生异常时回滚，最后关闭会话。
from app.db.session import get_db
# 管理向量数据库中的知识库集合；删除知识库时要同步删除对应向量。
from app.db.vector_store import VectorStore
# ORM 模型：User 对应用户表，KnowledgeBase 对应知识库表，Document 对应文档表。
from app.models.db import User, KnowledgeBase, Document
# Pydantic Schema：校验创建/更新请求，并控制普通响应和分页响应的 JSON 结构。
from app.models.schemas import KBCreate, KBUpdate, KBOut, Resp, PageResp
# 认证与授权依赖：恢复当前用户、检查菜单权限、查询用户获授权的知识库 ID。
from app.api.deps import get_current_user, require_permission, get_user_kb_ids
# Optional 表示“值可以是某类型，也可以是 None”；当前文件暂时没有直接使用该导入。
from typing import Optional

# APIRouter 是一组相关接口的集合。
# prefix="/kb" 表示下面的 "" 和 "/{kb_id}" 都会自动加上 /kb 前缀。
# main.py 又会给本 router 加上 /api/v1，所以最终地址是 /api/v1/kb...。
# tags 用于把这些接口归类到 Swagger/OpenAPI 文档的“知识库”分组。
router = APIRouter(prefix="/kb", tags=["知识库"])


# =============================================================================
# create_kb：创建知识库
# =============================================================================
# 请求流程：
#
#   POST /api/v1/kb
#   JSON: {"name": "产品资料", "description": "产品相关文档"}
#       |
#       v
#   KBCreate 校验请求体
#       |
#       v
#   get_db 提供 AsyncSession + require_permission 验证用户及权限
#       |
#       v
#   创建 KnowledgeBase ORM 对象，并把 owner_id 设为当前用户 ID
#       |
#       v
#   INSERT -> 获取数据库生成的 id/时间 -> 转成 KBOut -> 返回 Resp
#       |
#       v
#   路由正常结束后，get_db() 统一 commit
#
# response_model=Resp 告诉 FastAPI：最终响应应符合 Resp 的结构，并生成相应接口文档。
@router.post("", response_model=Resp)
async def create_kb(
    # body 来自请求 JSON；FastAPI 已在进入函数前将它校验并转换为 KBCreate 对象。
    body: KBCreate,
    # Depends(get_db) 让 FastAPI 为本次请求注入一个异步数据库会话。
    db: AsyncSession = Depends(get_db),
    # require_permission("kb_manage") 是一个依赖工厂：注册路由时生成权限检查函数，
    # 每次收到请求时才真正认证 Token、检查权限，并把验证成功的 User 注入进来。
    current_user: User = Depends(require_permission("kb_manage")),
):
    # body.model_dump() 把 KBCreate 对象转成字典，例如：
    # {"name": "产品资料", "description": "产品相关文档"}。
    # ** 会把字典展开成关键字参数；owner_id 不相信前端输入，而使用当前登录用户 ID。
    # 当前代码固定 is_public=True，所以新建知识库会被标记为公开。
    kb = KnowledgeBase(**body.model_dump(), owner_id=current_user.id, is_public=True)
    # 把新对象放入 SQLAlchemy 会话。此时通常只是登记“待插入”，还不一定已执行 SQL。
    db.add(kb)
    # flush() 立即把待处理 INSERT 发送到数据库，但尚未最终提交事务。
    # 执行后可以拿到数据库生成的 kb.id；若后续发生异常，事务仍可回滚。
    await db.flush()
    # refresh() 从数据库重新读取这条记录，补齐 id、created_at 等数据库生成的字段。
    await db.refresh(kb)
    # KBOut.model_validate(kb) 从 ORM 属性生成安全的响应模型；Resp 再统一包装到 data 中。
    # 大致返回：{"code": 200, "message": "success", "data": {...知识库字段...}}。
    return Resp(data=KBOut.model_validate(kb))


# =============================================================================
# list_kbs：分页列出当前用户能够看到的知识库
# =============================================================================
# 可见范围：
#
#   current_user.is_admin == True
#       |
#       +--> 查询全部知识库
#
#   current_user.is_admin == False
#       |
#       +--> 自己创建的知识库
#       +--> 角色权限中显式授权的知识库（权限编码形如 kb:17）
#
# 查询和组装流程：
#
#   page/page_size -> 计算 offset
#       |
#       v
#   根据用户身份构造基础查询 q
#       |
#       +--> 用 q 的子查询统计 total（分页前总数）
#       |
#       +--> 按创建时间倒序 + offset + limit 查询当前页
#       |
#       v
#   一次 GROUP BY 查询当前页所有知识库的真实文档数量
#       |
#       v
#   KnowledgeBase ORM 列表 -> KBOut 列表 -> PageResp
@router.get("", response_model=PageResp)
async def list_kbs(
    # page 和 page_size 是 URL 查询参数，例如：GET /kb?page=2&page_size=20。
    # 如果前端不传，则分别使用默认值 1 和 20。
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    # SQL 的 offset 表示跳过多少行。
    # 例如 page=1 时 offset=0；page=2、page_size=20 时 offset=20。
    offset = (page - 1) * page_size

    # 超级管理员可查看所有知识库，普通用户只能看自己的 + 角色授权的
    if current_user.is_admin:
        # select(KnowledgeBase) 只是构造查询对象，此处还没有真正访问数据库。
        q = select(KnowledgeBase)
    else:
        # 返回当前用户角色授权的知识库 ID，例如 [3, 8, 17]。
        # 自己创建的知识库即使不在此列表中，也会由下面的 owner_id 条件选中。
        user_kb_ids = await get_user_kb_ids(db, current_user)
        # “|” 在 SQLAlchemy 条件表达式中表示 SQL OR，而不是普通 Python 布尔 or。
        # 最终条件近似为：WHERE owner_id = 当前用户ID OR id IN (授权ID列表)。
        q = select(KnowledgeBase).where(
            (KnowledgeBase.owner_id == current_user.id)
            | (KnowledgeBase.id.in_(user_kb_ids))
        )
    # 把基础查询 q 包成子查询，再 COUNT，得到符合可见范围的总记录数。
    # 这里还没有应用 offset/limit，因此 total 是全部页的总数，不只是当前页数量。
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    # 在基础查询上加入分页和排序，并真正执行 SQL：
    # 先按 created_at 从新到旧排序，然后跳过 offset 行，最多取 page_size 行。
    result = await db.execute(q.offset(offset).limit(page_size).order_by(KnowledgeBase.created_at.desc()))
    # result 是 SQLAlchemy 查询结果；scalars() 只取每行中的 KnowledgeBase 对象，
    # all() 再把它们收集成列表。没有记录时得到空列表 []，而不是 None。
    kbs = result.scalars().all()

    # 实时从 documents 表查询真实文档数，避免 doc_count 字段不同步
    if kbs:
        # 只统计当前页出现的知识库，避免对所有知识库做无用统计。
        kb_ids = [kb.id for kb in kbs]
        # 生成类似下面的聚合查询：
        # SELECT kb_id, COUNT(document.id)
        # FROM documents
        # WHERE kb_id IN (...当前页知识库ID...)
        # GROUP BY kb_id;
        # 一次查询即可得到多个知识库的文档数，避免循环中每个知识库查询一次的 N+1 问题。
        count_rows = await db.execute(
            select(Document.kb_id, func.count(Document.id))
            .where(Document.kb_id.in_(kb_ids))
            .group_by(Document.kb_id)
        )
        # 把数据库结果 [(1, 5), (2, 3)] 转成字典 {1: 5, 2: 3}，便于按 kb.id 查找。
        count_map = {row[0]: row[1] for row in count_rows.fetchall()}
        for kb in kbs:
            # 某知识库没有文档时，GROUP BY 结果中不会有它，所以使用默认值 0。
            # doc_count 是 ORM 映射字段；赋值既供本次响应使用，也会把对象标记为可能已修改，
            # 请求正常结束并 commit 时，SQLAlchemy 可能把修正后的计数同步回数据库。
            kb.doc_count = count_map.get(kb.id, 0)

    # 列表推导式逐个把 ORM 对象转换成 KBOut；PageResp 同时返回分页元数据。
    return PageResp(data=[KBOut.model_validate(kb) for kb in kbs], total=total, page=page, page_size=page_size)


# =============================================================================
# get_kb：读取单个知识库
# =============================================================================
#
#   GET /api/v1/kb/17
#       |
#       v
#   kb_id = 17（FastAPI 将路径字符串转换为 int）
#       |
#       v
#   _get_kb_or_404(owner_only=False)
#       +--> 不存在：404
#       +--> 非所有者、非管理员、未获角色授权：403
#       +--> 允许：返回 KnowledgeBase
#       |
#       v
#   查询真实文档数 -> KBOut -> Resp
@router.get("/{kb_id}", response_model=Resp)
async def get_kb(
    # {kb_id} 是路径参数；类型标注 int 会让 FastAPI 自动校验并转换。
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    # 未传 owner_only，使用默认 False，表示这是读操作：所有者、管理员或获授权用户可访问。
    kb = await _get_kb_or_404(kb_id, current_user, db)
    # 实时查询真实文档数
    # select_from(Document) 指定从 documents 表统计，where 只保留当前知识库的文档。
    real_count = await db.scalar(
        select(func.count()).select_from(Document).where(Document.kb_id == kb_id)
    )
    # COUNT 通常返回整数；“or 0”用于防御性地把 None 等空值转换为 0。
    kb.doc_count = real_count or 0
    # 把查询和补充计数后的 ORM 对象转换为响应 JSON。
    return Resp(data=KBOut.model_validate(kb))


# =============================================================================
# update_kb：更新知识库名称或描述
# =============================================================================
#
#   PUT /api/v1/kb/17 + JSON 请求体
#       |
#       v
#   KBUpdate 校验 -> _get_kb_or_404(owner_only=True)
#       |
#       +--> 只有知识库所有者或超级管理员可以继续
#       v
#   取出本次实际提供的非 None 字段
#       |
#       v
#   setattr 修改 ORM 对象 -> flush UPDATE -> refresh -> Resp
@router.put("/{kb_id}", response_model=Resp)
async def update_kb(
    kb_id: int,
    # KBUpdate 中的字段是可选的，所以前端可以只修改 name 或只修改 description。
    body: KBUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    # owner_only=True 表示这是写操作：角色被授予“读取某知识库”不等于有权修改它。
    kb = await _get_kb_or_404(kb_id, current_user, db, owner_only=True)
    # model_dump(exclude_none=True) 只保留值不为 None 的字段。
    # 例如 body={name: "新名称", description: None} 会得到 {"name": "新名称"}。
    # 因此当前实现也意味着：传入 null 不会把 description 主动清空为数据库 NULL。
    for field, value in body.model_dump(exclude_none=True).items():
        # setattr(kb, "name", "新名称") 等价于 kb.name = "新名称"；
        # 使用循环可以用同一段代码更新任意被 Schema 允许的字段。
        setattr(kb, field, value)
    # 将 UPDATE 发送到数据库，但最终提交仍由 get_db() 在请求正常结束后完成。
    await db.flush()
    # 重新读取数据库记录，获得 updated_at 等可能由数据库更新的字段。
    await db.refresh(kb)
    return Resp(data=KBOut.model_validate(kb))


# =============================================================================
# delete_kb：删除知识库及其检索侧数据
# =============================================================================
# 一个知识库的数据不只存在关系数据库，还可能存在于向量库和 Redis 缓存中，因此删除
# 需要协调多个存储位置：
#
#   DELETE /api/v1/kb/17
#       |
#       v
#   _get_kb_or_404(owner_only=True)          不存在/无权 -> 404/403
#       |
#       v
#   删除向量库 collection：kb_17
#       |
#       v
#   标记删除 KnowledgeBase ORM 对象
#       |
#       +--> ORM cascade 会连带删除关联 Document 数据库记录
#       |
#       v
#   删除 Redis 中 kb:17:bm25 缓存
#       |
#       v
#   返回成功响应 -> get_db() 提交数据库事务
#
# 注意：向量库和 Redis 不属于当前 SQL 数据库事务。它们的删除不能随着 SQL rollback
# 自动恢复；当前相关辅助函数会忽略清理异常，以避免“清理数据不存在”阻止主删除流程。
@router.delete("/{kb_id}", response_model=Resp)
async def delete_kb(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("kb_manage")),
):
    # 删除属于写操作，因此只有所有者或超级管理员可以执行。
    kb = await _get_kb_or_404(kb_id, current_user, db, owner_only=True)
    # 每个知识库在向量数据库中的集合名形如 kb_{kb_id}。
    # delete_collection 内部捕获异常，因此集合不存在等情况不会中断本请求。
    VectorStore.delete_collection(kb_id)
    # db.delete() 把 ORM 对象标记为待删除；真正 DELETE/commit 在会话 flush 或结束时完成。
    # KnowledgeBase.documents 配置了 delete-orphan 级联，关联文档记录也会一并处理。
    await db.delete(kb)

    # 放在函数内部导入可以避免模块加载时产生不必要或循环的导入依赖。
    from app.services.document import _invalidate_bm25_cache
    # BM25 是另一种文本检索索引。知识库删除后，旧缓存必须失效，避免继续读到旧内容。
    # 该辅助函数内部也会捕获 Redis 清理异常。
    await _invalidate_bm25_cache(kb_id)

    # data 默认为 None，只返回统一的成功 code 和中文 message。
    return Resp(message="知识库已删除")


# =============================================================================
# _get_kb_or_404：共享的“查询 + 资源权限检查”辅助函数
# =============================================================================
# 函数名前的下划线表示它主要供本模块内部使用，不是通过装饰器暴露的 HTTP 接口。
# 它统一了 get/update/delete 三个路由的重复逻辑，确保它们使用相同的 404/403 规则。
#
#   输入 kb_id、user、db、owner_only
#       |
#       v
#   按主键查询 KnowledgeBase
#       |
#       +--> 查不到 ------------------------------------------> HTTP 404
#       |
#       v
#   owner_only == True？
#       |
#       +--> 是：所有者或超级管理员？ ----否------------------> HTTP 403
#       |                          |
#       |                          是
#       |                          v
#       |                       返回 kb
#       |
#       +--> 否：所有者或超级管理员？ ----是------------------> 返回 kb
#                                  |
#                                  否
#                                  v
#                         查询角色授权知识库 ID
#                                  |
#                         kb.id 在授权列表中？
#                            |             |
#                           是            否
#                            |             |
#                         返回 kb       HTTP 403
#
# 注意：当前读权限逻辑判断的是所有者、超级管理员或角色授权，并没有读取 kb.is_public。
# 换句话说，is_public=True 在本函数当前实现中不会单独让所有登录用户获得访问权。
async def _get_kb_or_404(
    # owner_only=False 用于读取；True 用于更新和删除。
    kb_id: int, user: User, db: AsyncSession, owner_only: bool = False
) -> KnowledgeBase:
    # 构造并执行“按知识库 ID 查询”的 SQL；主键最多匹配一行。
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    # 查询到一行就返回 ORM 对象，查不到返回 None；若意外多行则抛异常。
    kb = result.scalar_one_or_none()
    if not kb:
        # HTTPException 会立即终止当前依赖/路由执行，由 FastAPI 转换成 JSON 错误响应。
        raise HTTPException(status_code=404, detail="知识库不存在")
    if owner_only:
        # 写操作（更新/删除）：仅所有者或超级管理员可执行
        # “and” 表示两个条件必须同时成立才拒绝：既不是所有者，并且也不是管理员。
        if kb.owner_id != user.id and not user.is_admin:
            raise HTTPException(status_code=403, detail="无权操作该知识库")
    else:
        # 读操作：所有者、或角色授权、或超级管理员可访问
        # 所有者和管理员不需要额外查询角色知识库权限，可以直接跳过下面的数据库/缓存访问。
        if kb.owner_id != user.id and not user.is_admin:
            # 普通的非所有者需要检查其角色是否包含形如 kb:{id} 的资源权限。
            user_kb_ids = await get_user_kb_ids(db, user)
            if kb.id not in user_kb_ids:
                raise HTTPException(status_code=403, detail="无权访问该知识库")
    # 能执行到这里，说明知识库存在且用户具有当前操作所要求的访问权限。
    return kb
