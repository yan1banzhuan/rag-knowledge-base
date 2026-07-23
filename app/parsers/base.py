# =============================================================================
# 文件作用与架构位置（所有解析器共同遵守的数据协议）
# =============================================================================
# 不同文件库返回的数据形态各不相同。本文件先定义统一的结果结构，再规定所有解析器
# 必须提供 parse(file_path) 方法，使文档服务不需要关心具体文件格式。
#
# 类和方法关系：
#
#   BaseParser（抽象父类）
#       +--> parse()       子类必须实现
#       +--> clean_text()  子类可共同复用
#
#   parse() 返回 ParsedDocument
#                    |
#                    +--> pages: List[ParsedPage]
#                                      |
#                                      +--> content：页面兼容纯文本
#                                      +--> blocks: List[ContentBlock]
#                                                        |
#                                                        +--> text
#                                                        +--> table
#                                                        +--> image_text
#
#   ParsedDocument.full_text 把所有非空页面 content 合并，供后续分块和 Embedding 使用。
# =============================================================================

# ABC/abstractmethod 用于定义不能直接完整使用、必须由子类实现关键方法的抽象类。
from abc import ABC, abstractmethod
# dataclass 自动生成 __init__ 等样板代码；field(default_factory=...) 安全创建独立容器。
from dataclasses import dataclass, field
from typing import List, Optional

'''
这个文件定义了 文档解析器的基类和数据结构 ，是整个 RAG 系统中文档处理的第一步——
将各种格式的文件（PDF、Word、Excel 等）解析为统一的文本格式。
'''

@dataclass
class ContentBlock:
    """内容块，标记来源类型"""
    # type 表示内容来源，便于后续分块时保留表格或 OCR 语义。
    type: str  # "text" | "table" | "image_text"
    # content 是该块的实际文本。
    content: str
    # metadata 可保存页码、表格编号等附加信息；每个实例获得独立字典。
    metadata: dict = field(default_factory=dict)


@dataclass
class ParsedPage:
    # page_num 从 1 开始；Excel 中可把每个工作表视作一页。
    page_num: int
    # content 是向后兼容的整页合并文本。
    content: str = ""
    # blocks 提供比整页字符串更细的结构，避免表格和正文完全混在一起。
    blocks: List[ContentBlock] = field(default_factory=list)

#表示 整个文档 的解析结果，包含所有页面和元数据
@dataclass
class ParsedDocument:
    # default_factory=list 避免多个 ParsedDocument 共享同一个可变列表。
    pages: List[ParsedPage] = field(default_factory=list)
    # 文档级元数据可以记录总页数、解析器名称等。
    metadata: dict = field(default_factory=dict)

    # 合并所有页面内容为一个字符串 @property将方法转换为属性访问，方便后续使用
    @property
    def full_text(self) -> str:
        #将所有页面的内容用两个换行符连接
        # if p.content.strip() 会跳过纯空白页面；两个换行保留页面之间的自然分隔。
        return "\n\n".join(p.content for p in self.pages if p.content.strip())


class BaseParser(ABC):
    # 定义解析方法，必须实现
    @abstractmethod 
    def parse(self, file_path: str) -> ParsedDocument:
        # 抽象方法只规定接口，PDFParser、WordParser 等子类负责具体实现。
        pass

    # 定义文本清理方法，用于处理解析后的文本，去除空行和额外空白
    @staticmethod
    def clean_text(text: str) -> str:
        # 放在函数内部导入，只有真正清理文本时才加载这些依赖。
        import ftfy
        import re

        #修复文本编码问题（如乱码字符）
        text = ftfy.fix_text(text)
        # 去除连续空行，最多保留两个空行间隔
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 去除行首尾空白
        lines = [line.strip() for line in text.splitlines()]
        # 过滤纯空行聚集
        text = "\n".join(lines)
        return text.strip()
