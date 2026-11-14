"""规则版本与批量重评：版本登记、整批切换、失败断点续跑、记录不重复、老库迁移。"""
from __future__ import annotations

import copy
import json
import time

import pytest

from app import loader, main, pipeline, store
from conftest import poll

HEADER = "row_id,product_id,category,price,stock,weight,supplier,updated_at,rating\n"
BASE_RULES = loader.load_rules()


def valid_csv(rows: int = 12) -> str:
    lines = [HEADER]
    for i in range(1, rows + 1):
        lines.append(f"R{i:03d},P{i:03d},数码配件,{100 + i}.00,{i * 3},1.20,S01,2026-09-1{i % 9},4.5\n")
    return "".join(lines)


def make_rules(version: str) -> dict:
    """拷贝内置规则集：换版本号 + 放宽重量缺失率（5% → 20%），分数与工单必然变化。"""
    rules = copy.deepcopy(BASE_RULES)
    rules["version"] = version
    rules["columns"]["weight"]["max_missing_rate"] = 0.20
    return rules


def poll_campaign(client, campaign_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/reevals/{campaign_id}").json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"重评批次 {campaign_id} 超时未结束")


@pytest.fixture()
def restore_rules():
    """每个用例结束后把当前规则指针拨回文件版本，避免用例间串扰。"""
    yield
    store.set_current_rule_version(str(BASE_RULES.get("version")))
    main.reload_rules()


def test_register_rule_version(client, restore_rules):
    res = client.post("/api/rules/versions",
                      json={"rules": make_rules("2.0"), "note": "放宽重量缺失率"})
    assert res.status_code == 200
    assert res.json()["current"] == "2.0"
    assert res.json()["eligible"] >= 1                 # seed 批次仍是旧版，可重评
    assert client.get("/health").json()["rules_version"] == "2.0"

    versions = client.get("/api/rules/versions").json()
    assert versions["current"] == "2.0"
    assert [v["version"] for v in versions["versions"]] == ["1.0", "2.0"]

    again = client.post("/api/rules/versions", json={"rules": make_rules("2.0")})
    assert again.status_code == 409
    assert "已登记" in again.json()["detail"]

    # 历史批次分数不因登记新版本而变化
    seed = store.get_run("seed-sample")
    assert seed["basis_version"] == "1.0"


def test_register_rule_version_validation(client, restore_rules):
    missing_version = client.post("/api/rules/versions", json={"rules": {"columns": {"a": {}}}})
    assert missing_version.status_code == 400
    assert "version" in missing_version.json()["detail"]

    unknown_rule = client.post("/api/rules/versions", json={
        "rules": {"version": "8.0", "columns": {"a": {"rules": ["nope"]}}}})
    assert unknown_rule.status_code == 400
    assert "未知规则" in unknown_rule.json()["detail"]

    dangling_key = client.post("/api/rules/versions", json={
        "rules": {"version": "8.0", "columns": {"a": {}}, "business_key": ["ghost"]}})
    assert dangling_key.status_code == 400
    assert "business_key" in dangling_key.json()["detail"]

    not_object = client.post("/api/rules/versions", json={})
    assert not_object.status_code == 400


def test_reeval_full_flow(client, restore_rules):
    # 两个 v1 批次
    run_a = client.post("/api/runs/text", json={"csv_text": valid_csv(12)}).json()["run_id"]
    run_b = client.post("/api/runs/text", json={"csv_text": valid_csv(14)}).json()["run_id"]
    poll(client, run_a)
    poll(client, run_b)

    before = client.get("/api/runs/seed-sample/versions").json()
    assert before["basis"] == "1.0"
    v1 = {v["version"]: v for v in before["versions"]}["1.0"]

    res = client.post("/api/rules/versions",
                      json={"rules": make_rules("3.0"), "note": "放宽重量缺失率至 20%"})
    assert res.status_code == 200

    created = client.post("/api/reevals", json={})
    assert created.status_code == 200
    campaign_id = created.json()["campaign_id"]
    assert created.json()["total"] >= 3               # seed + 本用例两个批次（可能还有其它历史批次）

    campaign = poll_campaign(client, campaign_id)
    assert campaign["status"] == "done"
    assert campaign["done_count"] == campaign["total"]

    # 整批切换：范围内每个批次的依据版本都是 3.0，没有停在旧版的
    detail = client.get(f"/api/reevals/{campaign_id}").json()
    assert len(detail["records"]) == campaign["total"]
    for record in detail["records"]:
        assert record["status"] == "done"
        assert record["rule_version"] == "3.0"
        assert store.get_run(record["run_id"])["basis_version"] == "3.0"

    # 重评记录独立可查：新版本分数更高（重量缺失放宽），工单更少
    after = client.get("/api/runs/seed-sample/versions").json()
    assert after["original"] == "1.0"
    assert after["basis"] == "3.0"
    by_version = {v["version"]: v for v in after["versions"]}
    assert by_version["3.0"]["score"] > v1["score"]
    assert by_version["3.0"]["alerts_total"] < v1["alerts_total"]
    assert by_version["3.0"]["is_basis"] is True
    assert by_version["1.0"]["is_basis"] is False

    # 旧分数与旧工单继续可查
    old_page = client.get("/result/seed-sample?version=1.0")
    assert old_page.status_code == 200
    assert "正在查看历史版本" in old_page.text
    old_csv = client.get("/api/runs/seed-sample/alerts.csv?version=1.0")
    assert old_csv.status_code == 200
    assert "v1.0" in old_csv.headers["content-disposition"]
    # 默认打开 = 当前依据版本
    assert client.get("/result/seed-sample").status_code == 200
    assert client.get("/api/runs/seed-sample/alerts.csv").status_code == 200
    # 没评过的版本 → 404
    assert client.get("/result/seed-sample?version=9.9").status_code == 404
    assert client.get("/api/runs/seed-sample/alerts.csv?version=9.9").status_code == 404

    # 工作台能看出：批次规则版本、重评状态、工单变化
    workbench = client.get("/workbench").text
    assert "规则版本与批量重评" in workbench
    assert campaign_id in workbench
    assert "已完成" in workbench
    assert "工单变化" in workbench
    assert "已切换" in workbench or "当前版本" in workbench

    # 全部批次已是最新 → 不能再建批
    assert client.post("/api/reevals", json={}).status_code == 400


def test_reeval_failure_resume_and_no_duplicates(client, restore_rules, monkeypatch):
    res = client.post("/api/rules/versions",
                      json={"rules": make_rules("4.0"), "note": "演示断点续跑"})
    assert res.status_code == 200

    calls = {"n": 0, "healed": False}
    original_run = pipeline.run

    def flaky(rows, columns, rules_, weights=None, progress=None):
        calls["n"] += 1
        if calls["n"] == 2 and not calls["healed"]:
            raise RuntimeError("模拟重评中途失败")
        return original_run(rows, columns, rules_, weights, progress)

    monkeypatch.setattr(pipeline, "run", flaky)

    created = client.post("/api/reevals", json={})
    assert created.status_code == 200
    campaign_id = created.json()["campaign_id"]

    campaign = poll_campaign(client, campaign_id)
    assert campaign["status"] == "failed"
    assert campaign["done_count"] == 1                 # 第 2 批失败，停在断点
    assert campaign["failed_run_id"]

    # 失败不混用：没有任何批次切到 v4.0，整改依据全部仍是旧版本
    for item in client.get("/api/runs?limit=100").json()["runs"]:
        assert item["basis_version"] != "4.0"

    detail = client.get(f"/api/reevals/{campaign_id}").json()
    statuses = [r["status"] for r in detail["records"]]
    assert statuses.count("done") == 1
    assert statuses.count("failed") == 1

    # 存在失败批次时重复建批 → 409，只能断点续跑
    again = client.post("/api/reevals", json={})
    assert again.status_code == 409
    assert "断点续跑" in again.json()["detail"]

    # 断点续跑：已完成的跳过，只重跑失败与未跑的
    calls["healed"] = True
    resumed = client.post(f"/api/reevals/{campaign_id}/resume")
    assert resumed.status_code == 200
    campaign = poll_campaign(client, campaign_id)
    assert campaign["status"] == "done"
    assert campaign["done_count"] == campaign["total"]

    # 同一批重试不新增重复记录：记录数 == 批次数，run_id 唯一
    detail = client.get(f"/api/reevals/{campaign_id}").json()
    run_ids = [r["run_id"] for r in detail["records"]]
    assert len(run_ids) == campaign["total"]
    assert len(run_ids) == len(set(run_ids))
    assert all(r["status"] == "done" for r in detail["records"])

    # 整批切换完成
    for record in detail["records"]:
        assert store.get_run(record["run_id"])["basis_version"] == "4.0"

    # 已完成的批次不能续跑，也不能再建批
    assert client.post(f"/api/reevals/{campaign_id}/resume").status_code == 409
    assert client.post("/api/reevals", json={}).status_code == 400

    # seed 批次留下多版本轨迹：原始 1.0 + 各次重评
    versions = client.get("/api/runs/seed-sample/versions").json()
    assert versions["basis"] == "4.0"
    assert versions["original"] == "1.0"
    assert "4.0" in {v["version"] for v in versions["versions"]}


def test_rescore_applies_to_basis_report(client, restore_rules):
    """依据已切换到新版本后，按当前权重重算落在重评记录上，不回退到旧版口径。"""
    client.post("/api/rules/versions", json={"rules": make_rules("6.0")})
    campaign_id = client.post("/api/reevals", json={}).json()["campaign_id"]
    campaign = poll_campaign(client, campaign_id)
    assert campaign["status"] == "done"

    before = client.get("/api/runs/seed-sample/versions").json()
    v6_score = {v["version"]: v for v in before["versions"]}["6.0"]["score"]

    client.post("/api/rules/weights", json={"columns": {"rating": 6.0}, "rule_weights": {}})
    res = client.post("/api/runs/seed-sample/rescore")
    assert res.status_code == 200
    assert res.json()["version"] == "6.0"
    assert res.json()["total"] < v6_score            # 全空列权重加大 → 分数下降

    after = client.get("/api/runs/seed-sample/versions").json()
    assert after["basis"] == "6.0"
    by_version = {v["version"]: v for v in after["versions"]}
    assert by_version["6.0"]["score"] == res.json()["total"]
    assert by_version["1.0"]["score"] != res.json()["total"]   # 旧版记录保持原样
    client.post("/api/rules/weights/reset")


def test_run_without_data_not_eligible(client, restore_rules):
    run_id = client.post("/api/runs/text", json={"csv_text": valid_csv(11)}).json()["run_id"]
    poll(client, run_id)
    # 模拟功能上线前的历史批次：没有原始数据快照
    conn = store.connect()
    conn.execute("UPDATE runs SET rows_json=NULL WHERE run_id=?", (run_id,))
    conn.commit()

    client.post("/api/rules/versions", json={"rules": make_rules("5.0")})
    current = store.current_rule_version()
    assert current == "5.0"
    assert run_id not in store.eligible_runs(current)   # 无快照的批次不进重评范围
    assert "seed-sample" in store.eligible_runs(current)
    assert store.runs_without_data(current) >= 1


def test_migrate_old_db(tmp_path, monkeypatch):
    """老库（无新列、无新表）升级：补列 + 从报告快照回填评分/依据版本。"""
    import sqlite3

    from app import settings

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL,
          stage_index INTEGER NOT NULL DEFAULT 0, rows INTEGER NOT NULL DEFAULT 0,
          total_score REAL, grade TEXT, weight_overrides TEXT, report_json TEXT, error TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    report = {"dataset": {"rule_version": "1.0", "rows": 3},
              "score": {"total": 88.5, "grade": "B"}}
    conn.execute(
        "INSERT INTO runs (run_id, created_at, source, status, stage, stage_index,"
        " rows, total_score, grade, report_json)"
        " VALUES ('old-1', '2026-01-01 00:00:00', '样例数据', 'done', '已完成', 99,"
        " 3, 88.5, 'B', ?)",
        (json.dumps(report),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", db)
    store.reset_connection()
    try:
        run = store.get_run("old-1")
        assert run["rule_version"] == "1.0"
        assert run["basis_version"] == "1.0"
        assert store.list_rule_versions() == []        # 新表已建、为空
        assert store.latest_campaign() is None
    finally:
        store.reset_connection()
