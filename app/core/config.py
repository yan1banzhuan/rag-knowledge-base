# =============================================================================
# 文件作用与架构位置（零基础导读）
# =============================================================================
# 本文件是整个后端的“配置中心”。其他模块不需要分别读取环境变量，而是统一导入
# settings，从中取得数据库地址、缓存开关、模型名称、上传限制和检索参数。
#
# 配置来源和使用流程：
#
#   类字段中的默认值
#          +
#   项目根目录 .env / 系统环境变量（同名配置会覆盖默认值）
#          |
#          v
#   Settings() 由 Pydantic 校验并转换类型
#          |
#          v
#   全局 settings 对象
#          |
#          +--> db/session.py        数据库连接
#          +--> redis_client.py      Redis 连接
#          +--> security.py          JWT 密钥和有效期
#          +--> services/*           模型、检索和文件参数
#
# 本文件有 1 个配置类和 3 个只读属性方法：
#
#   Settings.cors_origins_list  把逗号分隔的 CORS 字符串转换为列表
#   Settings.DATABASE_URL       生成 SQLAlchemy 异步 MySQL URL
#   Settings.DATABASE_URL_SYNC  生成同步 MySQL URL，供同步工具或迁移场景使用
#
# 文件末尾会创建 settings，并确保上传目录和 ChromaDB 目录存在。因此导入此模块不仅
# 定义类型，也会进行一次轻量的配置初始化和目录检查。
# =============================================================================

# BaseSettings 自动读取环境变量和 .env；field_validator 当前未使用，保留为配置校验扩展。
from pydantic_settings import BaseSettings
from pydantic import field_validator
# Optional[str] 表示 API Key 等配置允许没有设置，即值可以是字符串或 None。
from typing import Optional
# os 用于在启动时创建上传和向量数据库目录。
import os


# Settings 中的类型标注同时承担默认值、类型转换和配置文档三种作用。
class Settings(BaseSettings):
    # 应用
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-secret-key-please-change"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7天

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        # 例如 "http://a.com, http://b.com" -> ["http://a.com", "http://b.com"]。
        # strip() 去掉每项两侧空格；最后的 if 会忽略连续逗号形成的空项。
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # 速率限制
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_DEFAULT: str = "60/minute"
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_UPLOAD: str = "20/minute"

    # Redis
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_ENABLED: bool = True
    BM25_CACHE_TTL: int = 3600
    PROVIDER_CACHE_TTL: int = 300
    PERMISSION_CACHE_TTL: int = 600

    # MySQL
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = ""
    MYSQL_DATABASE: str = "rag_system"

    #@property 是 Python 的内置装饰器，用于将类的 方法转换为属性 访问，而不需要调用方法
    # 例如：settings.DATABASE_URL 而不是 settings.DATABASE_URL()
    @property
    def DATABASE_URL(self) -> str:
        # aiomysql 是异步驱动，与项目中的 AsyncSession 配套使用。
        # 多个相邻 f-string 会由 Python 自动拼接成一个完整字符串。
        return (
            f"mysql+aiomysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
            f"?charset=utf8mb4"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:
        # pymysql 是同步驱动。该 URL 与上面的数据库参数相同，仅驱动名称不同。
        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
            f"?charset=utf8mb4"
        )

    # ChromaDB
    CHROMA_PERSIST_DIR: str = "./chroma_db"

    # 文件上传
    UPLOAD_DIR: str = "./uploads"
    MAX_FILE_SIZE_MB: int = 100
    # 图片文件上传
    MAX_IMAGE_SIZE_MB: int = 10
    ALLOWED_IMAGE_TYPES: str = "png,jpg,jpeg,gif,bmp,webp"
    # 语音文件上传
    VOICE_ENABLED: bool = False
    MAX_AUDIO_SIZE_MB: int = 30
    ALLOWED_AUDIO_TYPES: str = "mp3,wav,m4a,aac,ogg,wma,flac,pcm"

    # Embedding
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    # 推理设备（仅支持 CPU）
    EMBEDDING_DEVICE: str = "cpu"

    # 大模型
    DEFAULT_LLM_PROVIDER: str = "openai"

    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: str = "https://xiaoai.com/v1"
    OPENAI_MODEL: str = "gpt-4o"

    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-v4-flash"

    DASHSCOPE_API_KEY: Optional[str] = None
    DASHSCOPE_MODEL: str = "qwen-max"

    QIANFAN_ACCESS_KEY: Optional[str] = None
    QIANFAN_SECRET_KEY: Optional[str] = None
    QIANFAN_MODEL: str = "ERNIE-4.0-8K"

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b"

    # LM Studio
    LMSTUDIO_BASE_URL: str = "http://localhost:1234/v1"
    LMSTUDIO_MODEL: str = ""

    # 检索
    RETRIEVAL_TOP_K: int = 5
    RETRIEVAL_SCORE_THRESHOLD: float = 0.3
    VECTOR_WEIGHT: float = 0.7
    BM25_WEIGHT: float = 0.3
    RERANK_ENABLED: bool = True
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"
    RERANK_MULTIPLIER: int = 4
    RERANK_TOP_K: int = 5

    # 分块
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 80

    # env_file 指定配置文件；extra="ignore" 表示 .env 中出现未在 Settings 声明的键时忽略，
    # 而不是让应用因为无关环境变量而启动失败。
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


# 模块首次导入时创建全局唯一配置对象。后续 `from app.core.config import settings` 得到的
# 都是这个对象，不需要反复读取和解析 .env。
settings = Settings()

# 确保必要目录存在
# exist_ok=True 表示目录已存在时不报错；父目录不存在时 os.makedirs 会一并创建。
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.CHROMA_PERSIST_DIR, exist_ok=True)
