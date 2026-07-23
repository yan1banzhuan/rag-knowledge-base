# =============================================================================
# 文件作用与架构位置（OCR 兼容别名）
# =============================================================================
# PDF 解析器只需要“识别图片”和“清理 OCR 文本”两个函数。本文件把 OCRService 的类方法
# 暴露为简短函数别名，避免 PDFParser 依赖 OCRService 的其他实现细节。
#
#   PDFParser -> ocr_image(...)    -> OCRService.recognize(...)
#             -> clean_ocr_text()  -> OCRService.clean_text()
#
# 本文件没有自定义函数体；下面两个名称直接引用已有方法，调用效果与原方法相同。
# =============================================================================

from app.services.ocr import OCRService

# 函数对象赋值，不会立即执行 OCR。
ocr_image = OCRService.recognize
clean_ocr_text = OCRService.clean_text
