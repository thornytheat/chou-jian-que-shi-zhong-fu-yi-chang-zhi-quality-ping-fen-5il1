"""规则集与数据载入：CSV 解析、编码探测、表头契约校验、规则读取。

核心规则模块之外的所有「输入」都从这里进来，便于单测直接复用。
"""
from __future__ import annotations

import csv
import io
import json
import pathlib

from . import settings


class InputError(Exception):
    """输入不可用（编码错、缺列、空文件、超行数）→ 路由层转 400。"""


DEFAULT_MISSING_TOKENS = ("", "na", "n/a", "null", "none", "-", "—")

KNOWN_RULES = frozenset(
    {"missing", "type", "range", "outlier", "duplicate_key", "duplicate_row"}
)


def validate_rules_structure(rules: dict) -> None:
    """登记新规则版本前的最小结构校验；不合法 → InputError（路由层转 400）。"""
    if not isinstance(rules, dict):
        raise InputError("规则集必须是 JSON 对象。")
    version = str(rules.get("version") or "").strip()
    if not version:
        raise InputError("规则集缺少 version 字段（版本号必填，且不能与已登记版本重复）。")
    columns = rules.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise InputError("规则集缺少 columns 定义（至少声明一列）。")
    for name, spec in columns.items():
        if not isinstance(spec, dict):
            raise InputError(f"列 {name} 的定义必须是对象。")
        unknown = [r for r in (spec.get("rules") or []) if r not in KNOWN_RULES]
        if unknown:
            raise InputError(
                f"列 {name} 启用了未知规则：{'、'.join(unknown)}；"
                f"可用规则有 {'、'.join(sorted(KNOWN_RULES))}"
            )
    business_key = rules.get("business_key") or []
    dangling = [c for c in business_key if c not in columns]
    if dangling:
        raise InputError(f"business_key 引用了未定义的列：{'、'.join(dangling)}。")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """UTF-8-SIG / UTF-8 / GB18030 依次尝试，返回 (文本, 编码说明)。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig（含 BOM）"
        except UnicodeDecodeError as exc:
            raise InputError(f"带 BOM 的文件无法按 UTF-8 解码：{exc.reason}") from exc
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise InputError("无法识别文件编码，请另存为 UTF-8 或 GB18030 后重试。")


def load_rules(path: pathlib.Path | str | None = None) -> dict:
    p = pathlib.Path(path) if path else settings.RULES_JSON
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InputError(f"规则文件不存在：{p.name}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"规则文件不是合法 JSON：{exc.msg}") from exc


def parse_csv_text(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """解析 CSV 文本 → (列名, 行字典列表)。空白行跳过，重复列名报错。"""
    if not text.strip():
        raise InputError("CSV 内容为空，请检查是否上传了空文件。")
    reader = csv.DictReader(io.StringIO(text))
    columns = [(c or "").strip() for c in (reader.fieldnames or [])]
    if not columns:
        raise InputError("CSV 缺少表头行。")
    if len(set(columns)) != len(columns):
        raise InputError("CSV 表头存在重复列名，请先去重。")
    rows: list[dict[str, str]] = []
    try:
        for raw in reader:
            if raw is None:
                continue
            item = {}
            for col in columns:
                value = raw.get(col)
                item[col] = "" if value is None else str(value).strip()
            if any(item.values()):
                rows.append(item)
    except csv.Error as exc:
        raise InputError(f"CSV 解析失败（第 {reader.line_num} 行附近）：{exc}") from exc
    if not rows:
        raise InputError("CSV 只有表头、没有数据行。")
    if len(rows) > settings.MAX_ROWS:
        raise InputError(f"数据超过 {settings.MAX_ROWS} 行上限，请分批抽检。")
    return columns, rows


def required_columns(rules: dict) -> list[str]:
    """规则里声明了规则的列即必需列（含 weight=0 的列）。"""
    return [name for name, spec in rules["columns"].items() if spec.get("rules")]


def validate_columns(columns: list[str], rules: dict) -> None:
    need = required_columns(rules)
    missing = [c for c in need if c not in columns]
    if missing:
        raise InputError(
            "CSV 缺少必需列：" + "、".join(missing)
            + "；规则集要求的列有 " + "、".join(need)
        )
    extra = [c for c in columns if c not in rules["columns"]]
    if extra:
        raise InputError(
            "CSV 含规则集未定义的列：" + "、".join(extra)
            + "；请先在规则集中声明这些列，或从 CSV 中移除。"
        )


def missing_tokens(rules: dict) -> tuple[str, ...]:
    tokens = rules.get("missing_tokens")
    if not tokens:
        return DEFAULT_MISSING_TOKENS
    return tuple(str(t).strip().lower() for t in tokens)


def load_csv_text(text: str, rules: dict, source: str) -> dict:
    columns, rows = parse_csv_text(text)
    validate_columns(columns, rules)
    return {"columns": columns, "rows": rows, "source": source}


def load_sample(rules: dict, path: pathlib.Path | str | None = None) -> dict:
    p = pathlib.Path(path) if path else settings.SAMPLE_CSV
    try:
        raw = p.read_bytes()
    except FileNotFoundError as exc:
        raise InputError("样例数据缺失：sample_data/quality_sample.csv") from exc
    text, encoding = decode_bytes(raw)
    data = load_csv_text(text, rules, f"样例数据 {p.name}")
    data["encoding"] = encoding
    return data


def to_float(value: str) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"
