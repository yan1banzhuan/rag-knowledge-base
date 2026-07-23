# db 包封装数据存储基础设施。
#
#   session.py       MySQL/SQLAlchemy 异步会话、事务和初始化
#   vector_store.py  ChromaDB 向量集合的增删查操作
#
# 关系数据库保存结构化业务数据，向量数据库保存文本块向量；二者职责不同但会在文档
# 入库、检索和知识库删除流程中协同工作。
