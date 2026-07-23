# app 是后端应用的顶层 Python 包。
#
# 目录关系：
#
#   app
#   +-- api/       HTTP 路由与依赖注入
#   +-- core/      配置、安全、日志、Redis 等基础设施
#   +-- db/        数据库会话和向量数据库访问
#   +-- models/    SQLAlchemy 数据模型与 Pydantic Schema
#   +-- parsers/   PDF、Word、图片等文件解析器
#   +-- services/  文档处理、检索、模型调用等业务服务
#
# __init__.py 让 Python 把 app 识别为包。当前不在这里导出对象，可以避免导入 app 时
# 提前初始化数据库、模型或第三方服务。
