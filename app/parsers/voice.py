# =============================================================================
# 文件作用与架构位置（语音识别结果适配器）
# =============================================================================
# 本文件不直接请求语音识别服务。DocumentService 会先调用 VoiceASRService 获得文字，
# 然后 get_parser() 把文字传入 VoiceParser，最后转换成通用 ParsedDocument。
#
#   音频文件 -> VoiceASRService -> asr_text -> VoiceParser -> ParsedDocument
#
# 类中有两个方法：__init__() 保存识别文本；parse() 按统一解析器接口返回结果。
# =============================================================================

from app.parsers.base import BaseParser, ParsedDocument, ParsedPage


class VoiceParser(BaseParser):
    """
    语音文件解析器：将音频文件通过 ASR 识别为文本。
    实际识别逻辑在 DocumentService.process_document 中调用 VoiceASRService 完成，
    此 Parser 返回的 full_text 即为 ASR 识别结果。
    """

    def __init__(self, asr_text: str = ""):
        # 保存已经完成 ASR 的文本；默认空字符串便于无结果时仍返回合法结构。
        self.asr_text = asr_text

    def parse(self, file_path: str) -> ParsedDocument:
        """直接返回 ASR 识别文本（实际识别在 DocumentService 中调用）"""
        # file_path 不再用于读取音频，只作为溯源信息放入 metadata。
        return ParsedDocument(
            pages=[ParsedPage(page_num=1, content=self.asr_text)],
            metadata={"file_path": file_path},
        )
