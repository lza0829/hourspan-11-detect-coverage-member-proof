"""detect_coverage_member_proof 验收脚本（架构乙：可落跑+失败闭）

直接对服务层与核证只读函数做黑盒验收，不依赖 HTTP。
运行：backend/.venv/bin/python -I tests/verify_coverage.py
"""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

# 隔离临时库（须在导入 app 之前固定 DATA_DIR）
TMP = tempfile.mkdtemp(prefix="hourspan-cov-")
FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
os.environ["DATA_DIR"] = TMP
os.environ["FIXTURES_DIR"] = str(FIXTURES)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402
from app.seed import init_db, now_iso, row_hash  # noqa: E402
from app.services import detect, ingest, scd  # noqa: E402
from app.modules.detect_coverage_member_proof import (  # noqa: E402
    ARCHITECTURE,
    RunNotFound,
    backfill_guard,
    coverage_proof,
)

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def hist_count(store_id):
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?", (store_id,)
        ).fetchone()["c"]
    finally:
        conn.close()


def main():
    print(f"== 架构选型：{ARCHITECTURE}（乙：可落跑+失败闭） ==")
    init_db()
    ing = ingest.run_batch("ingest_change_batch.json")
    check("入档夹具 2 条快照", ing["accepted"] == 2, json.dumps(ing, ensure_ascii=False))
    batch_id = ing["batch_id"]

    # 1) 批范围：含变更的一批应全覆盖（成员集合 == 范围集合）
    r1 = detect.run_detect(batch_id)
    conn = connect()
    try:
        p1 = coverage_proof(conn, r1["detect_run_id"])
    finally:
        conn.close()
    check("批检测 valid=true", r1["valid"] is True)
    check("范围数=2", p1["scope_count"] == 2, str(p1["scope_count"]))
    check("成员数=2（不只写 changes 之外不缺员）", p1["member_count"] == 2)
    check("成员集合与范围集合全等", p1["sets_equal"] is True)
    check("缺员列表为空", p1["missing_snapshot_ids"] == [])
    check("范围外混入为空", p1["extraneous_snapshot_ids"] == [])
    check("成员 kind 全 changed", p1["kind_counts"] == {"changed": 2, "unchanged": 0, "skipped": 0})
    check("跑次头列与核证一致(scope/member/valid)",
          (p1["header"]["scope_count"], p1["header"]["member_count"], p1["header"]["valid"])
          == (2, 2, True))

    # 2) 人工少写一条成员（须在镜像被 Type2 改写前做，保证批快照仍为 changed）
    #    → 乙：有跑次但 valid=false 且未开段
    os.environ["DETECT_COVERAGE_DROP"] = "SNAP-CHG-SOUTH-01"
    r3 = detect.run_detect(batch_id)
    del os.environ["DETECT_COVERAGE_DROP"]
    conn = connect()
    try:
        p3 = coverage_proof(conn, r3["detect_run_id"])
        run3 = conn.execute("SELECT * FROM detect_runs WHERE id=?", (r3["detect_run_id"],)).fetchone()
    finally:
        conn.close()
    check("缺员时仍落跑次头（乙口径）", run3 is not None)
    check("缺员跑次 valid=false", r3["valid"] is False and run3["valid"] == 0)
    check("缺员列表可查且含被漏快照", p3["missing_snapshot_ids"] == ["SNAP-CHG-SOUTH-01"],
          str(p3["missing_snapshot_ids"]))
    check("范围数=2 成员数=1", (p3["scope_count"], p3["member_count"]) == (2, 1))
    check("响应 changes 只含已落库成员（被漏快照不得借 changes 冒充）",
          [c["snapshot_id"] for c in r3["changes"]] == ["SNAP-CHG-EAST-01"]
          and r3["changed_count"] == 1, str(r3["changes"]))
    south_hist_before = hist_count("ST-SOUTH-01")
    gated = scd.apply_type2(r3["detect_run_id"])
    check("失败闭：禁止开段", gated["ok"] is False and gated.get("gated") == "coverage_invalid",
          json.dumps(gated, ensure_ascii=False))
    check("被闸后无新历史行", hist_count("ST-SOUTH-01") == south_hist_before)

    # 3) 全店最新范围：含变更与未变更的一批全覆盖，unchanged 不得开段
    r2 = detect.run_detect(None)
    conn = connect()
    try:
        p2 = coverage_proof(conn, r2["detect_run_id"])
    finally:
        conn.close()
    check("全店范围数=6", p2["scope_count"] == 6, str(p2["scope_count"]))
    check("全店成员数=6", p2["member_count"] == 6)
    check("changed=2 unchanged=4", p2["kind_counts"] == {"changed": 2, "unchanged": 4, "skipped": 0},
          str(p2["kind_counts"]))
    check("全店集合全等", p2["sets_equal"] is True and r2["valid"] is True)
    kinds = {m["snapshot_id"]: m["kind"] for m in p2["members"]}
    check("核证页列出每条成员 kind", set(kinds.values()) <= {"changed", "unchanged", "skipped"}
          and len(kinds) == 6)

    before_unchanged = hist_count("ST-WEST-01")  # unchanged 门店
    before_changed = hist_count("ST-EAST-01")
    apply_res = scd.apply_type2(r2["detect_run_id"])
    check("齐套跑次允许开段", apply_res["ok"] is True and apply_res["count"] == 2,
          json.dumps(apply_res, ensure_ascii=False))
    check("unchanged 门店未开新段", hist_count("ST-WEST-01") == before_unchanged)
    check("changed 门店开了新段", hist_count("ST-EAST-01") == before_changed + 1)

    # 4) 事后补成员不得把已失败跑次改成功（定稿不可翻案）
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO detect_run_members(run_id, snapshot_id, store_id, kind, row_hash, "
            "detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (r3["detect_run_id"], "SNAP-CHG-SOUTH-01", "ST-SOUTH-01", "changed",
             "x" * 16, "{}", now_iso()),
        )
        conn.commit()
        run3b, p3b = backfill_guard(conn, r3["detect_run_id"])
    finally:
        conn.close()
    check("补员后实时复算已齐套", p3b["recomputed_valid"] is True and p3b["sets_equal"] is True)
    check("跑次头 valid 仍为 false（不可翻案）", run3b["valid"] == 0 and p3b["valid"] is False)
    gated2 = scd.apply_type2(r3["detect_run_id"])
    check("补员后旧失败跑次仍禁止开段", gated2["ok"] is False)
    # 重跑产生新跑次才是正路
    r4 = detect.run_detect(batch_id)
    check("重跑得到新的齐套跑次", r4["valid"] is True and r4["detect_run_id"] != r3["detect_run_id"])

    # 5) 禁止范围外混入：手工注入范围外成员 → 核证失败且写闸不认
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO detect_run_members(run_id, snapshot_id, store_id, kind, row_hash, "
            "detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (r1["detect_run_id"], "SNAP-SEED-ST-NORTH-01", "ST-NORTH-01", "changed",
             "y" * 16, "{}", now_iso()),
        )
        conn.commit()
        p1b = coverage_proof(conn, r1["detect_run_id"])
    finally:
        conn.close()
    check("范围外成员被核证识别", p1b["extraneous_snapshot_ids"] == ["SNAP-SEED-ST-NORTH-01"])
    check("混入后复算 valid=false", p1b["recomputed_valid"] is False)
    north_before = hist_count("ST-NORTH-01")
    scd.apply_type2(r1["detect_run_id"])  # 头仍 valid=1，但 JOIN 范围表把混入行挡在门外
    check("范围外成员不得开段", hist_count("ST-NORTH-01") == north_before)

    # 6) skipped 成员：仍计入覆盖，且不开段
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO source_snapshots(id, batch_id, store_id, as_of_ts, weekday_mask, "
            "biz_status, payload_json, row_hash) VALUES (?,?,?,?,?,?,?,?)",
            ("SNAP-BAD-1", batch_id, "ST-NORTH-01", "2026-11-16T00:00:00Z", "1111100",
             "open", "{}", ""),
        )
        conn.commit()
    finally:
        conn.close()
    r5 = detect.run_detect(batch_id)
    conn = connect()
    try:
        p5 = coverage_proof(conn, r5["detect_run_id"])
    finally:
        conn.close()
    check("skipped 落成员且整次仍齐套",
          r5["valid"] is True and p5["kind_counts"]["skipped"] == 1
          and p5["sets_equal"] is True, str(p5["kind_counts"]))
    north_before2 = hist_count("ST-NORTH-01")
    scd.apply_type2(r5["detect_run_id"])
    check("skipped 不开段", hist_count("ST-NORTH-01") == north_before2)

    # 7) 并发两次同范围检测：互不串改，各跑次成员各自全等
    out = {}

    def worker(tag):
        out[tag] = detect.run_detect(batch_id)

    t1, t2 = threading.Thread(target=worker, args=("a",)), threading.Thread(target=worker, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    conn = connect()
    try:
        pa = coverage_proof(conn, out["a"]["detect_run_id"])
        pb = coverage_proof(conn, out["b"]["detect_run_id"])
    finally:
        conn.close()
    check("并发两跑次 id 不同", out["a"]["detect_run_id"] != out["b"]["detect_run_id"])
    check("跑次A成员未被跑次B串改", pa["sets_equal"] and pa["member_count"] == pa["scope_count"])
    check("跑次B成员未被跑次A串改", pb["sets_equal"] and pb["member_count"] == pb["scope_count"])

    # 8) 只读核证接口缺参/空库行为
    conn = connect()
    try:
        latest = coverage_proof(conn)  # 无 run_id → 最近跑次
        check("无参核证取最近跑次", latest["run_id"] is not None)
        try:
            coverage_proof(conn, "DET-NOPE")
            check("不存在跑次抛 RunNotFound", False)
        except RunNotFound:
            check("不存在跑次抛 RunNotFound", True)
    finally:
        conn.close()

    print()
    print("== 双架构口径对照（题面要求分别核对） ==")
    matrix = [
        ("人工少写一条成员", "甲：无成功跑次落库，拒绝原因+缺员列表另表可查，禁止开段",
         "乙：落跑次头但 valid=false，缺员列表在核证接口可查，禁止开段（本仓实现）"),
        ("含变更与未变更的一批", "甲：齐套才允许成功落跑，成员 changed/unchanged 全覆盖",
         "乙：落 valid=true 跑次，成员 changed/unchanged 全覆盖（本仓实现）"),
        ("事后补成员", "甲：重新检测成功后才占成功 run",
         "乙：旧失败跑次 valid 永不翻案，重跑产生新成功跑次（本仓实现）"),
        ("开段闸", "两架构共同：未齐套/缺员 → 永远禁止开 hist 段", "同左"),
    ]
    for scene, a, b in matrix:
        print(f"- {scene}\n    甲：{a}\n    乙：{b}")

    print()
    if FAILS:
        print(f"== {len(FAILS)} 项失败: {FAILS} ==")
        sys.exit(1)
    print("== 全部通过 ==")


if __name__ == "__main__":
    main()
