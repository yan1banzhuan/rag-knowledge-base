# =============================================================================
# 文件作用与架构位置（解析器工厂）
# =============================================================================
# parsers 包负责把 PDF、Word、Excel、图片、音频等不同格式统一转换为 ParsedDocument。
# 本文件既集中导出解析器类型，也提供 get_parser() 根据文件扩展名选择解析器。
#
#   上传文件路径 + file_type
#              |
#              v
#       get_parser(file_type)
#              |
#       +------+-------+---------+----------+
#       |              |         |          |
#      PDF           Word      Image      Voice ...
#       |              |         |          |
#       +--------------+---------+----------+
#                      |
#                      v
#              BaseParser.parse()
#                      |
#                      v
#                ParsedDocument
#
# 本文件只有 1 个函数 get_parser()。它不负责真正读取文件，而是返回适合该类型的解析器
# 实例；真正解析由各解析器的 parse() 方法完成。这种集中选择对象的写法称为“工厂”。
# =============================================================================

# 统一数据结构和抽象基类。
from app.parsers.base import BaseParser, ParsedDocument, ParsedPage, ContentBlock
# 各文件格式的具体实现。
from app.parsers.pdf import PDFParser
from app.parsers.word import WordParser
from app.parsers.excel import ExcelParser, CSVParser
from app.parsers.text import TextParser
from app.parsers.markdown import MarkdownParser
from app.parsers.image import ImageParser
from app.parsers.voice import VoiceParser


def get_parser(file_type: str, asr_text: str = "") -> BaseParser:
    # mapping 的键是小写扩展名，值是解析器类本身（此时还没有创建实例）。
    # 多个扩展名可以共用一个实现，例如 doc/docx 都由 WordParser 处理。
    mapping = {
        "pdf": PDFParser,
        "docx": WordParser,
        "doc": WordParser,
        "xlsx": ExcelParser,
        "xls": ExcelParser,
        "csv": CSVParser,
        "txt": TextParser,
        "md": MarkdownParser,
        "png": ImageParser,
        "jpg": ImageParser,
        "jpeg": ImageParser,
        "gif": ImageParser,
        "bmp": ImageParser,
        "webp": ImageParser,
        "mp3": VoiceParser,
        "wav": VoiceParser,
        "m4a": VoiceParser,
        "aac": VoiceParser,
        "ogg": VoiceParser,
        "wma": VoiceParser,
        "flac": VoiceParser,
        "pcm": VoiceParser,
    }
    # lower() 让 PDF、Pdf、pdf 等大小写形式都能匹配同一个键。
    cls = mapping.get(file_type.lower())
    if not cls:
        # 未支持的类型尽早失败，避免进入后续处理后才出现难理解的错误。
        raise ValueError(f"不支持的文件类型: {file_type}")
    # 音频已由 DocumentService 调用 ASR 得到文字，所以 VoiceParser 需要接收 asr_text。
    if file_type.lower() in mapping and mapping[file_type.lower()] is VoiceParser:
        return cls(asr_text=asr_text)
    # 普通解析器不需要初始化参数，直接实例化并返回。
    return cls()
