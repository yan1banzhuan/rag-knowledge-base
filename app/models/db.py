# =============================================================================
# 文件作用与架构位置（零基础导读）
# =============================================================================
# 这个文件是项目的“数据库模型层（Model 层）”。它使用 SQLAlchemy ORM，把
# Python 类映射成关系型数据库中的表，把类属性映射成表中的字段。
#
# ORM 是 Object Relational Mapping（对象关系映射）的缩写。使用 ORM 后，业务代码
# 可以操作 User、Document 这样的 Python 对象，而不必为每次查询都手写 SQL。
#
# 本文件在典型分层架构中的位置如下：
#
#   +---------------------------+
#   | API / Router              |  接收 HTTP 请求
#   +-------------+-------------+
#                 |
#                 v
#   +---------------------------+
#   | Service / Business Logic  |  执行业务规则
#   +-------------+-------------+
#                 |
#                 v
#   +---------------------------+
#   | Models: this db.py        |  定义数据结构和表之间的关系
#   +-------------+-------------+
#                 |
#                 v
#   +---------------------------+
#   | SQLAlchemy ORM            |  把对象操作转换成 SQL
#   +-------------+-------------+
#                 |
#                 v
#   +---------------------------+
#   | MySQL / other SQL DB      |  持久化保存数据
#   +---------------------------+
#
# RAG 相关的主要数据流程：
#
#   User
#     |
#     v
#   KnowledgeBase
#     |
#     v
#   Document --> DocumentChunk --> Chroma vector record
#
# 问答相关的主要数据流程：
#
#   User --> ChatSession --> ChatMessage
#                 |
#                 +--> KnowledgeBase (可选，kb_id 可以为空)
#
# 权限相关的主要数据流程：
#
#   User -- user_roles --> Role -- role_permissions --> Permission
#
# 本文件中“自行定义的函数/方法”只有一个：
#   User.is_admin：检查用户拥有的角色中，是否至少有一个管理员角色。
# `func.now()` 是 SQLAlchemy 提供的数据库函数调用器，不是本文件定义的函数；
# `relationship()` 和 `Column()` 也都是 SQLAlchemy 提供的声明工具。
#
# 本文件为什么必须存在：
#   1. 集中规定数据库里有哪些表、每张表有哪些字段。
#   2. 规定主键、外键、唯一性、是否允许为空和默认值等约束。
#   3. 规定 User.roles、Document.chunks 等对象之间的访问关系。
#   4. 为建表、迁移、增删改查和业务层开发提供统一的数据结构依据。
#
# 阅读 Column 时常见参数的含义：
#   primary_key=True   当前字段是主键，可以唯一标识一行数据。
#   autoincrement=True 新增数据时由数据库自动生成递增整数。
#   nullable=False     该字段不允许保存 NULL（“没有值”）。
#   unique=True        整张表中该字段的值不能重复。
#   index=True         为该字段建立索引，以空间和写入成本换取更快的查询。
#   default=...        SQLAlchemy 侧默认值，通常在 ORM 发出 INSERT 时填入。
#   server_default=... 数据库服务器侧默认值，即使直接执行 SQL 也可生效。
#   onupdate=...       ORM 更新该行时生成新的值。
#   ondelete=...       被引用记录删除时，数据库如何处理当前外键记录。
#
# 注意：relationship() 主要建立 Python 对象间的导航关系；ForeignKey() 才是在
# 数据库字段层建立外键约束。两者用途不同，但通常配合使用。
# =============================================================================

# 从 SQLAlchemy 导入“字段类型、约束工具和数据库函数工具”。
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean,
    ForeignKey, BigInteger, Enum, func
)
# 导入 ORM 基类工厂和模型关系声明工具。
from sqlalchemy.orm import declarative_base, relationship
# 导入 Python 标准库枚举模块，用于定义有限且明确的状态集合。
import enum

# 创建所有 ORM 模型共同继承的基类。
# SQLAlchemy 会通过这个 Base 收集模型的表名、字段和关系等元数据。
Base = declarative_base()


# =============================================================================
# Permission：权限表
# 一行数据代表一个可分配的系统权限，例如“知识库管理”或“问答对话”。
# =============================================================================
class Permission(Base):
    """权限表：定义所有权限"""
    __tablename__ = "permissions"

    # id 是数据库内部使用的唯一编号；用户通常看到的是 code 或 name。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # code 适合在程序中判断权限；unique 防止两个权限使用相同编码。
    code = Column(String(64), unique=True, nullable=False, index=True)   # 权限编码，如 kb_read
    # name 是给人看的权限名称，所以不要求像 code 一样作为程序标识符。
    name = Column(String(128), nullable=False)                          # 权限名称，如 "查看知识库"
    # type 用于进一步区分权限类别；未显式赋值时，ORM 默认使用 "menu"。
    type = Column(String(32), nullable=False, default="menu")           # menu=菜单权限, kb=知识库权限
    # 创建权限记录时，由数据库使用当前时间填充 created_at。
    created_at = Column(DateTime, server_default=func.now())

    '''
    在数据库中， Permission （权限）和 Role （角色）是典型的 多对多关系 ：
    - 一个角色可以拥有多个权限
    - 一个权限可以被多个角色拥有
    relationship 就是用来在 ORM 层面表达这种关系，让你可以通过对象属性直接访问关联数据。
    secondary="role_permissions" 多对多关系的中间表名称（存储角色和权限的关联关系）
    back_populates="permissions" 指定反向关联的属性名（Role 类中定义的 permissions 属性）
    '''
    # 这是 ORM 关系属性，不是 permissions 表中的实际字段。
    # 读取 permission.roles 时，SQLAlchemy 会借助 role_permissions 中间表查找角色。
    roles = relationship("Role", secondary="role_permissions", back_populates="permissions")


# =============================================================================
# Role：角色表
# 角色是权限的集合。把角色分配给用户，比逐个给用户分配权限更容易管理。
# =============================================================================
class Role(Base):
    """角色表"""
    __tablename__ = "roles"

    # 角色自身的唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 角色名称必须唯一，例如 admin、editor、viewer。
    name = Column(String(64), unique=True, nullable=False, index=True)
    # 可选的说明文字；nullable=True 表示允许没有描述。
    description = Column(String(255), nullable=True)
    # 这是角色级管理员标记，不是用户表中的字段。
    is_admin = Column(Boolean, default=False)     # 超级管理员角色（拥有所有权限）
    # created_at 记录首次创建时间；updated_at 记录最近一次 ORM 更新时间。
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # users 通过 user_roles 找到拥有当前角色的用户。
    users = relationship("User", secondary="user_roles", back_populates="roles")
    # permissions 通过 role_permissions 找到当前角色拥有的全部权限。
    permissions = relationship("Permission", secondary="role_permissions", back_populates="roles")


# =============================================================================
# UserRole：用户与角色的多对多关联表
#
#   users.id <--- user_id | user_roles | role_id ---> roles.id
#
# 每一行只表达“某个用户拥有某个角色”这一件事。
# =============================================================================
class UserRole(Base):
    """用户角色关联表"""
    __tablename__ = "user_roles"

    # 关联记录自身的主键。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # user_id 必须指向 users 表中存在的 id。
    # CASCADE 表示删除用户后，由数据库删除相应的用户-角色关联记录。
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # role_id 必须指向 roles 表中存在的 id；删除角色时也清理关联记录。
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    # 记录角色是什么时候分配给用户的。
    created_at = Column(DateTime, server_default=func.now())

    # __table_args__ 用于填写表级设置或表级约束，而不是某一个字段的设置。
    __table_args__ = (
        # 联合唯一约束：同一用户不能重复关联同一角色
        # SQLAlchemy 2.0 联合唯一约束（MySQL 语法）
        # 当前实际代码只设置了 MySQL 引擎和字符集，没有真正声明联合唯一约束。
        # 因此，仅从模型定义看，同一个 user_id 和 role_id 组合仍可能被重复插入。
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


# =============================================================================
# RolePermission：角色与权限的多对多关联表
#
#   roles.id <--- role_id | role_permissions | permission_id ---> permissions.id
# =============================================================================
class RolePermission(Base):
    """角色权限关联表"""
    __tablename__ = "role_permissions"

    # 关联记录自身的主键。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 指向角色；删除角色时级联清理相应关联。
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    # 指向权限；删除权限时级联清理相应关联。
    permission_id = Column(Integer, ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False)
    # 记录该权限被分配给角色的时间。
    created_at = Column(DateTime, server_default=func.now())

    # 与 UserRole 一样，这里是表级设置。
    __table_args__ = (
        # InnoDB 支持事务和外键；utf8mb4 可以完整保存中文和 Emoji。
        # 这里也没有限制 role_id + permission_id 组合必须唯一。
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


# ===== Permission constants =====
# =============================================================================
# PermissionCode：权限编码常量
# 这个类没有继承 Base，所以不会生成数据库表。它只是把常用字符串集中命名，
# 防止业务代码到处手写 "chat"、"user_manage" 时发生拼写错误。
#
# 示例：PermissionCode.CHAT 的值就是字符串 "chat"。
# =============================================================================
class PermissionCode:
    # 知识库管理权限；当前设计把多项知识库操作归入同一个权限编码。
    KB_MANAGE = "kb_manage"        # 知识库管理（包含查看/创建/编辑/删除/上传/删除全部）
    # 使用问答对话功能的权限。
    CHAT = "chat"                  # 问答对话
    # 使用检索功能的权限。
    SEARCH = "search"              # 检索
    # 查看统计信息的权限。
    STATS = "stats"               # 统计
    # 修改大模型配置的权限。
    MODEL_CONFIG = "model_config"     # 模型配置
    # 修改语音服务配置的权限。
    VOICE_CONFIG = "voice_config"     # 语音配置
    # 管理系统用户的权限。
    USER_MANAGE = "user_manage"       # 用户管理
    # 管理角色及角色权限的权限。
    ROLE_MANAGE = "role_manage"       # 角色管理


# =============================================================================
# MenuItem：前端菜单标识常量
# 该类同样不是数据库模型。后端可根据用户权限筛选这些菜单标识，再交给前端显示。
#
#   User roles --> Permission codes --> Allowed menu items --> Frontend
# =============================================================================
class MenuItem:
    """菜单项定义，供前端和权限过滤使用"""
    # 仪表盘菜单。
    MENU_DASHBOARD = "dashboard"
    # 知识库菜单。
    MENU_KB = "kb"
    # 问答聊天菜单。
    MENU_CHAT = "chat"
    # 模型配置菜单。
    MENU_MODELS = "models"
    # 语音配置菜单。
    MENU_VOICE = "voice"
    # 用户管理菜单。
    MENU_USERS = "users"
    # 角色管理菜单。
    MENU_ROLES = "roles"


# =============================================================================
# User：用户表
# 一行数据代表一个可登录系统的用户。User 是权限、知识库和聊天记录的起点。
#
#   User --many-to-many--> Role
#     |
#     +--one-to-many-----> KnowledgeBase
#     |
#     +--one-to-many-----> ChatSession
# =============================================================================
class User(Base):
    # ORM 将这个类映射到数据库中的 users 表。
    __tablename__ = "users"

    # 用户的内部唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 登录用户名必须唯一、不能为空，并建立索引以加快按用户名查找。
    username = Column(String(64), unique=True, nullable=False, index=True)
    # 邮箱必须唯一且不能为空；当前字段没有显式 index=True。
    email = Column(String(128), unique=True, nullable=False)
    # 这里只应保存经过安全哈希算法处理后的密码，不能保存用户的明文密码。
    hashed_password = Column(String(255), nullable=False)
    # 表示账号能否正常使用；新用户默认启用。
    is_active = Column(Boolean, default=True)
    # 记录账号创建和最近更新时间。
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # roles 是多对多关系：一个用户可有多个角色，一个角色也可属于多个用户。
    roles = relationship("Role", secondary="user_roles", back_populates="users")
    # 用户是一方、知识库是多方。back_populates 与 KnowledgeBase.owner 双向对应。
    # all 表示常用 ORM 操作会传播到子对象；delete-orphan 表示脱离父对象的子记录可被删除。
    knowledge_bases = relationship("KnowledgeBase", back_populates="owner", cascade="all, delete-orphan")
    # 一个用户可以建立多个聊天会话；删除/孤立会话时按相同 ORM 级联规则处理。
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")

    # @property 把下面的方法包装为“只读属性”。调用时写 user.is_admin，末尾不加括号。
    @property
    # 这是本文件唯一自行定义的方法，返回值类型标注为 bool（True 或 False）。
    def is_admin(self) -> bool:
        """判断用户是否为超级管理员（拥有 is_admin=True 的角色）"""
        # 如果 roles 是空列表或其他空值，说明用户没有角色，因此不可能是管理员。
        if not self.roles:
            return False
        # 生成器表达式逐个读取角色的 is_admin 字段；any() 遇到任意 True 就返回 True。
        #
        #   no roles? --yes--> False
        #       |
        #       no
        #       v
        #   any role.is_admin == True? --yes--> True
        #                  |
        #                  no
        #                  v
        #                False
        return any(r.is_admin for r in self.roles)


# =============================================================================
# KnowledgeBase：知识库表
# 知识库是文档的逻辑容器，并且属于某一个用户。
# =============================================================================
class KnowledgeBase(Base):
    # 映射到 knowledge_bases 表。
    __tablename__ = "knowledge_bases"

    # 知识库的唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 给用户显示的知识库名称，不能为空。
    name = Column(String(128), nullable=False)
    # 可选的长文本说明；Text 比固定长度 String 更适合较长内容。
    description = Column(Text, nullable=True)
    # 外键指向 users.id，说明谁拥有该知识库；不能为空，所以知识库必须有所有者。
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # 是否允许其他用户访问；默认是私有知识库。
    is_public = Column(Boolean, default=False)
    # 文档数量缓存，方便快速展示；上传或删除文档时需要由业务逻辑同步维护。
    doc_count = Column(Integer, default=0)
    # 创建时间与更新时间。
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # owner 让代码可以用 knowledge_base.owner 取得 User 对象。
    owner = relationship("User", back_populates="knowledge_bases")
    # documents 让代码取得知识库里的全部文档；知识库是父对象，文档是子对象。
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan")


# =============================================================================
# DocumentStatus：文档处理状态枚举
# 它不是数据库表。枚举把状态限制为四种明确取值，避免任意字符串导致状态混乱。
#
#   pending --> processing --> completed
#                   |
#                   +--------> failed
# =============================================================================
class DocumentStatus(str, enum.Enum):
    # 已登记文档，但后台任务尚未开始处理。
    pending = "pending"
    # 正在读取文件、切分文本或生成向量。
    processing = "processing"
    # 文档已经成功处理完成。
    completed = "completed"
    # 处理过程中发生错误；错误原因通常写入 Document.error_msg。
    failed = "failed"


# =============================================================================
# Document：文档表
# 一行数据保存一个上传文件的元信息和处理进度，而不是把整个原始文件存入此表。
# =============================================================================
class Document(Base):
    # 映射到 documents 表。
    __tablename__ = "documents"

    # 文档的唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 文档所属知识库。删除知识库时，数据库通过 CASCADE 删除相关文档记录。
    kb_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    # 用户上传时的文件名。
    filename = Column(String(255), nullable=False)
    # 文件在磁盘、对象存储或其他介质中的位置；它不是文件内容本身。
    file_path = Column(String(512), nullable=False)
    # 文件类型，例如 pdf、txt、docx，用于选择对应解析方式。
    file_type = Column(String(32), nullable=False)
    # 文件字节数可能很大，因此使用 BigInteger；未统计时默认 0。
    file_size = Column(BigInteger, default=0)
    # 使用前面的 DocumentStatus 枚举；新文档默认进入 pending 状态。
    status = Column(Enum(DocumentStatus), default=DocumentStatus.pending)
    # 处理失败时保存错误详情；成功或尚未出错时允许为 NULL。
    error_msg = Column(Text, nullable=True)
    # 缓存文档被切分出的文本块数量，需要由文档处理流程维护。
    chunk_count = Column(Integer, default=0)
    # 可选标签。当前字段是字符串，标签格式（例如 JSON 或逗号分隔）需由业务层约定。
    tags = Column(String(512), nullable=True)
    # 文档记录创建和最近更新时间。
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # 从文档对象导航回所属知识库。
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    # 从文档取得所有文本块；文档被删除或文本块成为孤儿时执行 ORM 级联处理。
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


# =============================================================================
# DocumentChunk：文档文本分块表
# RAG 通常不会把整篇长文直接交给模型，而是先切成较小文本块，再分别生成向量。
# 关系型数据库保存文本、页码等元数据；chroma_id 连接 Chroma 中的向量记录。
#
#   Uploaded file
#        |
#        v
#   Parse document
#        |
#        v
#   Split into chunks
#        |
#        +--> DocumentChunk row (text and metadata)
#        |
#        +--> Embedding model --> Chroma vector (identified by chroma_id)
#
# 用户提问后的检索通常反向进行：
#
#   Question --> Embedding --> Chroma similarity search --> chroma_id
#                                                       |
#                                                       v
#                                                DocumentChunk.content
#                                                       |
#                                                       v
#                                               LLM generates answer
# =============================================================================
class DocumentChunk(Base):
    # 映射到 document_chunks 表。
    __tablename__ = "document_chunks"

    # 文本块自身的唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 指向来源文档；删除来源文档时，由数据库级联删除相应分块。
    doc_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    # 冗余保存知识库 ID，方便直接按知识库过滤并通过索引加速查询。
    # 注意：这个字段没有 ForeignKey，因此数据库本身不会检查对应知识库是否存在。
    kb_id = Column(Integer, nullable=False, index=True)
    # Chroma 向量记录的唯一 ID，用来连接 SQL 数据与向量数据。
    chroma_id = Column(String(128), nullable=False, unique=True)
    # 当前文本块的实际文字内容，是生成回答时提供给大模型的上下文候选。
    content = Column(Text, nullable=False)
    # 文本块在原文档中的顺序编号；默认从业务约定的 0 开始。
    chunk_index = Column(Integer, default=0)
    # 文本所在页码；纯文本等没有页码的文件可以保存 NULL。
    page_num = Column(Integer, nullable=True)
    # 当前分块的 Token 数缓存，可用于控制提示词长度和模型费用。
    token_count = Column(Integer, default=0)
    # 文本块记录的创建时间。
    created_at = Column(DateTime, server_default=func.now())

    # 通过 chunk.document 可以读取来源 Document；与 Document.chunks 双向对应。
    document = relationship("Document", back_populates="chunks")


# =============================================================================
# ChatSession：聊天会话表
# 会话把同一次连续问答中的多条消息组织到一起，并记录所用知识库和模型。
# =============================================================================
class ChatSession(Base):
    # 映射到 chat_sessions 表。
    __tablename__ = "chat_sessions"

    # 会话的唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 会话属于哪个用户；删除用户后，数据库级联删除其会话。
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 可选的知识库外键。删除知识库时保留历史会话，但把 kb_id 设置成 NULL。
    kb_id = Column(Integer, ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    # 会话标题，可以由用户填写或根据首条问题自动生成。
    title = Column(String(255), nullable=True)
    # 记录本会话选择的模型服务商；未指定时默认使用 deepseek。
    llm_provider = Column(String(32), default="deepseek")
    # 记录具体模型名；允许为空，此时业务层可采用服务商的默认模型。
    llm_model = Column(String(64), nullable=True)
    # 会话创建时间与最近更新时间。
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # session.user 取得会话所属 User；与 User.chat_sessions 双向对应。
    user = relationship("User", back_populates="chat_sessions")
    # session.messages 取得会话消息；删除会话时在 ORM 层删除其所有消息。
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")


# =============================================================================
# ChatMessage：聊天消息表
# 一行代表会话中的一条发言，多行按创建时间共同形成完整聊天历史。
#
#   ChatSession
#       |
#       +--> user message
#       +--> assistant message + retrieved sources
#       +--> user message
#       +--> assistant message + retrieved sources
# =============================================================================
class ChatMessage(Base):
    # 映射到 chat_messages 表。
    __tablename__ = "chat_messages"

    # 消息的唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 消息属于哪个会话；删除会话时，由数据库级联删除消息。
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    # 消息发送者角色，常见值是 user、assistant 或 system。
    # 当前代码使用 String 而不是 Enum，因此允许值需要由业务层校验。
    role = Column(String(16), nullable=False)
    # 消息正文，不能为空。
    content = Column(Text, nullable=False)
    # 可选的引用来源，常用于保存 RAG 检索命中的文档/分块信息。
    # 当前类型是 Text；若保存 JSON，需要由写入和读取代码负责序列化与反序列化。
    sources = Column(Text, nullable=True)
    # 消息创建时间，可用于恢复消息顺序。
    created_at = Column(DateTime, server_default=func.now())

    # message.session 取得所属会话；与 ChatSession.messages 双向对应。
    session = relationship("ChatSession", back_populates="messages")


# =============================================================================
# ModelConfig：大语言模型服务配置表
# 它把不同 Provider 的连接信息保存在数据库中，让管理员可以在运行期间修改配置。
# 注释说明其优先级高于 .env，但真正的优先级逻辑应由读取配置的业务代码实现。
#
#   Admin configuration
#          |
#          v
#   model_configs row
#          |
#          v
#   Provider client --> OpenAI / DeepSeek / DashScope / Qianfan / Ollama
# =============================================================================
class ModelConfig(Base):
    """模型配置表：存储各 Provider 的 API Key 等信息，优先级高于 .env 配置"""
    # 映射到 model_configs 表。
    __tablename__ = "model_configs"

    # 配置记录的唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 服务商标识必须唯一，意味着每个 provider 最多只有一条配置记录。
    provider = Column(String(32), unique=True, nullable=False)  # openai/deepseek/dashscope/qianfan/ollama
    # 主密钥；不同服务商对这个字段的解释可能不同。
    # 这是敏感信息，生产环境应考虑加密存储、日志脱敏和严格访问控制。
    api_key = Column(String(512), nullable=True)       # 主密钥（qianfan 用作 access_key）
    # 辅助密钥，当前注释约定主要用于千帆 secret_key；同样属于敏感信息。
    api_secret = Column(String(512), nullable=True)    # 仅 qianfan secret_key
    # 自定义 API 根地址，可用于兼容接口、代理服务或本地部署服务。
    base_url = Column(String(256), nullable=True)      # 自定义 API 地址
    # 未在会话中明确指定模型时可使用的默认模型名。
    model_name = Column(String(128), nullable=True)    # 默认模型名
    # 控制该 Provider 配置是否允许被业务层选用。
    is_enabled = Column(Boolean, default=True)
    # 配置创建时间与最近更新时间。
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# =============================================================================
# VoiceProvider：语音服务商枚举
# 这不是数据库表，用于集中表达当前程序已知的语音服务商。
# 注意：下面 VoiceConfig.provider 目前仍是普通 String，并未直接使用此枚举约束字段。
# =============================================================================
class VoiceProvider(str, enum.Enum):
    # 百度语音服务。
    baidu = "baidu"
    # 阿里云语音服务。
    aliyun = "aliyun"


# =============================================================================
# VoiceConfig：语音识别服务配置表
# 保存各语音 Provider 的访问凭证和专属参数，供语音识别相关业务代码读取。
#
#   Audio input --> Voice service client --> Provider API --> Recognized text
#                         |
#                         +--> reads VoiceConfig
# =============================================================================
class VoiceConfig(Base):
    """语音识别配置表：存储各 Provider 的 API 凭证"""
    # 映射到 voice_configs 表。
    __tablename__ = "voice_configs"

    # 配置记录的唯一编号。
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 服务商名称必须唯一；当前字段是 String，业务层仍需验证是否为受支持的值。
    provider = Column(String(32), unique=True, nullable=False)  # baidu / aliyun
    # 主身份凭证；百度与阿里对该字段的含义不同。
    # 这是敏感信息，应避免明文输出到日志或接口响应。
    api_key = Column(String(512), nullable=True)       # 百度 AppID / 阿里 AccessKey ID
    # 私密凭证，也应考虑加密存储和最小权限访问。
    api_secret = Column(String(512), nullable=True)    # 百度 API Key / 阿里 AccessKey Secret
    # 用 Text 保存 JSON 字符串形式的 Provider 专属参数。
    # 数据库不会自动保证内容是合法 JSON，校验和解析应由业务层完成。
    extra_params = Column(Text, nullable=True)         # JSON，存储 provider 专属参数
    # 配置是否启用；业务层选择服务商时应过滤掉 False 的记录。
    is_enabled = Column(Boolean, default=True)
    # 标记默认 Provider。当前模型没有数据库约束保证全表只能有一个 True。
    is_default = Column(Boolean, default=False)        # 是否为默认 Provider
    # 配置创建时间与最近更新时间。
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
