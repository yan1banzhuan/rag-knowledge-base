# =============================================================================
# 文件作用与架构位置（Markdown 解析器）
# =============================================================================
# 本文件只有 MarkdownParser.parse()。它和 TextParser 的主要区别是保留 Markdown 的换行
# 与缩进，因为列表层级和代码块依赖这些空白结构。
#
#   .md 文件 -> 尝试多种编码 -> ftfy 修复乱码 -> 保留结构 -> ParsedDocument
# =============================================================================

from app.parsers.base import BaseParser, ParsedDocument, ParsedPage


class MarkdownParser(BaseParser):
    """
    Markdown 解析器 — 保留原始 Markdown 结构。
    与 TextParser 的区别：不调用 clean_text()，避免 line.strip() 破坏
    缩进语义（嵌套列表、代码块等）。
    """

    def parse(self, file_path: str) -> ParsedDocument:
        doc = ParsedDocument()
        # 按常见程度依次尝试编码；成功读取后 break 结束循环。
        for encoding in ("utf-8", "gbk", "utf-16"):
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    text = f.read()
                break
            except UnicodeDecodeError:
                # 当前编码不匹配时尝试下一种，而不是立即让整个解析失败。
                continue
        else:
            # for 的 else 仅在循环没有 break 时执行；最后忽略无法解码的个别字节。
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()

        # 只修复编码乱码，不破坏 Markdown 结构
        import ftfy
        text = ftfy.fix_text(text)
        text = text.strip()

        if text:
            doc.pages.append(ParsedPage(page_num=1, content=text))
        return doc
