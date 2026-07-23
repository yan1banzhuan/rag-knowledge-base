from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import settings

# =============================================================================
# 文件作用与架构位置（零基础导读）
# =============================================================================
# 本文件是项目的“基础安全工具层”，集中处理两类安全操作：
#
#   1. 密码哈希与校验：bcrypt + passlib；
#   2. 登录令牌签发与解析：JWT + python-jose。
#
# 它不负责查询数据库，也不直接接收 HTTP 请求。认证路由 auth.py 调用这里的函数
# 校验密码和创建 Token；认证依赖 deps.py 调用这里的函数解码 Token。
#
# 架构位置和调用关系：
#
#   Registration route
#          |
#          +--> get_password_hash(password) --> hashed password --> users table
#
#   Login route
#          |
#          +--> verify_password(plain, hash)
#          |
#          +--> create_access_token({"sub": user_id}) --> JWT returned to client
#
#   Protected API request
#          |
#          +--> deps.py --> decode_access_token(token)
#                             |
#                             +--> payload containing sub / exp
#
# 本文件共有 4 个函数：
#
#   verify_password()      比较明文密码和已有哈希；
#   get_password_hash()    把明文密码转换成不可逆哈希；
#   create_access_token()  生成带过期时间和签名的 JWT；
#   decode_access_token()  验证 JWT 签名/有效期并返回载荷，失败返回 None。
#
# 重要安全概念：
#
#   - 密码“哈希”不是加密。正确设计是不需要还原原密码，只验证是否匹配；
#   - JWT 默认是“签名”而不是“加密”，载荷可以被客户端解码查看，因此不要把密码、
#     API Secret 等敏感信息放入 JWT；
#   - SECRET_KEY 用于签名和验证签名，必须保密且应在生产环境使用足够随机的值；
#   - exp 是令牌过期时间，python-jose 解码时会验证它。
# =============================================================================

# CryptContext 封装密码算法细节。schemes=["bcrypt"] 表示新密码采用 bcrypt；
# deprecated="auto" 允许 passlib 识别未来被标记为旧算法的哈希并协助平滑迁移。
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT 使用 HMAC-SHA256。HS256 是对称签名算法：签发方和验证方使用同一个 SECRET_KEY。
ALGORITHM = "HS256"

#验证用户输入的密码是否正确
#输入：用户输入的密码(明文)、数据库中存储的密码哈希值
#输出：布尔值，表示密码是否匹配
#
# 校验流程：
#
#   plain_password + hashed_password
#                 |
#                 v
#       passlib reads salt/cost from hash
#                 |
#                 v
#       hash the supplied plaintext again
#                 |
#                 v
#          values match? -> True / False
#
# bcrypt 哈希通常自带随机盐，因此同一个明文密码多次生成的哈希可能不同，这是正常现象。
def verify_password(plain_password: str, hashed_password: str) -> bool:
    # verify 会识别 hashed_password 的格式并安全比较，不要用普通字符串 == 替代它。
    return pwd_context.verify(plain_password, hashed_password)

#生成密码的哈希值
#输入：用户输入的密码(明文)
#输出：密码的哈希值
# 注册和重置密码时调用。返回结果适合存入 users.hashed_password，原始 password 不应
# 写入数据库、日志、JWT 或接口响应。
def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

#创建访问令牌
#输入：包含用户信息的字典(例如：{"sub": str(user.id)})，可选的过期时间
#输出：访问令牌字符串
#
# 创建流程：
#
#   input data --copy--> to_encode
#                         |
#                         +--> add exp expiration time
#                         |
#                         v
#                jwt.encode(payload, SECRET_KEY, HS256)
#                         |
#                         v
#                  signed token string
#
# data.copy() 很重要：函数会向副本增加 exp，而不会修改调用者传入的原字典。
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    # 计算过期时间  datetime.utcnow() 返回当前时间的 UTC 时间
    expire = datetime.utcnow() + (
        # 如果提供了过期时间，就使用它，否则，就使用默认的过期时间(7天)
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    # update 会新增 exp；如果输入数据本身带有 exp，这里会用计算值覆盖它。
    to_encode.update({"exp": expire})
    # JWT 通常由 header.payload.signature 三部分组成；签名可检测内容是否被篡改。
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)

#解码访问令牌
#输入：访问令牌字符串
#输出：包含用户信息的字典(例如：{"sub": str(user.id)})，如果令牌无效，返回None
#
# 解码流程：
#
#   token
#     |
#     v
#   verify signature with SECRET_KEY and allowed HS256
#     |
#     +--> valid and not expired --> return payload dict
#     |
#     +--> invalid/expired/tampered --> JWTError --> return None
#
# algorithms=[ALGORITHM] 明确限制允许的算法，避免接受攻击者指定的其他算法。
def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        # 安全工具层把各种 JWT 错误统一转换成 None；deps.py 再将其转换为 HTTP 401。
        return None
