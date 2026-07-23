# =============================================================================
# 文件作用与架构位置（PDF 复合内容解析器）
# =============================================================================
# PDF 页面可能同时包含普通文字、表格和嵌入图片。本文件把三类内容分别提取为
# ContentBlock，再汇总为统一 ParsedDocument。
#
# PDFParser 有 3 个方法：
#
#   parse()                 主流程：逐页协调文字、表格和图片 OCR
#   _extract_tables()       从一页中提取表格并渲染为 pipe 文本
#   _extract_image_text()   对一页中的嵌入图片执行 OCR
#
# 调用关系和数据流：
#
#   PDF 文件 -> fitz.open
#                  |
#                  v
#              逐页处理
#        +---------+----------+
#        |         |          |
#     page文本   表格提取    图片提取
#        |    _extract_tables  _extract_image_text -> OCR
#        |         |          |
#        +---------+----------+
#                  |
#             ContentBlock 列表
#                  |
#        必要时合并跨页连续表格
#                  |
#             ParsedPage 列表
#                  |
#             ParsedDocument
# =============================================================================

from typing import List
from app.parsers.base import BaseParser, ParsedDocument, ParsedPage, ContentBlock


class PDFParser(BaseParser):
    def parse(self, file_path: str) -> ParsedDocument:
        # PyMuPDF 的导入名是 fitz，用于打开 PDF、读取文本、表格和嵌入图片。
        import fitz

        doc = ParsedDocument()
        # 打开 PDF 文档；pdf 可迭代，每次得到一个页面对象。
        pdf = fitz.open(file_path)
        # 保存上一页最后一个块的引用，用来识别并合并跨页表格。
        prev_last_block = None  # 用于跨页表格合并

        # enumerate(..., start=1) 让返回页码符合用户习惯，从 1 而不是 0 开始。
        for page_num, page in enumerate(pdf, start=1):
            # blocks 严格保留当前页不同来源内容的类型和顺序分组。
            blocks = []

            # 1. 纯文本
            # page.get_text("text") 读取 PDF 的文字层；扫描件可能没有文字层。
            text = self.clean_text(page.get_text("text"))
            if text:
                blocks.append(ContentBlock(type="text", content=text))

            # 2. 表格（结构化 pipe 渲染）
            table_contents = self._extract_tables(page)
            for tbl_text in table_contents:
                # 每张表作为独立原子块，避免后续切分时过早与普通段落混合。
                blocks.append(ContentBlock(type="table", content=tbl_text))

            # 3. 图片 OCR
            ocr_texts = self._extract_image_text(page, file_path)
            for ocr_text in ocr_texts:
                blocks.append(ContentBlock(type="image_text", content=ocr_text))

            # === 跨页表格合并 ===
            # 条件：上一页最后一块是 table，且当前页第一块也是 table，则合并为同一张表
            if prev_last_block and prev_last_block.type == "table" and blocks and blocks[0].type == "table":
                # prev_last_block 仍引用上一页已加入 doc.pages 的 ContentBlock；修改它的 content
                # 就能把当前页首表格直接接到上一页表格后面。
                prev_last_block.content += "\n" + blocks.pop(0).content

            # 构建向后兼容的 content 字符串
            page_content = "\n\n".join(b.content for b in blocks)
            page_content = self.clean_text(page_content)

            if page_content or blocks:
                # content 供只认识纯文本的旧流程使用，blocks 供结构化的新流程使用。
                doc.pages.append(ParsedPage(
                    page_num=page_num,
                    content=page_content,
                    blocks=blocks,
                ))

            # 为下一轮页面处理保存当前页最后一个未被弹出的块。
            prev_last_block = blocks[-1] if blocks else None

        # PDF 原始页数可能大于最终 pages 数，因为完全空白页不会加入结果。
        doc.metadata["total_pages"] = len(pdf)
        # 显式关闭底层文件句柄，释放 PDF 文件资源。
        pdf.close()
        return doc

    def _extract_tables(self, page) -> List[str]:
        """提取页面中的表格，返回每个表格的 pipe 渲染文本列表"""
        try:
            # find_tables() 使用 PyMuPDF 的表格检测能力定位当前页中的表格。
            tables = page.find_tables()
            if not tables or not tables.tables:
                return []
            results = []
            for tbl in tables.tables:
                rows = []
                # extract() 返回二维单元格数组；None 表示空单元格。
                for row in tbl.extract():
                    cells = [str(cell).strip() if cell is not None else "" for cell in row]
                    rows.append(" | ".join(cells))
                if rows:
                    # 每行用换行分开，列用 | 分开，形成适合文本模型读取的结构。
                    results.append("\n".join(rows))
            return results
        except Exception:
            # 个别 PDF 的表格结构可能损坏。表格提取失败时返回空列表，普通文字仍可继续解析。
            return []

    def _extract_image_text(self, page, file_path: str) -> List[str]:
        """提取页面中嵌入图片的 OCR 文字，返回文本列表"""
        try:
            # full=True 返回更完整的图片引用信息，其中第一个元素是 xref 对象编号。
            images = page.get_images(full=True)
            if not images:
                return []
            # 延迟导入 OCR，只有页面确实含图片时才加载相关模块。
            from app.parsers.ocr_utils import ocr_image, clean_ocr_text
            import fitz

            results = []
            for img in images:
                # xref 是 PDF 内部对象编号，可用于从父文档提取原始图片字节。
                xref = img[0]
                base_image = page.parent.extract_image(xref)
                if not base_image or not base_image.get("image"):
                    continue
                # OCR 接收图片字节，返回文字；随后再次使用 OCR 专用规则清理。
                text = ocr_image(base_image["image"])
                text = clean_ocr_text(text)
                # 过滤空结果和极短噪声，避免无意义字符进入知识库。
                if text and len(text) > 2:
                    results.append(text)

            return results
        except Exception:
            # 图片解码或 OCR 失败时跳过图片，不影响同一 PDF 的文字和表格解析。
            return []
