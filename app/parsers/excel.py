# =============================================================================
# 文件作用与架构位置（表格文件解析器）
# =============================================================================
# 本文件有两个解析器类，各实现一个 parse()：
#
#   ExcelParser.parse()  读取 xls/xlsx；每个工作表转换为一个 ParsedPage
#   CSVParser.parse()    读取 csv；整个文件转换为一个 ParsedPage
#
# 二者都会把二维表转换为以 " | " 分隔的纯文本，供通用分块、Embedding 和检索流程使用。
#
#   Excel/CSV -> pandas DataFrame -> 清理空行/空值 -> 行文本 -> ParsedDocument
# =============================================================================

from app.parsers.base import BaseParser, ParsedDocument, ParsedPage


'''
        Excel 解析器，用于将 Excel 文件解析为文本格式。
        支持解析 Excel 文件中的所有工作表，每个工作表的解析结果作为文档的一个页面。
        解析结果包含工作表名称、表头和数据行，每个数据行用 | 连接。
        空值用空字符串填充。

        原始 Excel：
        姓名 部门 迟到次数 扣款金额 
        张三 技术部 3      200 
        李四 销售部 5      500

        解析后的文本：

            【表格：Sheet1】
        姓名 | 部门 | 迟到次数 | 扣款金额
        ----------------------------------------
        张三 | 技术部 | 3 | 200
        李四 | 销售部 | 5 | 500

'''

class ExcelParser(BaseParser):
    def parse(self, file_path: str) -> ParsedDocument:
        # pandas 属于较重依赖，函数内导入可减少未使用 Excel 功能时的启动成本。
        import pandas as pd

        # 先创建空的统一结果容器。
        doc = ParsedDocument()
        # ExcelFile 读取工作簿目录，并允许后续按工作表解析。
        xl = pd.ExcelFile(file_path)

        # enumerate(..., start=1) 同时得到从 1 开始的页号和工作表名称。
        for sheet_num, sheet_name in enumerate(xl.sheet_names, start=1):
            # 解析每个工作表，将数据转换为字符串类型
            # 并删除所有全为空的行（how="all"），用空字符串填充缺失值（fillna("")）
            df = xl.parse(sheet_name, dtype=str) #将当前 Sheet 解析为 DataFrame，所有列强制转为字符串类型
            df = df.dropna(how="all").fillna("")
            if df.empty:
                # 全空工作表没有可检索内容，直接跳过。
                continue

            lines = [f"【表格：{sheet_name}】"]
            # 表头 用 | 连接所有列名作为表头，如 姓名 | 部门 | 迟到次数 | 扣款金额
            lines.append(" | ".join(str(c) for c in df.columns))
            lines.append("-" * 40)
            # 数据行 df.iterrows() ：逐行遍历 DataFrame，返回行索引和行数据
            for _, row in df.iterrows():
                row_text = " | ".join(str(v) for v in row.values)
                lines.append(row_text)

            content = self.clean_text("\n".join(lines))
            if content:
                # 一个工作表对应一个 ParsedPage，sheet_num 作为页号。
                doc.pages.append(ParsedPage(page_num=sheet_num, content=content))

        # 即使所有工作表都为空，也返回空 ParsedDocument，而不是 None。
        return doc

'''
        CSV 解析器，用于将 CSV 文件解析为文本格式。
        支持解析 CSV 文件中的所有数据行，每个数据行用 | 连接。
        空值用空字符串填充。
'''
class CSVParser(BaseParser):
    def parse(self, file_path: str) -> ParsedDocument:
        import pandas as pd

        doc = ParsedDocument()
        try:
            # 优先使用互联网和现代系统最常见的 UTF-8。
            df = pd.read_csv(file_path, dtype=str, encoding="utf-8")
        except UnicodeDecodeError:
            # 很多中文 Windows CSV 使用 GBK，UTF-8 失败时再回退读取。
            df = pd.read_csv(file_path, dtype=str, encoding="gbk")

        df = df.dropna(how="all").fillna("")
        lines = [" | ".join(str(c) for c in df.columns), "-" * 40]
        for _, row in df.iterrows():
            lines.append(" | ".join(str(v) for v in row.values))

        content = self.clean_text("\n".join(lines))
        if content:
            # CSV 没有工作表概念，整个文件统一记作第 1 页。
            doc.pages.append(ParsedPage(page_num=1, content=content))
        return doc
