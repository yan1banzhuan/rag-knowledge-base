# =============================================================================
# 文件作用与架构位置（图片 OCR 解析器）
# =============================================================================
# 图片通常没有可直接读取的文本层，因此本文件把图片交给 OCRService，再把识别文字包装
# 成统一 ParsedDocument。只有 1 个类 ImageParser 和 1 个方法 parse()。
#
#   图片路径 -> OCRService.recognize() -> 文字 -> ParsedPage -> ParsedDocument
# =============================================================================

from app.parsers.base import BaseParser, ParsedDocument, ParsedPage
from app.services.ocr import OCRService
from app.core.logger import logger


class ImageParser(BaseParser):
    def parse(self, file_path: str) -> ParsedDocument:
        # 日志记录文件路径，便于定位 OCR 耗时或失败的具体文件。
        logger.info(f"开始 OCR 处理图片: {file_path}")
        # OCRService 会按配置选择 OCR 提供方，并返回识别后的纯文本。
        text = OCRService.recognize(file_path)
        # 一张图片视作一页；metadata 标明内容来自图片 OCR。
        return ParsedDocument(
            pages=[ParsedPage(page_num=1, content=text)],
            metadata={"parser": "image_ocr"},
        )
