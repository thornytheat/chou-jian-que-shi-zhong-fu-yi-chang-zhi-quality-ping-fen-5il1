"""幂等 seed：首次启动用样例数据跑一次剖析，供历史列表与复核页立刻有内容。

幂等口径：settings.seed_version 记录已完成的 seed 版本，重复启动不再插入新记录。
老库升级时，样例来源的历史运行会补存原始数据快照（rows_json），使其可参与批量重评；
上传来源的历史运行数据已不在内存，无法补回，重评范围会自动跳过。
"""
from __future__ import annotations

from . import loader, pipeline, settings, store

SEED_RUN_ID = "seed-sample"


def ensure_seed() -> dict:
    store.connect()
    rules = store.current_rules() or loader.load_rules()
    current = store.get_setting("seed_version")
    if current == settings.SEED_VERSION and store.get_run(SEED_RUN_ID):
        backfill_sample_rows()
        return {"seeded": False, "run_id": SEED_RUN_ID, "rules": rules}
    if store.get_run(SEED_RUN_ID) is None:
        data = loader.load_sample(rules)
        report = pipeline.run(data["rows"], data["columns"], rules,
                              weights=store.weight_overrides())
        report["dataset"]["source"] = data["source"]
        report["dataset"]["encoding"] = data.get("encoding", "utf-8")
        report["seeded"] = True
        store.create_run(SEED_RUN_ID, data["source"], store.weight_overrides(),
                         data={"columns": data["columns"], "rows": data["rows"]},
                         rule_version=rules.get("version"))
        store.finish_run(SEED_RUN_ID, report)
    store.set_setting("seed_version", settings.SEED_VERSION)
    backfill_sample_rows()
    return {"seeded": True, "run_id": SEED_RUN_ID, "rules": rules}


def backfill_sample_rows() -> int:
    """给样例来源、但还没有原始数据快照的历史运行补一份样例数据。

    校验用文件版 rules.json（与样例 CSV 同包发布、必然一致）；当前规则版本若已
    加入样例 CSV 没有的新列，也不影响回填，回填失败不阻断启动。
    """
    pending = store.runs_missing_data("样例数据%")
    if not pending:
        return 0
    try:
        data = loader.load_sample(loader.load_rules())
    except loader.InputError:
        return 0
    payload = {"columns": data["columns"], "rows": data["rows"]}
    for run_id in pending:
        store.attach_run_data(run_id, payload)
    return len(pending)
