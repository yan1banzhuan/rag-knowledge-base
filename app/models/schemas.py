from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Any
from datetime import datetime
from app.models.db import DocumentStatus

# =============================================================================
# 文件作用与架构位置（零基础导读）
# =============================================================================
# 本文件属于项目的“数据传输模型 / API Schema 层”。它使用 Pydantic 描述：
#
#   1. 前端调用 API 时，允许提交哪些字段；
#   2. 每个字段应该是什么类型、是否必填、长度或数值范围是多少；
#   3. 后端返回给前端的数据应该具有怎样的结构；
#   4. 如何把 SQLAlchemy ORM 对象转换成可序列化的响应对象。
#
# 它位于 API 路由与业务/数据库模型之间：
#
#   Frontend / HTTP client
#            |
#            | JSON request
#            v
#   FastAPI route function
#            |
#            | 使用本文件的 XxxCreate / XxxUpdate / XxxRequest 校验输入
#            v
#   Service / SQLAlchemy ORM model / Database
#            |
#            | 查询或修改结果
#            v
#   FastAPI route function
#            |
#            | 使用本文件的 XxxOut / XxxResponse 整理输出
#            v
#   JSON response returned to frontend
#
# 为什么不能直接把前端 JSON 或数据库对象到处传递：
#
#   - Schema 是 API 边界的“数据合同”，字段不符合要求时 FastAPI 会自动拒绝请求；
#   - 请求模型可以避免前端修改本不应该修改的数据库字段；
#   - 响应模型可以控制哪些字段允许返回，降低泄露密码哈希等敏感数据的风险；
#   - OpenAPI/Swagger 文档会读取这些类型和 Field 描述并自动生成接口说明。
#
# 本文件没有普通 def/async def 函数。这里的主要组成是 Pydantic 模型类：
#
#   认证：UserRegister、UserLogin、UserOut、Token
#   权限：PermissionOut、RoleCreate、RoleUpdate、RoleOut、RoleAssignReq、RoleWithKbIds
#   用户：UserWithRoles、UserAssignRoleReq
#   知识库：KBCreate、KBUpdate、KBOut
#   文档：DocumentOut
#   聊天：ChatSessionCreate、ChatSessionOut、ChatRequest、ChatMessageOut
#   检索：SearchRequest、SearchResult、SearchResponse
#   模型配置：ModelConfigUpdate、ModelConfigOut
#   语音配置：VoiceConfigUpdate、VoiceConfigOut
#   通用包装：Resp、PageResp
#
# 常见命名约定：
#
#   XxxCreate / XxxUpdate / XxxRequest  -> 通常用于接收请求
#   XxxOut / XxxResult / XxxResponse   -> 通常用于组织响应
#
# Field(...) 中的三个点是 Pydantic 的“必填”标记，不是省略代码。例如：
#
#   username: str = Field(..., min_length=3)
#
# 表示 username 必须提供，而且必须是字符串，长度至少为 3。
# Optional[str] 表示该值可以是字符串，也可以是 None。
# model_config = {"from_attributes": True} 允许模型读取 ORM 对象属性，例如把
# User.username 转换为 UserOut.username，而不要求输入一定是普通字典。
# =============================================================================

'''
    定义了 API 中的请求和响应模型，用于与前端进行交互。
'''

# ===== Auth =====
# 注册请求模型：只接收创建账号所需的数据，不允许客户端直接指定用户 ID、
# is_active、created_at 或 hashed_password 等由系统负责维护的字段。
class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, description="用户名, 3-64个字符, 不能包含特殊字符")
    email: EmailStr = Field(..., description="邮箱, 格式为 example@example.com")
    password: str = Field(..., min_length=6, description="密码, 6个字符以上")


# 登录请求模型：路由收到该模型后，会用 username 查询用户，再校验 password。
class UserLogin(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


# 安全的用户响应模型：只返回可以公开给客户端的用户信息，故意不包含
# hashed_password。auth.register、auth.login 和 /auth/me/permissions 都会使用它。
class UserOut(BaseModel):
    id: int = Field(..., description="用户ID")
    username: str = Field(..., description="用户名")
    email: str = Field(..., description="邮箱")
    is_active: bool = Field(..., description="是否激活")
    created_at: datetime = Field(..., description="创建时间")

    model_config = {"from_attributes": True}


# 登录成功响应模型：同时返回 JWT 字符串、认证方案名称和当前用户基本信息。
# 客户端后续通常发送请求头：Authorization: Bearer <access_token>。
class Token(BaseModel):
    access_token: str = Field(..., description="访问令牌")
    token_type: str = "bearer"
    user: UserOut


# ===== Permission & Role =====
# 权限输出模型：对应 db.py 的 Permission ORM 模型，用于向前端描述权限。
class PermissionOut(BaseModel):
    id: int = Field(..., description="权限ID")
    code: str = Field(..., description="权限编码")
    name: str = Field(..., description="权限名称")
    type: str = Field(..., description="权限类型")

    model_config = {"from_attributes": True} 


# 新建角色请求模型：角色的 ID、管理员标记和权限关联由服务端决定。
class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="角色名称, 1-64个字符")
    description: Optional[str] = None


# 更新角色请求模型：字段均可选，表示客户端可以只修改其中一部分。
class RoleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64, description="角色名称, 1-64个字符")
    description: Optional[str] = None


# 角色响应模型：除了角色本身的信息，还嵌套返回 PermissionOut 列表。
# 结构关系为 RoleOut -> List[PermissionOut]。
class RoleOut(BaseModel):
    id: int = Field(..., description="角色ID")
    name: str = Field(..., description="角色名称")
    description: Optional[str] = Field(None, description="角色描述")
    is_admin: bool = Field(..., description="是否为管理员角色")
    permissions: List[PermissionOut] = Field(..., description="角色关联的权限列表")
    created_at: datetime = Field(..., description="创建时间")

    model_config = {"from_attributes": True}


# 向角色分配某个对象时使用的简短请求体，目前只需要一个 role_id。
class RoleAssignReq(BaseModel):
    role_id: int = Field(..., description="角色ID")


# 在 RoleOut 的基础上增加知识库权限 ID。继承意味着 RoleWithKbIds 自动拥有
# RoleOut 的全部字段，只额外声明 kb_permission_ids。
class RoleWithKbIds(RoleOut):
    kb_permission_ids: List[int] = []   # 知识库权限关联的 kb_id 列表


# ===== User Management =====
# 用户管理页面使用的响应模型：在 UserOut 类似字段的基础上嵌套角色列表。
# 结构关系为 UserWithRoles -> List[RoleOut] -> List[PermissionOut]。
class UserWithRoles(BaseModel):
    id: int = Field(..., description="用户ID")
    username: str = Field(..., description="用户名")
    email: str = Field(..., description="邮箱")
    is_active: bool = Field(..., description="是否激活")
    created_at: datetime = Field(..., description="创建时间")
    roles: List[RoleOut] = []

    model_config = {"from_attributes": True}


# 批量为用户设置角色时的请求模型，role_ids 中每个整数对应 roles.id。
class UserAssignRoleReq(BaseModel):
    role_ids: List[int]


# ===== KnowledgeBase =====
# 创建知识库请求模型。owner_id、doc_count 和时间字段由当前登录用户及服务端生成。
class KBCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="知识库名称, 1-128个字符")
    description: Optional[str] = None


# 部分更新知识库请求模型；None 通常表示该字段没有在本次请求中修改。
class KBUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128, description="知识库名称, 1-128个字符")
    description: Optional[str] = None


# 知识库响应模型，对应 KnowledgeBase ORM 对象中允许返回给前端的字段。
class KBOut(BaseModel):
    id: int = Field(..., description="知识库ID")
    name: str = Field(..., description="知识库名称")
    description: Optional[str] = Field(None, description="知识库描述")
    owner_id: int = Field(..., description="知识库所有者ID")
    is_public: bool = Field(..., description="是否公开")
    doc_count: int = Field(..., description="文档数量")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    model_config = {"from_attributes": True}


# ===== Document =====
# 文档信息响应模型。它返回的是文件元数据和处理状态，不返回磁盘路径 file_path，
# 也不直接返回 document_chunks 中的大量正文内容。
class DocumentOut(BaseModel):
    id: int = Field(..., description="文档ID")
    kb_id: int = Field(..., description="知识库ID")
    filename: str = Field(..., description="文档文件名")
    file_type: str = Field(..., description="文档文件类型")
    file_size: int = Field(..., description="文档文件大小")
    status: DocumentStatus = Field(..., description="文档状态")
    error_msg: Optional[str] = Field(None, description="文档状态错误信息")
    chunk_count: int = Field(..., description="文档分块数量")
    tags: Optional[str] = Field(None, description="文档标签")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    model_config = {"from_attributes": True}


# ===== Chat =====
# 创建聊天会话的请求模型。kb_id 可以为空，因此允许创建不绑定知识库的普通对话。
class ChatSessionCreate(BaseModel):
    kb_id: Optional[int] = None
    title: Optional[str] = None
    llm_provider: str = "deepseek"
    llm_model: Optional[str] = None


# 聊天会话响应模型，用于把 ChatSession ORM 对象转换为接口数据。
class ChatSessionOut(BaseModel):
    id: int
    kb_id: Optional[int]
    title: Optional[str]
    llm_provider: str
    llm_model: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# 发起一次聊天的请求模型：指定已有会话、用户问题以及是否采用流式返回。
class ChatRequest(BaseModel):
    session_id: int = Field(..., description="聊天会话ID")
    message: str = Field(..., description="用户消息")
    stream: bool = Field(..., description="是否开启流式响应")


# 单条聊天消息响应模型。sources 通常保存 RAG 检索引用来源的序列化内容。
class ChatMessageOut(BaseModel):
    id: int = Field(..., description="聊天消息ID")
    session_id: int = Field(..., description="聊天会话ID")
    role: str = Field(..., description="角色")
    content: str = Field(..., description="消息内容")
    sources: Optional[str] = Field(None, description="引用文档来源")
    created_at: datetime = Field(..., description="创建时间")

    model_config = {"from_attributes": True}


# ===== Search =====
# 检索请求模型：限制 top_k 在 1~20，score_threshold 在 0~1，避免明显无效参数。
# file_type 和 tags 是可选过滤条件。
class SearchRequest(BaseModel):
    kb_id: int = Field(..., description="知识库ID")
    query: str = Field(..., description="搜索查询")
    top_k: int = Field(5, ge=1, le=20, description="返回结果数量")
    score_threshold: float = Field(0.3, ge=0.0, le=1.0, description="分数阈值")
    file_type: Optional[str] = None
    tags: Optional[str] = None


# 单条检索命中结果。它把文档信息、分块位置、正文和最终分数组合在一起。
class SearchResult(BaseModel):
    doc_id: int = Field(..., description="文档ID")
    filename: str = Field(..., description="文档文件名")
    file_type: str = Field(..., description="文档文件类型")
    chunk_index: int = Field(..., description="文档分块索引")
    page_num: Optional[int] = Field(None, description="文档分页索引")
    content: str = Field(..., description="文档内容")
    score: float = Field(..., description="文档分数")
    tags: Optional[str] = Field(None, description="文档标签")


# 检索接口的整体响应，其中 results 是多个 SearchResult。
# 结构关系为 SearchResponse -> List[SearchResult]。
class SearchResponse(BaseModel):
    query: str = Field(..., description="搜索查询")
    results: List[SearchResult] = Field(..., description="搜索结果")
    total: int = Field(..., description="总文档数量")


# ===== ModelConfig =====
# 更新大模型 Provider 配置的请求模型。全部字段均可选，支持部分更新。
# api_key/api_secret 属于敏感信息，路由和日志不应直接输出其原始值。
class ModelConfigUpdate(BaseModel):
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    is_enabled: Optional[bool] = None


# 模型配置响应模型。注释约定 api_key 和 api_secret 返回前要脱敏；
# is_configured/is_available 是面向前端的派生状态，不一定直接来自数据库字段。
class ModelConfigOut(BaseModel):
    id: Optional[int] = None
    provider: str
    api_key: Optional[str] = None      # 脱敏后返回
    api_secret: Optional[str] = None   # 脱敏后返回
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    is_enabled: bool = True
    is_configured: bool = False         # 是否已填写必要凭证
    is_available: bool = False          # 是否通过连通性测试

    model_config = {"from_attributes": True}


# ===== VoiceConfig =====
# 更新语音识别服务配置的请求模型。extra_params 是 Provider 专属 JSON 字符串，
# 例如不同云厂商要求的 AppID、AppKey 或其他参数。
class VoiceConfigUpdate(BaseModel):
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    extra_params: Optional[str] = None    # JSON 字符串，存储 provider 专属参数
    is_enabled: Optional[bool] = None
    is_default: Optional[bool] = None


# 语音配置响应模型，与 ModelConfigOut 一样，敏感凭证应当脱敏后再返回。
class VoiceConfigOut(BaseModel):
    id: Optional[int] = None
    provider: str
    api_key: Optional[str] = None      # 脱敏后返回
    api_secret: Optional[str] = None   # 脱敏后返回
    extra_params: Optional[str] = None
    is_enabled: bool = True
    is_default: bool = False
    is_configured: bool = False

    model_config = {"from_attributes": True}


# ===== 通用响应 =====
# 普通接口统一响应包装：code 表示业务状态，message 是提示文本，data 放实际数据。
# 示例：{"code": 200, "message": "success", "data": {...}}。
class Resp(BaseModel):
    code: int = 200 
    message: str = "success"
    data: Optional[Any] = None


# 分页接口统一响应包装：除了 data，还返回总数、当前页和每页数量。
class PageResp(BaseModel):
    code: int = 200
    message: str = "success"
    data: Optional[Any] = None
    total: int = 0
    page: int = 1
    page_size: int = 20
