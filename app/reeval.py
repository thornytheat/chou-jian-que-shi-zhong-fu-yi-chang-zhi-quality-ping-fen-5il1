"""批量重评：规则调整后，按新规则版本给历史批次生成独立重评记录。

口径：
- 每个批次（运行）的重评结果是一条独立记录，原始报告快照不被覆盖，旧分数与旧工单继续可查。
- 重评记录挂在 (campaign_id, run_id) 上，同一批重试只更新原记录，不新增重复记录。
- 只有批次内全部运行成功，才在一个事务里把整改依据（basis_version）整批切到新版本；
  中途失败时所有批次仍停在旧版本，新旧依据不混用。
- 失败后可断点续跑：已完成的批次跳过，只重跑失败与未跑的。
"""
from __future__ import annotations

import json
import threading

from . import loader, pipeline, store


def start_campaign_thread(campaign_id: str) -> None:
    threading.Thread(
        target=run_campaign, args=(campaign_id,),
        name=f"reeval-{campaign_id}", daemon=True,
    ).start()


def run_campaign(campaign_id: str) -> None:
    """后台线程执行体：逐批重评，遇失败记录断点并停止，全部成功则整批切换依据。"""
    campaign = store.get_campaign(campaign_id)
    if not campaign or campaign["status"] != "running":
        return
    rules = store.get_rule_version(campaign["to_version"])
    if rules is None:
        store.fail_campaign(campaign_id, None, f"规则版本 v{campaign['to_version']} 不存在")
        return
    weights = store.weight_overrides()
    for run_id in campaign["scope"]:
        record = store.get_reeval_record(campaign_id, run_id)
        if record and record["status"] == "done":
            continue  # 断点续跑：已完成的批次直接跳过
        run = store.get_run(run_id, with_data=True)
        try:
            if run is None or not run.get("rows_json"):
                raise RuntimeError("该批次没有保存原始数据快照，无法重评")
            data = json.loads(run["rows_json"])
            loader.validate_columns(data["columns"], rules)  # 新规则缺列 → 本批失败
            report = pipeline.run(data["rows"], data["columns"], rules, weights)
            report["run_id"] = run_id
            report["dataset"]["source"] = run["source"]
            report["dataset"]["encoding"] = _original_encoding(run)
            report["reeval"] = {
                "campaign_id": campaign_id,
                "from_version": basis_version(run),
                "to_version": campaign["to_version"],
            }
            store.save_reeval_record(campaign_id, run_id, campaign["to_version"],
                                     "done", report=report)
            store.bump_campaign_done(campaign_id)
        except Exception as exc:  # noqa: BLE001 - 记录断点等待续跑，不整批失败
            store.save_reeval_record(campaign_id, run_id, campaign["to_version"],
                                     "failed", error=f"{type(exc).__name__}: {exc}")
            store.fail_campaign(campaign_id, run_id,
                                f"批次 {run_id} 重评失败：{exc}；已完成批次保留记录，"
                                f"整改依据仍全部为旧版本，可修正后断点续跑")
            return
    store.finish_campaign(campaign_id)


def _original_encoding(run: dict) -> str:
    report = run.get("report") or {}
    return (report.get("dataset") or {}).get("encoding") or "—"


# ---------------------------------------------------------------- 报告版本解析


def original_version(run: dict) -> str | None:
    """运行评分时使用的规则版本。"""
    version = run.get("rule_version")
    if not version and run.get("report"):
        version = (run["report"].get("dataset") or {}).get("rule_version")
    return str(version) if version else None


def basis_version(run: dict) -> str | None:
    """当前作为整改依据的规则版本（未切换过 = 评分版本）。"""
    return run.get("basis_version") or original_version(run)


def resolve_report(run: dict, version: str | None = None):
    """取某规则版本下的报告 → (report, kind, version)；没有该版本记录 → (None, None, version)。"""
    original = original_version(run)
    target = str(version) if version else basis_version(run)
    if target is None or (original and target == original):
        return run.get("report"), "original", original
    record = store.latest_done_record(run["run_id"], target)
    if record and record.get("report"):
        return record["report"], "reeval", target
    return None, None, target


def available_versions(run: dict) -> list[dict]:
    """版本切换器数据：原始版本 + 各重评成功版本，标注当前整改依据。"""
    items: list[dict] = []
    original = original_version(run)
    basis = basis_version(run)
    report = run.get("report") or {}
    if original:
        items.append({
            "version": original,
            "kind": "original",
            "score": (report.get("score") or {}).get("total"),
            "grade": (report.get("score") or {}).get("grade"),
            "alerts_total": (report.get("alerts") or {}).get("total"),
            "is_basis": original == basis,
            "campaign_id": None,
        })
    seen = {original}
    for record in store.records_for_run(run["run_id"]):
        version = record["rule_version"]
        if version in seen or record["status"] != "done":
            continue
        seen.add(version)
        items.append({
            "version": version,
            "kind": "reeval",
            "score": record.get("total_score"),
            "grade": record.get("grade"),
            "alerts_total": record.get("alerts_total"),
            "is_basis": version == basis,
            "campaign_id": record.get("campaign_id"),
        })
    return items
