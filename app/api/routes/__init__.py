# routes 包按业务领域拆分 FastAPI 路由，例如 auth、kb、docs、chat、user 和 role。
#
# 每个路由文件通常包含：
#
#   APIRouter
#       +--> HTTP 方法和 URL
#       +--> Pydantic 请求/响应模型
#       +--> Depends 身份与权限依赖
#       +--> 对服务层或数据库层的调用
#
# main.py 会逐个导入并 include_router，因此这里不做通配导入，避免循环依赖和隐式副作用。
