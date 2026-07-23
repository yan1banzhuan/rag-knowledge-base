# services 包是业务服务层，承接路由层和底层工具之间的复杂流程。
#
#   API 路由
#       |
#       v
#   services：文档处理、Embedding、检索、重排、LLM、OCR、语音识别
#       |
#       +--> parsers / models / db / 外部模型服务
#
# 当前文件不批量导出服务，调用方按需导入具体模块，以减少启动副作用和循环依赖。
