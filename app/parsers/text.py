# =============================================================================
# 文件作用与架构位置（纯文本解析器）
# =============================================================================
# 本文件只有 TextParser.parse()，负责读取 txt 文件、处理常见中文编码并调用父类的
# clean_text() 统一清理空白。
#
#   文件路径 -> 尝试 UTF-8/GBK/UTF-16 -> clean_text -> ParsedPage -> ParsedDocument
# =============================================================================

from app.parsers.base import BaseParser, ParsedDocument, ParsedPage

'''
        如果所有编码都失败（循环正常结束，没有 break ），则使用 utf-8 编码并忽略无法解码的
        字符（ errors="ignore" ），确保至少能读到部分内容。
'''
class TextParser(BaseParser):
    def parse(self, file_path: str) -> ParsedDocument:
        doc = ParsedDocument()
        # 成功读取会 break；解码失败则尝试下一种编码。
        for encoding in ("utf-8", "gbk", "utf-16"):
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    text = f.read()
                break
            except UnicodeDecodeError:
                continue
        else:
            # 三种严格解码都失败时忽略损坏字节，尽量保留仍可读取的内容。
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()

        content = self.clean_text(text)
        if content:
            # 普通文本没有原生页码概念，整个文件作为第 1 页。
            doc.pages.append(ParsedPage(page_num=1, content=content))
        return doc
