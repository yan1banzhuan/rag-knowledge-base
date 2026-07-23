# api 包保存应用的 HTTP 接口层。
#
#   客户端请求
#       |
#       v
#   api/routes/   根据 URL 选择路由函数
#       |
#       +--> api/deps.py  注入数据库、恢复用户、检查权限
#       |
#       v
#   services/db   执行业务与数据操作
#
# 当前文件保持轻量，不统一导入全部路由，具体路由由 main.py 显式注册。
