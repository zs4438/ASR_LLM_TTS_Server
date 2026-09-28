r"""从 Excel 批量合成 TTS 音频文件。

用法示例：
    python tools/batch_tts_from_excel.py --xlsx input.xlsx --out-dir output
    python tools/batch_tts_from_excel.py --xlsx input.xlsx --out-dir output --workers 2 --overwrit
    用法步骤：
    先预览合成有多个音频文件
    1.python .\tools\batch_tts_from_excel.py --xlsx "<xlsx路径>" --out-dir "<输出目录>" --dry-run
    正式合成音频
    2.python .\tools\batch_tts_from_excel.py --xlsx "<xlsx路径>" --out-dir "<输出目录>"
    覆盖之前相同重命名的音频文件
    3.python .\tools\batch_tts_from_excel.py --xlsx "<xlsx路径>" --out-dir "<输出目录>" --overwrite

    python .\tools\batch_tts_from_excel.py --xlsx "C:\Users\Administrator\Desktop\播报音合成\智能语音助手语音合成内容.xlsx" --out-dir "C:\Users\Administrator\Desktop\播报音合成\tts合成内容" --dry-run
    python .\tools\batch_tts_from_excel.py --xlsx "C:\Users\Administrator\Desktop\播报音合成\智能语音助手语音合成内容.xlsx" --out-dir "C:\Users\Administrator\Desktop\播报音合成\tts合成内容"
    python .\tools\batch_tts_from_excel.py --xlsx "C:\Users\Administrator\Desktop\播报音合成\智能语音助手语音合成内容.xlsx" --out-dir "C:\Users\Administrator\Desktop\播报音合成\tts合成内容" --overwrite
    
说明：
    - 表格默认读取第一张工作表。
    - 表头默认需要包含：序号、音频名、合成内容。
    - 输出文件名格式为：[序号]音频名.扩展名。
    - 扩展名优先通过返回音频头识别，识别不到时按 .env 里的 BAIDU_TTS_AUE 推断。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import threading
import time
import zipfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from asr_llm_tts_server.config import ServerConfig  # noqa: E402
from asr_llm_tts_server.errors import ConfigError, ProviderError  # noqa: E402
from asr_llm_tts_server.providers import build_tts_client  # noqa: E402


INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
AUE_EXTENSION_MAP = {
    3: "mp3",
    4: "pcm",
    5: "pcm",
    6: "wav",
}
_thread_local = threading.local()


@dataclass(frozen=True)
class BatchTtsRow:
    """功能说明：保存 Excel 中一行待合成任务。

    入参含义：`row_number` 是 Excel 行号，`seq` 是序号，`audio_name` 是音频名，`text` 是合成内容。
    返回值说明：作为数据对象使用，无独立返回值。
    使用注意事项：`seq` 和 `audio_name` 会参与生成文件名，写文件前仍需做文件名安全清理。
    """

    row_number: int
    seq: str
    audio_name: str
    text: str


@dataclass(frozen=True)
class BatchTtsResult:
    """功能说明：保存单条 TTS 合成结果，便于最终生成 CSV 报告。

    入参含义：包含 Excel 行号、输出路径、状态、错误信息和音频字节数。
    返回值说明：作为数据对象使用，无独立返回值。
    使用注意事项：状态值固定为 ok、skip、fail，便于后续筛选失败条目。
    """

    row_number: int
    seq: str
    audio_name: str
    text: str
    status: str
    output_path: str
    audio_bytes: int
    error: str


def main() -> int:
    """功能说明：解析命令行参数，读取 Excel 表格并批量合成音频。

    入参含义：命令行传入 Excel 路径、输出目录、列名、并发数和重试次数。
    返回值说明：全部成功或仅跳过已存在文件时返回 0；存在失败条目时返回 1。
    使用注意事项：默认直接调用项目里的百度 TTS 客户端，不需要先启动本地 HTTP 服务器。
    """

    parser = argparse.ArgumentParser(description="从 Excel 批量合成 TTS 音频文件")
    parser.add_argument("--xlsx", required=True, help="Excel .xlsx 文件路径")
    parser.add_argument("--out-dir", required=True, help="音频输出文件夹")
    parser.add_argument("--sheet", default="", help="工作表名称；不填则读取第一张工作表")
    parser.add_argument("--header-row", type=int, default=1, help="表头所在行号，默认 1")
    parser.add_argument("--seq-col", default="序号", help="序号列表头，默认：序号")
    parser.add_argument("--name-col", default="音频名", help="音频名列表头，默认：音频名")
    parser.add_argument("--text-col", default="合成内容", help="合成内容列表头，默认：合成内容")
    parser.add_argument("--workers", type=int, default=1, help="并发合成数量，默认 1；建议 1-3")
    parser.add_argument("--retries", type=int, default=2, help="单条失败后的重试次数，默认 2")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="重试等待秒数，默认 1.0")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在音频文件")
    parser.add_argument("--dry-run", action="store_true", help="只读取表格并预览输出文件名，不实际合成")
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    out_dir = Path(args.out_dir)
    if not xlsx_path.exists():
        raise SystemExit(f"Excel 文件不存在：{xlsx_path}")

    config = ServerConfig.load()
    rows = read_tts_rows(
        xlsx_path=xlsx_path,
        sheet_name=args.sheet,
        header_row=args.header_row,
        seq_col=args.seq_col,
        name_col=args.name_col,
        text_col=args.text_col,
    )
    if not rows:
        raise SystemExit("Excel 中没有可合成的有效行，请检查表头和合成内容。")

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"读取到 {len(rows)} 条待合成内容，输出目录：{out_dir}")

    if args.dry_run:
        for row in rows[:20]:
            print(f"DRY-RUN row={row.row_number}: {build_output_stem(row)}")
        if len(rows) > 20:
            print(f"DRY-RUN 仅预览前 20 条，剩余 {len(rows) - 20} 条省略。")
        return 0

    workers = max(1, args.workers)
    results: list[BatchTtsResult] = []
    if workers == 1:
        client = build_tts_client(config)
        for index, row in enumerate(rows, start=1):
            result = synthesize_one_row(row, out_dir, config.baidu_tts_aue, client, args.overwrite, args.retries, args.retry_delay)
            results.append(result)
            print_progress(index, len(rows), result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(synthesize_one_row_threaded, row, out_dir, config, args.overwrite, args.retries, args.retry_delay): row
                for row in rows
            }
            for index, future in enumerate(as_completed(future_map), start=1):
                result = future.result()
                results.append(result)
                print_progress(index, len(rows), result)

    report_path = out_dir / "batch_tts_result.csv"
    write_report(report_path, sorted(results, key=lambda item: item.row_number))
    ok_count = sum(1 for item in results if item.status == "ok")
    skip_count = sum(1 for item in results if item.status == "skip")
    fail_count = sum(1 for item in results if item.status == "fail")
    print(f"完成：成功 {ok_count}，跳过 {skip_count}，失败 {fail_count}。报告：{report_path}")
    return 1 if fail_count else 0


def read_tts_rows(
    *,
    xlsx_path: Path,
    sheet_name: str,
    header_row: int,
    seq_col: str,
    name_col: str,
    text_col: str,
) -> list[BatchTtsRow]:
    """功能说明：从 .xlsx 工作簿中读取待合成 TTS 行。

    入参含义：`xlsx_path` 是 Excel 文件，`sheet_name` 是工作表名，`header_row` 是表头行，三个列名用于定位数据列。
    返回值说明：返回 `BatchTtsRow` 列表，只包含合成内容非空的有效行。
    使用注意事项：本函数只依赖 Python 标准库解析 xlsx，不要求额外安装 openpyxl。
    """

    rows = read_xlsx_sheet_rows(xlsx_path, sheet_name)
    if header_row < 1 or header_row > len(rows):
        raise SystemExit(f"表头行号超出范围：{header_row}")

    header_values = rows[header_row - 1]
    header_map = {normalize_cell(value): index for index, value in enumerate(header_values) if normalize_cell(value)}
    required = {seq_col: normalize_cell(seq_col), name_col: normalize_cell(name_col), text_col: normalize_cell(text_col)}
    missing = [label for label, normalized in required.items() if normalized not in header_map]
    if missing:
        existing = "、".join(value for value in header_map.keys() if value)
        raise SystemExit(f"Excel 缺少表头：{', '.join(missing)}。当前表头：{existing}")

    seq_index = header_map[required[seq_col]]
    name_index = header_map[required[name_col]]
    text_index = header_map[required[text_col]]

    result: list[BatchTtsRow] = []
    for offset, values in enumerate(rows[header_row:], start=header_row + 1):
        seq = normalize_sequence(get_cell(values, seq_index))
        audio_name = normalize_cell(get_cell(values, name_index))
        text = str(get_cell(values, text_index)).strip()
        if not seq and not audio_name and not text:
            continue
        if not text:
            continue
        if not seq:
            seq = str(offset - header_row)
        if not audio_name:
            audio_name = f"audio_{seq}"
        result.append(BatchTtsRow(row_number=offset, seq=seq, audio_name=audio_name, text=text))
    return result


def read_xlsx_sheet_rows(xlsx_path: Path, sheet_name: str) -> list[list[str]]:
    """功能说明：读取 .xlsx 中指定工作表的全部单元格文本。

    入参含义：`xlsx_path` 是工作簿路径，`sheet_name` 是可选工作表名称。
    返回值说明：返回二维列表，列表下标从 0 开始，对应 Excel 行列。
    使用注意事项：公式单元格只读取 Excel 保存的缓存值；如果缓存为空，需要先在 Excel 中保存一次。
    """

    with zipfile.ZipFile(xlsx_path) as archive:
        shared_strings = load_shared_strings(archive)
        sheet_path = resolve_sheet_path(archive, sheet_name)
        root = ElementTree.fromstring(archive.read(sheet_path))

    rows: list[list[str]] = []
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    for row_elem in root.findall(".//x:sheetData/x:row", namespace):
        row_index = int(row_elem.attrib.get("r", str(len(rows) + 1)))
        while len(rows) < row_index:
            rows.append([])
        row_values = rows[row_index - 1]
        for cell_elem in row_elem.findall("x:c", namespace):
            ref = cell_elem.attrib.get("r", "")
            col_index = column_index_from_ref(ref)
            while len(row_values) <= col_index:
                row_values.append("")
            row_values[col_index] = read_cell_value(cell_elem, shared_strings)
    return rows


def load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """功能说明：读取 .xlsx 的共享字符串表。

    入参含义：`archive` 是已打开的 xlsx zip 包。
    返回值说明：返回共享字符串列表，供单元格 `t=s` 时按索引取值。
    使用注意事项：富文本会把多个 `<t>` 节点拼接成完整文本。
    """

    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read(path))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values: list[str] = []
    for item in root.findall("x:si", namespace):
        parts = [node.text or "" for node in item.findall(".//x:t", namespace)]
        values.append("".join(parts))
    return values


def resolve_sheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    """功能说明：把工作表名称解析为 xlsx 内部 XML 路径。

    入参含义：`archive` 是 xlsx zip 包，`sheet_name` 是用户指定的工作表名，可为空。
    返回值说明：返回类似 `xl/worksheets/sheet1.xml` 的内部路径。
    使用注意事项：未指定工作表时默认读取工作簿里的第一张表。
    """

    workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rels_root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    workbook_ns = {
        "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    rels_ns = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    rel_map = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_root.findall("rel:Relationship", rels_ns)
        if "Id" in rel.attrib and "Target" in rel.attrib
    }
    sheets = workbook_root.findall("x:sheets/x:sheet", workbook_ns)
    if not sheets:
        raise SystemExit("Excel 工作簿中没有工作表。")

    selected = sheets[0]
    if sheet_name:
        selected = next((sheet for sheet in sheets if sheet.attrib.get("name") == sheet_name), None)
        if selected is None:
            names = "、".join(sheet.attrib.get("name", "") for sheet in sheets)
            raise SystemExit(f"找不到工作表：{sheet_name}。当前工作表：{names}")

    rel_id = selected.attrib.get(f"{{{workbook_ns['r']}}}id", "")
    target = rel_map.get(rel_id)
    if not target:
        raise SystemExit(f"无法解析工作表路径：{selected.attrib.get('name', '')}")
    return "xl/" + target.lstrip("/")


def read_cell_value(cell_elem: ElementTree.Element, shared_strings: list[str]) -> str:
    """功能说明：读取单个 xlsx 单元格的文本值。

    入参含义：`cell_elem` 是单元格 XML 节点，`shared_strings` 是共享字符串列表。
    返回值说明：返回单元格文本；空单元格返回空字符串。
    使用注意事项：数字会按 Excel 原始保存值返回，后续由序号规范化逻辑处理。
    """

    cell_type = cell_elem.attrib.get("t", "")
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell_elem.findall(".//x:t", namespace))

    value_elem = cell_elem.find("x:v", namespace)
    if value_elem is None or value_elem.text is None:
        return ""
    raw_value = value_elem.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)]
        except (ValueError, IndexError):
            return ""
    return raw_value


def column_index_from_ref(ref: str) -> int:
    """功能说明：把 Excel 单元格引用中的列名转换为 0 基下标。

    入参含义：`ref` 是类似 A1、BC12 的单元格引用。
    返回值说明：返回列下标，A 为 0，B 为 1。
    使用注意事项：引用为空或异常时返回 0，避免解析阶段直接崩溃。
    """

    letters = "".join(ch for ch in ref.upper() if "A" <= ch <= "Z")
    if not letters:
        return 0
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def synthesize_one_row_threaded(
    row: BatchTtsRow,
    out_dir: Path,
    config: ServerConfig,
    overwrite: bool,
    retries: int,
    retry_delay: float,
) -> BatchTtsResult:
    """功能说明：在线程池中合成一行 TTS，线程内复用自己的百度 TTS 客户端。

    入参含义：包含待合成行、输出目录、服务器配置、覆盖策略和重试策略。
    返回值说明：返回该行的 `BatchTtsResult`。
    使用注意事项：每个线程只创建一次客户端，避免每条都重复申请 token。
    """

    client = getattr(_thread_local, "tts_client", None)
    if client is None:
        client = build_tts_client(config)
        _thread_local.tts_client = client
    return synthesize_one_row(row, out_dir, config.baidu_tts_aue, client, overwrite, retries, retry_delay)


def synthesize_one_row(
    row: BatchTtsRow,
    out_dir: Path,
    baidu_tts_aue: int,
    client: object,
    overwrite: bool,
    retries: int,
    retry_delay: float,
) -> BatchTtsResult:
    """功能说明：合成单行文本并写入音频文件。

    入参含义：`row` 是 Excel 行数据，`out_dir` 是输出目录，`client` 是 TTS 客户端，后续参数控制覆盖和重试。
    返回值说明：返回合成结果，包含成功、跳过或失败。
    使用注意事项：先用临时文件写入，成功后再改名，避免中断时留下半个音频文件。
    """

    stem = build_output_stem(row)
    existing = find_existing_audio(out_dir, stem)
    if existing and not overwrite:
        return BatchTtsResult(row.row_number, row.seq, row.audio_name, row.text, "skip", str(existing), existing.stat().st_size, "")

    last_error = ""
    attempts = max(0, retries) + 1
    for attempt in range(1, attempts + 1):
        try:
            audio = client.synthesize_mp3(row.text)  # 当前接口名沿用历史命名，实际格式由 BAIDU_TTS_AUE 决定。
            ext = detect_audio_extension(audio, baidu_tts_aue)
            output_path = out_dir / f"{stem}.{ext}"
            tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
            if existing and overwrite and existing != output_path:
                existing.unlink(missing_ok=True)
            tmp_path.write_bytes(audio)
            tmp_path.replace(output_path)
            return BatchTtsResult(row.row_number, row.seq, row.audio_name, row.text, "ok", str(output_path), len(audio), "")
        except (ConfigError, ProviderError, OSError, ValueError) as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(max(0.0, retry_delay))
                continue
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(max(0.0, retry_delay))
                continue
    return BatchTtsResult(row.row_number, row.seq, row.audio_name, row.text, "fail", "", 0, last_error)


def build_output_stem(row: BatchTtsRow) -> str:
    """功能说明：根据行数据生成不带扩展名的目标文件名。

    入参含义：`row` 提供序号和音频名。
    返回值说明：返回 `[序号]音频名` 格式的安全文件名主干。
    使用注意事项：非法文件名字符会被替换成下划线，避免 Windows 和 Linux 保存失败。
    """

    seq = sanitize_filename_part(normalize_sequence(row.seq)) or str(row.row_number)
    name = sanitize_filename_part(row.audio_name) or "audio"
    return f"[{seq}]{name}"


def sanitize_filename_part(value: str) -> str:
    """功能说明：清理文件名片段中的非法字符。

    入参含义：`value` 是序号或音频名。
    返回值说明：返回可安全用于文件名的字符串。
    使用注意事项：会保留中文和空格，但会去掉首尾空白并限制过长片段。
    """

    cleaned = INVALID_FILENAME_CHARS.sub("_", str(value)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.rstrip(". ")
    return cleaned[:120]


def find_existing_audio(out_dir: Path, stem: str) -> Path | None:
    """功能说明：查找同名主干的已生成音频文件。

    入参含义：`out_dir` 是输出目录，`stem` 是不带扩展名的文件名主干。
    返回值说明：找到返回文件路径，否则返回 None。
    使用注意事项：用于支持不同音频扩展名下的断点续跑。
    """

    matches = sorted(path for path in out_dir.glob(f"{stem}.*") if path.is_file() and not path.name.endswith(".tmp"))
    return matches[0] if matches else None


def detect_audio_extension(audio: bytes, baidu_tts_aue: int) -> str:
    """功能说明：根据音频字节头和 BAIDU_TTS_AUE 推断输出扩展名。

    入参含义：`audio` 是 TTS 返回的音频字节，`baidu_tts_aue` 是 .env 中配置的百度 TTS 编码值。
    返回值说明：返回 mp3、wav、ogg、flac 或 pcm 等扩展名。
    使用注意事项：优先相信音频文件头；裸 PCM 没有固定魔术字，只能按配置兜底。
    """

    if audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
        return "wav"
    if audio.startswith(b"ID3") or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if audio.startswith(b"OggS"):
        return "ogg"
    if audio.startswith(b"fLaC"):
        return "flac"
    return AUE_EXTENSION_MAP.get(baidu_tts_aue, "audio")


def get_cell(values: list[str], index: int) -> str:
    """功能说明：安全读取一行中的指定列。

    入参含义：`values` 是行数据，`index` 是列下标。
    返回值说明：列存在返回单元格内容，否则返回空字符串。
    使用注意事项：避免行尾空单元格导致下标越界。
    """

    return values[index] if 0 <= index < len(values) else ""


def normalize_cell(value: object) -> str:
    """功能说明：规范化普通单元格文本。

    入参含义：`value` 是从 Excel 读取出的任意文本值。
    返回值说明：返回去掉首尾空白后的字符串。
    使用注意事项：表头匹配会移除所有空白，便于兼容 `合成 内容` 这类轻微格式差异。
    """

    return re.sub(r"\s+", "", str(value or "").strip())


def normalize_sequence(value: object) -> str:
    """功能说明：规范化序号单元格，避免 Excel 数字序号变成 1.0。

    入参含义：`value` 是序号单元格内容。
    返回值说明：如果是整数型浮点文本，返回整数文本；否则返回清理后的原文本。
    使用注意事项：文本序号如 A001 会保持原样。
    """

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except ValueError:
        pass
    return text


def print_progress(index: int, total: int, result: BatchTtsResult) -> None:
    """功能说明：打印单条合成进度。

    入参含义：`index` 是当前完成序号，`total` 是总条数，`result` 是单条结果。
    返回值说明：无返回值，直接打印。
    使用注意事项：失败时只打印错误摘要，完整文本在 CSV 报告中保留。
    """

    if result.status == "ok":
        print(f"[{index}/{total}] OK row={result.row_number} bytes={result.audio_bytes} -> {result.output_path}")
    elif result.status == "skip":
        print(f"[{index}/{total}] SKIP row={result.row_number} 已存在 -> {result.output_path}")
    else:
        print(f"[{index}/{total}] FAIL row={result.row_number}: {result.error[:200]}")


def write_report(report_path: Path, results: Iterable[BatchTtsResult]) -> None:
    """功能说明：把批量合成结果写入 CSV 报告。

    入参含义：`report_path` 是报告路径，`results` 是所有行的合成结果。
    返回值说明：无返回值，直接写入文件。
    使用注意事项：使用 utf-8-sig 编码，方便 Windows Excel 直接打开中文不乱码。
    """

    with report_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["row_number", "seq", "audio_name", "status", "output_path", "audio_bytes", "error", "text"],
        )
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "row_number": item.row_number,
                    "seq": item.seq,
                    "audio_name": item.audio_name,
                    "status": item.status,
                    "output_path": item.output_path,
                    "audio_bytes": item.audio_bytes,
                    "error": item.error,
                    "text": item.text,
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
