# =============================================================================
# 文件作用与架构位置（图片文字识别服务）
# =============================================================================
# 本文件使用 RapidOCR 把图片像素转换为文本，被 ImageParser 和 PDF 内嵌图片解析调用。
#
# OCRService 有 4 个方法：
#
#   get_engine()       延迟创建并复用 RapidOCR 引擎
#   _is_valid_image()  校验路径、大小、文件完整性和尺寸（当前主 recognize 未复用它）
#   clean_text()       清理控制字符和多余空白
#   recognize()        完整校验、OCR、清理和错误转换主入口
#
#   图片文件路径 -> 图片有效性校验 -> RapidOCR -> txts -> clean_text -> 文本
#
# 当前 recognize() 的前置校验使用 os.path.exists/getsize，因此接口实际要求文件路径。
# PDFParser 当前会把内嵌图片原始 bytes 传给该别名，异常随后在 PDF 图片提取函数中被捕获；
# 这意味着现有实现下 PDF 内嵌图片 OCR 可能被跳过，这里仅说明行为，不修改逻辑。
#
# 模型在第一次识别时加载，后续请求复用类级 _engine，避免反复初始化。
# =============================================================================

import re
import os
from PIL import Image
from rapidocr import RapidOCR
from app.core.logger import logger

# RapidOCR 模型在首次推理时自动下载到 ~/.rapidocr/，无需手动配置
# 若需要自定义模型路径，可通过环境变量 RAPIDOCR_MODEL_DIR 设置


class OCRService:
    # _instance 当前没有参与逻辑；真正使用的单例缓存是 _engine。
    _instance = None
    _engine = None

    # 获取 RapidOCR 识别器实例，确保单例模式
    @classmethod
    def get_engine(cls) -> RapidOCR:
        # 惰性单例：第一次调用才创建，之后所有解析任务共享同一识别器。
        if cls._engine is None:
            logger.info("初始化 RapidOCR 识别器（中英文）")
            cls._engine = RapidOCR()
            logger.info("RapidOCR 识别器初始化完成")
        return cls._engine

    @staticmethod
    def _is_valid_image(image_path: str) -> bool:
        """校验图片文件是否有效且非空"""
        try:
            if not os.path.exists(image_path):
                return False
            if os.path.getsize(image_path) == 0:
                logger.warning(f"图片文件为空: {image_path}")
                return False
            img = Image.open(image_path)
            # verify 检查文件结构但会使当前 Image 对象不能继续正常读取，所以随后重新打开。
            img.verify()
            img = Image.open(image_path)
            width, height = img.size
            if width < 10 or height < 10:
                logger.warning(f"图片尺寸过小 ({width}x{height}): {image_path}")
                return False
            return True
        except Exception as e:
            logger.warning(f"图片文件损坏或无法读取 {image_path}: {e}")
            return False

    @staticmethod
    def clean_text(text: str) -> str:
        """对 OCR 识别结果进行清洗：去除控制字符、规范化空白、合并换行"""
        if not text:
            return ""

        # 1. 去除不可见控制字符
        # 范围包含 NUL、部分终端控制码等，但后面会重新按现存换行处理文本。
        text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)

        # 2. 将所有连续空白字符（含 tab 等）替换为单个空格
        text = re.sub(r"[ \t]+", " ", text)

        # 3. 按行分割，逐行清洗
        raw_lines = text.splitlines()
        cleaned = []
        for raw in raw_lines:
            line = raw.strip()
            if not line:
                # 暂时保留空行，后续统一压缩为最多一个段落分隔。
                cleaned.append("")
            else:
                line = re.sub(r" +", " ", line)
                cleaned.append(line)

        # 4. 去除首尾空行
        while cleaned and cleaned[0] == "":
            cleaned.pop(0)
        while cleaned and cleaned[-1] == "":
            cleaned.pop()

        # 5. 合并连续空行（最多保留一个空行作为段落分隔）
        merged = []
        for line in cleaned:
            if line == "":
                if merged and merged[-1] != "":
                    merged.append("")
            else:
                merged.append(line)

        # 6. 去除末尾空行
        while merged and merged[-1] == "":
            merged.pop()

        return "\n".join(merged).strip()

    @classmethod
    def recognize(cls, image_path: str) -> str:
        """
        使用 RapidOCR 识别图片中的文字。

        异常情况处理：
        - 文件不存在 / 文件为空 / 图片损坏  → raise ValueError("图片文件无效")
        - 图片尺寸过小                     → raise ValueError("图片文件无效")
        - 未识别到任何文字                  → raise ValueError("图片中未识别到文字内容")
        - 其他 OCR 异常                    → raise ValueError(f"OCR 处理失败: {原错误}")
        """
        # 前置校验：文件有效性
        # 这里展开写了与 _is_valid_image 相近的校验，以便返回更具体的 ValueError 信息。
        if not os.path.exists(image_path):
            raise ValueError(f"图片文件不存在: {image_path}")
        if os.path.getsize(image_path) == 0:
            raise ValueError("图片文件为空")
        try:
            img = Image.open(image_path)
            img.verify()
            # verify 后重新打开才能读取尺寸。
            img = Image.open(image_path)
            if img.size[0] < 10 or img.size[1] < 10:
                raise ValueError("图片文件无效")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"图片文件无效: {e}")

        try:
            engine = cls.get_engine()
            # RapidOCR 返回包含 txts、boxes、scores、elapse 等信息的输出对象。
            output = engine(image_path)

            if output is None or not hasattr(output, "txts"):
                logger.warning(f"RapidOCR 未识别到任何内容: {image_path}")
                raise ValueError("图片中未识别到文字内容")

            txts = output.txts
            if not txts:
                logger.warning(f"RapidOCR 未识别到任何内容: {image_path}")
                raise ValueError("图片中未识别到文字内容")

            # 当前实现只保留识别文本，将多个文本框内容用空格连接。
            raw_text = " ".join(str(t) for t in txts)
            cleaned = cls.clean_text(raw_text)

            if not cleaned:
                logger.warning(f"RapidOCR 清洗后文本为空: {image_path}")
                raise ValueError("图片中未识别到文字内容")

            elapsed = getattr(output, "elapse", 0)
            logger.info(f"RapidOCR 识别完成，提取文字 {len(cleaned)} 字符，耗时 {elapsed:.3f}s")
            return cleaned

        except ValueError:
            # 主动产生的业务错误保持原消息，不再包装成“处理失败”。
            raise
        except Exception as e:
            # 第三方库异常统一转成 ValueError，方便 DocumentService 标记文档 failed。
            logger.error(f"RapidOCR 处理异常: {e}")
            raise ValueError(f"OCR 处理失败: {e}")
