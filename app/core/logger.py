# =============================================================================
# 文件作用与架构位置
# =============================================================================
# 本文件集中配置项目日志，并向其他模块导出同一个 Loguru logger。
#
#   业务代码 logger.info/debug/error(...)
#                  |
#                  v
#           本文件配置的 logger
#             |             |
#             v             v
#          控制台          按天日志文件
#          INFO+           DEBUG+，保留 30 天
#
# 本文件没有自定义函数；所有语句在模块第一次导入时执行一次。集中配置可以避免每个
# 模块自行设置格式、级别和文件路径，确保日志风格一致。
# =============================================================================

# sys.stdout 是标准输出，适合 Docker、终端和进程管理器收集日志。
import sys
# Loguru 提供开箱即用的结构化格式、文件轮转和更简单的 logger API。
from loguru import logger

# 删除 Loguru 默认处理器，避免默认输出和下面自定义输出重复打印同一条日志。
logger.remove()
# 添加控制台输出处理器。
logger.add(
    sys.stdout,
    # format 中的占位符由 Loguru 替换；尖括号标签负责终端颜色。
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    # 控制台只显示 INFO、WARNING、ERROR 等，不显示更详细的 DEBUG。
    level="INFO",
)
# 添加文件输出处理器，供排查历史问题和保留 DEBUG 细节。
logger.add(
    # {time:YYYY-MM-DD} 会生成按日期命名的文件，例如 logs/rag_2026-07-21.log。
    "logs/rag_{time:YYYY-MM-DD}.log",
    # 每天零点关闭旧文件并创建新文件。
    rotation="00:00",
    # 自动清理 30 天以前的日志，避免无限占用磁盘。
    retention="30 days",
    encoding="utf-8",
    level="DEBUG",
)

# __all__ 规定 `from app.core.logger import *` 时只导出 logger。
__all__ = ["logger"]
