"""SQLite 访问：运行记录（含报告快照）、规则版本、批量重评与规则权重覆盖。

表：runs（运行 + 原始数据快照 + 评分/依据版本）、settings（键值）、
rule_versions（规则版本快照）、reeval_campaigns（重评批次）、reeval_records（按规则版本的重评记录）。
够用即可，不引入 ORM。报告整份以 JSON 存储，重算权重时无需重新解析全部单元格。
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import threading

from . import settings

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  stage_index INTEGER NOT NULL DEFAULT 0,
  rows INTEGER NOT NULL DEFAULT 0,
  total_score REAL,
  grade TEXT,
  weight_overrides TEXT,
  report_json TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_versions (
  version TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  note TEXT,
  content_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reeval_campaigns (
  campaign_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  to_version TEXT NOT NULL,
  from_versions TEXT NOT NULL,
  status TEXT NOT NULL,
  total INTEGER NOT NULL,
  done_count INTEGER NOT NULL DEFAULT 0,
  failed_run_id TEXT,
  error TEXT,
  scope_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reeval_records (
  campaign_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  status TEXT NOT NULL,
  total_score REAL,
  grade TEXT,
  alerts_total INTEGER,
  report_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (campaign_id, run_id)
);
"""

# runs 表后加的列（老库 ALTER 迁移用）：原始数据快照 + 评分版本 + 当前依据版本
_RUN_EXTRA_COLUMNS = (
    ("rows_json", "TEXT"),
    ("rule_version", "TEXT"),
    ("basis_version", "TEXT"),
)

_RUN_BASE_COLUMNS = (
    "run_id, created_at, source, status, stage, stage_index, rows, total_score, grade,"
    " weight_overrides, report_json, error, rule_version, basis_version"
)


def connect() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript(SCHEMA)
            _migrate(_CONN)
            _CONN.commit()
        return _CONN


def _migrate(conn: sqlite3.Connection) -> None:
    """老库升级：补 runs 新列，并从报告快照回填评分/依据版本。调用方已持有 _LOCK。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    for name, ddl in _RUN_EXTRA_COLUMNS:
        if name not in columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")
    pending = conn.execute(
        "SELECT run_id, report_json FROM runs WHERE basis_version IS NULL"
    ).fetchall()
    for row in pending:
        version = None
        raw = row["report_json"]
        if raw:
            try:
                version = (json.loads(raw).get("dataset") or {}).get("rule_version")
            except json.JSONDecodeError:
                version = None
        if version:
            conn.execute(
                "UPDATE runs SET rule_version=?, basis_version=? WHERE run_id=?",
                (str(version), str(version), row["run_id"]),
            )


def reset_connection() -> None:
    """测试用：切换 DB 路径后重建连接。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_run(run_id: str, source: str, weight_overrides: dict | None,
               data: dict | None = None, rule_version: str | None = None) -> None:
    """登记一次运行；data 为 {"columns": [...], "rows": [...]} 原始快照，供日后按新版本重评。"""
    conn = connect()
    rows_json = json.dumps(data, ensure_ascii=False) if data else None
    version = str(rule_version) if rule_version else None
    with _LOCK:
        conn.execute(
            "INSERT INTO runs (run_id, created_at, source, status, stage, stage_index,"
            " weight_overrides, rows_json, rule_version, basis_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, now(), source, "running", "排队中", 0,
             json.dumps(weight_overrides or {}, ensure_ascii=False),
             rows_json, version, version),
        )
        conn.commit()


def set_progress(run_id: str, stage_index: int, stage: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET stage_index=?, stage=? WHERE run_id=?",
            (stage_index, stage, run_id),
        )
        conn.commit()


def finish_run(run_id: str, report: dict) -> None:
    conn = connect()
    score = report["score"]
    version = (report.get("dataset") or {}).get("rule_version")
    with _LOCK:
        conn.execute(
            "UPDATE runs SET status='done', stage='已完成', stage_index=99, rows=?,"
            " total_score=?, grade=?, report_json=?, rule_version=? WHERE run_id=?",
            (report["dataset"]["rows"], score["total"], score["grade"],
             json.dumps(report, ensure_ascii=False),
             str(version) if version else None, run_id),
        )
        conn.commit()


def fail_run(run_id: str, message: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET status='failed', stage='失败', error=? WHERE run_id=?",
            (message, run_id),
        )
        conn.commit()


def get_run(run_id: str, with_data: bool = False) -> dict | None:
    """with_data=True 时才带出 rows_json 原始快照（重评 worker 用），默认查询不取大字段。"""
    conn = connect()
    columns = "*" if with_data else _RUN_BASE_COLUMNS
    row = conn.execute(f"SELECT {columns} FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_runs(limit: int = 20) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT run_id, created_at, source, status, stage, rows, total_score, grade, error,"
        " rule_version, basis_version"
        " FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_report(run_id: str, report: dict, weights: dict | None) -> None:
    conn = connect()
    score = report["score"]
    with _LOCK:
        conn.execute(
            "UPDATE runs SET total_score=?, grade=?, report_json=?, weight_overrides=? WHERE run_id=?",
            (score["total"], score["grade"], json.dumps(report, ensure_ascii=False),
             json.dumps(weights or {}, ensure_ascii=False), run_id),
        )
        conn.commit()


def update_reeval_report(run_id: str, rule_version: str, report: dict,
                         weights: dict | None) -> None:
    """权重重算落在重评记录上：更新记录报告与 runs 展示分（仅当它是当前依据版本）。"""
    conn = connect()
    score = report["score"]
    payload = json.dumps(report, ensure_ascii=False)
    with _LOCK:
        conn.execute(
            "UPDATE reeval_records SET total_score=?, grade=?, report_json=?, updated_at=?"
            " WHERE run_id=? AND rule_version=? AND status='done'",
            (score["total"], score["grade"], payload, now(), run_id, str(rule_version)),
        )
        conn.execute(
            "UPDATE runs SET total_score=?, grade=?, weight_overrides=?"
            " WHERE run_id=? AND basis_version=?",
            (score["total"], score["grade"],
             json.dumps(weights or {}, ensure_ascii=False), run_id, str(rule_version)),
        )
        conn.commit()


def count_runs() -> int:
    conn = connect()
    row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = data.pop("report_json", None)
    data["report"] = json.loads(raw) if raw else None
    try:
        data["weight_overrides"] = json.loads(data.get("weight_overrides") or "{}")
    except json.JSONDecodeError:
        data["weight_overrides"] = {}
    return data


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def weight_overrides() -> dict:
    raw = get_setting("weight_overrides", "{}") or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {
        "columns": {k: float(v) for k, v in (data.get("columns") or {}).items()},
        "rule_weights": {k: float(v) for k, v in (data.get("rule_weights") or {}).items()},
    }


def save_weight_overrides(columns: dict, rule_weights: dict) -> dict:
    payload = {
        "columns": {k: float(v) for k, v in columns.items()},
        "rule_weights": {k: float(v) for k, v in rule_weights.items()},
    }
    set_setting("weight_overrides", json.dumps(payload, ensure_ascii=False))
    return payload


# ---------------------------------------------------------------- 规则版本


def ensure_rule_version(content: dict) -> bool:
    """启动时登记 rules.json 的版本：不存在则注册并设为当前；已存在则不动当前指针。"""
    version = str((content or {}).get("version") or "").strip()
    if not version:
        return False
    conn = connect()
    created = False
    with _LOCK:
        exists = conn.execute(
            "SELECT 1 FROM rule_versions WHERE version=?", (version,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO rule_versions (version, created_at, note, content_json)"
                " VALUES (?,?,?,?)",
                (version, now(), "rules.json 启动注册", json.dumps(content, ensure_ascii=False)),
            )
            created = True
        pointer = conn.execute(
            "SELECT value FROM settings WHERE key='current_rule_version'"
        ).fetchone()
        if created or pointer is None:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('current_rule_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (version,),
            )
        conn.commit()
    return created


def register_rule_version(version: str, content: dict, note: str = "") -> None:
    """登记新规则版本并设为当前版本（调用方负责校验结构与版本号唯一性）。"""
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO rule_versions (version, created_at, note, content_json)"
            " VALUES (?,?,?,?)",
            (version, now(), note, json.dumps(content, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('current_rule_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (version,),
        )
        conn.commit()


def set_current_rule_version(version: str) -> None:
    set_setting("current_rule_version", version)


def current_rule_version() -> str | None:
    return get_setting("current_rule_version")


def get_rule_version(version: str) -> dict | None:
    conn = connect()
    row = conn.execute(
        "SELECT content_json FROM rule_versions WHERE version=?", (version,)
    ).fetchone()
    return json.loads(row["content_json"]) if row else None


def list_rule_versions() -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT version, created_at, note FROM rule_versions"
        " ORDER BY created_at ASC, version ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def current_rules() -> dict | None:
    version = current_rule_version()
    return get_rule_version(version) if version else None


# ---------------------------------------------------------------- 批量重评


def eligible_runs(current_version: str) -> list[str]:
    """可参与重评的运行：已完成、有原始数据快照、依据版本不是当前版本。最老的先评。"""
    conn = connect()
    rows = conn.execute(
        "SELECT run_id FROM runs"
        " WHERE status='done' AND rows_json IS NOT NULL"
        " AND COALESCE(basis_version, rule_version) != ?"
        " ORDER BY created_at ASC, run_id ASC",
        (str(current_version),),
    ).fetchall()
    return [r["run_id"] for r in rows]


def runs_without_data(current_version: str) -> int:
    """已完成但无原始数据快照、无法重评的历史批次数（功能上线前的上传）。"""
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs"
        " WHERE status='done' AND rows_json IS NULL"
        " AND COALESCE(basis_version, rule_version) != ?",
        (str(current_version),),
    ).fetchone()
    return int(row["n"])


def basis_versions_of(run_ids: list[str]) -> list[str]:
    if not run_ids:
        return []
    conn = connect()
    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT DISTINCT COALESCE(basis_version, rule_version) AS v"
        f" FROM runs WHERE run_id IN ({marks})",
        list(run_ids),
    ).fetchall()
    return sorted(str(r["v"]) for r in rows if r["v"])


def create_campaign(campaign_id: str, to_version: str, scope: list[str],
                    from_versions: list[str]) -> None:
    conn = connect()
    timestamp = now()
    with _LOCK:
        conn.execute(
            "INSERT INTO reeval_campaigns (campaign_id, created_at, updated_at, to_version,"
            " from_versions, status, total, done_count, scope_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (campaign_id, timestamp, timestamp, str(to_version),
             json.dumps(from_versions, ensure_ascii=False), "running", len(scope), 0,
             json.dumps(scope, ensure_ascii=False)),
        )
        conn.commit()


def _campaign_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        data["from_versions"] = json.loads(data.get("from_versions") or "[]")
    except json.JSONDecodeError:
        data["from_versions"] = []
    try:
        data["scope"] = json.loads(data.pop("scope_json", None) or "[]")
    except json.JSONDecodeError:
        data["scope"] = []
    return data


def get_campaign(campaign_id: str) -> dict | None:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM reeval_campaigns WHERE campaign_id=?", (campaign_id,)
    ).fetchone()
    return _campaign_to_dict(row) if row else None


def latest_campaign() -> dict | None:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM reeval_campaigns ORDER BY created_at DESC, campaign_id DESC LIMIT 1"
    ).fetchone()
    return _campaign_to_dict(row) if row else None


def list_campaigns(limit: int = 10) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM reeval_campaigns ORDER BY created_at DESC, campaign_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_campaign_to_dict(r) for r in rows]


def active_campaign(to_version: str | None = None) -> dict | None:
    """进行中或失败待续跑的批次；指定 to_version 时只看该目标版本。"""
    conn = connect()
    sql = ("SELECT * FROM reeval_campaigns WHERE status IN ('running','failed')")
    args: list = []
    if to_version is not None:
        sql += " AND to_version=?"
        args.append(str(to_version))
    sql += " ORDER BY created_at DESC, campaign_id DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    return _campaign_to_dict(row) if row else None


def interrupt_running_campaigns(message: str = "服务重启导致重评中断，可从断点续跑") -> int:
    """进程重启后，把停在 running 的批次标记为失败（记录保留，可续跑）。"""
    conn = connect()
    with _LOCK:
        cursor = conn.execute(
            "UPDATE reeval_campaigns SET status='failed', error=?, updated_at=?"
            " WHERE status='running'",
            (message, now()),
        )
        conn.commit()
        return cursor.rowcount


def save_reeval_record(campaign_id: str, run_id: str, rule_version: str, status: str,
                       report: dict | None = None, error: str | None = None) -> None:
    """按 (campaign_id, run_id)  upsert：同一批重试只更新原记录，不新增重复记录。"""
    conn = connect()
    score = (report or {}).get("score") or {}
    alerts = (report or {}).get("alerts") or {}
    payload = json.dumps(report, ensure_ascii=False) if report else None
    timestamp = now()
    with _LOCK:
        conn.execute(
            "INSERT INTO reeval_records (campaign_id, run_id, rule_version, status,"
            " total_score, grade, alerts_total, report_json, error, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(campaign_id, run_id) DO UPDATE SET"
            " rule_version=excluded.rule_version, status=excluded.status,"
            " total_score=excluded.total_score, grade=excluded.grade,"
            " alerts_total=excluded.alerts_total, report_json=excluded.report_json,"
            " error=excluded.error, updated_at=excluded.updated_at",
            (campaign_id, run_id, str(rule_version), status,
             score.get("total"), score.get("grade"), alerts.get("total"),
             payload, error, timestamp, timestamp),
        )
        conn.commit()


def get_reeval_record(campaign_id: str, run_id: str) -> dict | None:
    conn = connect()
    row = conn.execute(
        "SELECT campaign_id, run_id, rule_version, status, total_score, grade,"
        " alerts_total, error, created_at, updated_at"
        " FROM reeval_records WHERE campaign_id=? AND run_id=?",
        (campaign_id, run_id),
    ).fetchone()
    return dict(row) if row else None


def campaign_records(campaign_id: str) -> list[dict]:
    """批次的全部重评记录（不含报告正文，列表展示用）。"""
    conn = connect()
    rows = conn.execute(
        "SELECT campaign_id, run_id, rule_version, status, total_score, grade,"
        " alerts_total, error, created_at, updated_at"
        " FROM reeval_records WHERE campaign_id=? ORDER BY created_at ASC, run_id ASC",
        (campaign_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def records_for_run(run_id: str) -> list[dict]:
    """某次运行的全部重评记录（新的在前，不含报告正文）。"""
    conn = connect()
    rows = conn.execute(
        "SELECT campaign_id, run_id, rule_version, status, total_score, grade,"
        " alerts_total, error, created_at, updated_at"
        " FROM reeval_records WHERE run_id=?"
        " ORDER BY updated_at DESC, campaign_id DESC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_done_record(run_id: str, rule_version: str) -> dict | None:
    """某运行在某规则版本下最近一条成功的重评记录（含报告正文）。"""
    conn = connect()
    row = conn.execute(
        "SELECT * FROM reeval_records WHERE run_id=? AND rule_version=? AND status='done'"
        " ORDER BY updated_at DESC, campaign_id DESC LIMIT 1",
        (run_id, str(rule_version)),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    raw = data.pop("report_json", None)
    data["report"] = json.loads(raw) if raw else None
    return data


def bump_campaign_done(campaign_id: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE reeval_campaigns SET done_count=done_count+1, updated_at=?"
            " WHERE campaign_id=?",
            (now(), campaign_id),
        )
        conn.commit()


def fail_campaign(campaign_id: str, run_id: str | None, error: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE reeval_campaigns SET status='failed', failed_run_id=?, error=?,"
            " updated_at=? WHERE campaign_id=?",
            (run_id, error, now(), campaign_id),
        )
        conn.commit()


def resume_campaign(campaign_id: str) -> bool:
    """失败的批次回到 running（断点续跑）；已成功记录保留，返回是否切换成功。"""
    conn = connect()
    with _LOCK:
        cursor = conn.execute(
            "UPDATE reeval_campaigns SET status='running', failed_run_id=NULL, error=NULL,"
            " updated_at=? WHERE campaign_id=? AND status='failed'",
            (now(), campaign_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def finish_campaign(campaign_id: str) -> None:
    """整批切换整改依据：一个事务内更新范围内所有运行的依据版本与展示分数。"""
    conn = connect()
    records = [r for r in campaign_records(campaign_id) if r["status"] == "done"]
    with _LOCK:
        for record in records:
            conn.execute(
                "UPDATE runs SET basis_version=?, total_score=?, grade=? WHERE run_id=?",
                (record["rule_version"], record["total_score"], record["grade"],
                 record["run_id"]),
            )
        conn.execute(
            "UPDATE reeval_campaigns SET status='done', updated_at=? WHERE campaign_id=?",
            (now(), campaign_id),
        )
        conn.commit()


# ---------------------------------------------------------------- 原始数据快照


def runs_missing_data(source_like: str) -> list[str]:
    conn = connect()
    rows = conn.execute(
        "SELECT run_id FROM runs WHERE rows_json IS NULL AND source LIKE ?",
        (source_like,),
    ).fetchall()
    return [r["run_id"] for r in rows]


def attach_run_data(run_id: str, data: dict) -> None:
    """给历史运行补存原始数据快照（老库升级回填，只补空的不覆盖）。"""
    conn = connect()
    payload = json.dumps(data, ensure_ascii=False)
    with _LOCK:
        conn.execute(
            "UPDATE runs SET rows_json=? WHERE run_id=? AND rows_json IS NULL",
            (payload, run_id),
        )
        conn.commit()
