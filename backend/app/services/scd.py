import json
from uuid import uuid4

from app.db import connect
from app.modules.detect_coverage_member_proof import (
    RunNotFound,
    assert_open_segment_allowed,
)
from app.seed import now_iso


def apply_type2(detect_run_id: str):
    """对覆盖齐套（valid=1）跑次的 changed 成员关旧开新；刷新当前镜。

    历史维写闸（detect_coverage_member_proof 架构乙）：
    - 跑次 valid=false（缺员/范围外混入）一律拒绝开段；
    - 只处理成员表中的 changed 成员，unchanged/skipped 永不开段；
    - 成员必须属于本跑次范围，禁止范围外混入。
    """
    conn = connect()
    now = now_iso()
    try:
        try:
            assert_open_segment_allowed(conn, detect_run_id)
        except RunNotFound as exc:
            return {"ok": False, "message": str(exc)}
        except PermissionError as exc:
            return {"ok": False, "message": str(exc), "gated": "coverage_invalid"}

        # 权威变更来源 = 成员表的 changed 行（不再只读 report_json 的 changes）
        changed_members = conn.execute(
            """
            SELECT m.* FROM detect_run_members m
            JOIN detect_run_scopes sc
              ON sc.run_id=m.run_id AND sc.snapshot_id=m.snapshot_id
            WHERE m.run_id=? AND m.kind='changed'
            ORDER BY m.store_id, m.snapshot_id
            """,
            (detect_run_id,),
        ).fetchall()
        applied = []
        for m in changed_members:
            snap = conn.execute(
                "SELECT * FROM source_snapshots WHERE id=?", (m["snapshot_id"],)
            ).fetchone()
            if not snap:
                continue
            store_id = snap["store_id"]
            cur_hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
                (store_id,),
            ).fetchone()
            valid_from = snap["as_of_ts"]
            if cur_hist:
                conn.execute(
                    """UPDATE dim_store_hours_hist
                       SET valid_to=?, is_current=0, closed_by_run=?
                       WHERE id=?""",
                    (valid_from, detect_run_id, cur_hist["id"]),
                )
            new_id = f"HIST-{uuid4().hex[:10]}"
            rh = snap["row_hash"]
            conn.execute(
                """INSERT INTO dim_store_hours_hist(
                  id, store_id, open_time, close_time, weekday_mask, biz_status,
                  valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id,
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    valid_from,
                    None,
                    1,
                    snap["id"],
                    rh,
                    None,
                ),
            )
            conn.execute(
                """INSERT INTO dim_store_hours_current(
                  store_id, open_time, close_time, weekday_mask, biz_status,
                  hist_id, as_of_ts, row_hash
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(store_id) DO UPDATE SET
                  open_time=excluded.open_time,
                  close_time=excluded.close_time,
                  weekday_mask=excluded.weekday_mask,
                  biz_status=excluded.biz_status,
                  hist_id=excluded.hist_id,
                  as_of_ts=excluded.as_of_ts,
                  row_hash=excluded.row_hash
                """,
                (
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    new_id,
                    valid_from,
                    rh,
                ),
            )
            applied.append({"store_id": store_id, "hist_id": new_id, "closed": cur_hist["id"] if cur_hist else None})
        conn.commit()
        return {"ok": True, "applied": applied, "count": len(applied), "detect_run_id": detect_run_id}
    finally:
        conn.close()


def rebuild_proofs():
    """Attest: each current mirror hist_id must point to is_current=1 hist with same hash."""
    conn = connect()
    now = now_iso()
    try:
        mirrors = conn.execute("SELECT * FROM dim_store_hours_current ORDER BY store_id").fetchall()
        results = []
        for m in mirrors:
            hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (m["hist_id"],)
            ).fetchone()
            valid = 1
            detail = {}
            if not hist:
                valid = 0
                detail["error"] = "mirror_hist_missing"
            elif hist["is_current"] != 1:
                valid = 0
                detail["error"] = "mirror_not_current_hist"
            elif hist["row_hash"] != m["row_hash"]:
                valid = 0
                detail["error"] = "hash_mismatch"
            elif hist["store_id"] != m["store_id"]:
                valid = 0
                detail["error"] = "store_mismatch"
            else:
                detail["ok"] = True
            conn.execute(
                """INSERT INTO scd_proofs(
                  store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    m["store_id"],
                    hist["id"] if hist else "",
                    m["hist_id"],
                    m["row_hash"],
                    valid,
                    json.dumps(detail, ensure_ascii=False),
                    now,
                ),
            )
            results.append({"store_id": m["store_id"], "valid": bool(valid), "detail": detail})
        conn.commit()
        return {
            "checked": len(results),
            "failed": sum(1 for r in results if not r["valid"]),
            "results": results,
        }
    finally:
        conn.close()
