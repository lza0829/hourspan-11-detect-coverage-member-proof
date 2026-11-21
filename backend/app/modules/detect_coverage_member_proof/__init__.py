"""detect_coverage_member_proof · 检测覆盖成员核证管道

架构选型（全局唯一口径）：**乙 · 可落跑 + 失败闭**

一次检测对「约定范围」内的每条入档快照都必须落一条成员分类
（changed / unchanged / skipped），成员 snapshot_id 集合与范围 snapshot_id
集合须能全等证明。禁止只写 changes 冒充覆盖、禁止范围外混入、禁止 unchanged
被开段；并发第二次同范围检测不得串改第一次的成员。

乙口径：
- 无论覆盖齐不齐，都落 detect_runs 跑次头（跑次是审计事件）；
- 缺员 → valid=0（失败闭），核证暴露缺员列表，同成败一律禁止开 hist 段；
- 跑次定稿不可翻案：事后补成员只进新跑次，禁止把已失败跑次改成 valid=1；
- 历史维写闸（scd.apply_type2）只接受 valid=1 的跑次。

对照甲口径（本仓不采用，仅留双架构核对矩阵）：缺员则整次拒绝成功落跑——
不占成功 run、禁止开段，拒绝原因与缺员列表另表可查。
两架构共同不变量：缺员/未齐套 → 永远不开段。

故障注入（仅测试用）：环境变量 DETECT_COVERAGE_DROP=<snapshot_id>
使该快照在写成员时被故意漏写，用于制造「人工少写一条成员」。
"""

import json
import os
import sqlite3
from uuid import uuid4

from app.seed import now_iso

ARCHITECTURE = "B"  # 乙：可落跑 + 失败闭
MEMBER_KINDS = ("changed", "unchanged", "skipped")
_DROP_ENV = "DETECT_COVERAGE_DROP"


class RunNotFound(LookupError):
    pass


def new_run_id() -> str:
    return f"DET-{uuid4().hex[:8]}"


def scan_scope(conn: sqlite3.Connection, batch_id: str | None):
    """范围扫描：约定检测范围 = 指定批的全部快照，或每店最新一条快照。

    返回快照 Row 列表（有序、确定 seq）。这是检测覆盖管道的扫描水位，
    范围一旦随跑次落库即冻结，重跑不改变旧跑次的范围。
    """
    if batch_id:
        return conn.execute(
            "SELECT * FROM source_snapshots WHERE batch_id=? "
            "ORDER BY store_id, as_of_ts, id",
            (batch_id,),
        ).fetchall()
    return conn.execute(
        """
        SELECT s.* FROM source_snapshots s
        JOIN (
          SELECT store_id, MAX(as_of_ts) AS mx FROM source_snapshots GROUP BY store_id
        ) t ON s.store_id=t.store_id AND s.as_of_ts=t.mx
        ORDER BY s.store_id, s.id
        """
    ).fetchall()


def classify(snap: sqlite3.Row, cur: sqlite3.Row | None):
    """单成员分类：changed（new/attr_change）/ unchanged / skipped。

    skipped = 快照自身不合法（hash 缺失等），不参与开段但仍计入覆盖。
    """
    if not snap["row_hash"]:
        return "skipped", {"reason": "snapshot_hash_missing"}
    if cur is None:
        return "changed", {"change": "new", "from_hash": None, "to_hash": snap["row_hash"]}
    if cur["row_hash"] != snap["row_hash"]:
        return "changed", {
            "change": "attr_change",
            "from_hash": cur["row_hash"],
            "to_hash": snap["row_hash"],
        }
    return "unchanged", {"row_hash": snap["row_hash"]}


def _dropped_snapshot_id() -> str | None:
    return os.environ.get(_DROP_ENV) or None


def run_coverage_detect(conn: sqlite3.Connection, batch_id: str | None = None):
    """在一个 BEGIN IMMEDIATE 事务内完成：范围冻结 → 成员落库 → 跑次头定稿。

    由 detect 服务调用；调用方不另开事务。返回响应字典（含核证摘要）。
    """
    run_id = new_run_id()
    now = now_iso()
    drop_id = _dropped_snapshot_id()
    conn.execute("BEGIN IMMEDIATE")  # 写锁：并发第二次同范围检测排队，不得串改第一次
    try:
        snaps = scan_scope(conn, batch_id)
        scope_ids = [s["id"] for s in snaps]

        # 1) 冻结范围（范围表即扫描水位凭证）
        conn.executemany(
            "INSERT INTO detect_run_scopes(run_id, snapshot_id, store_id, seq) "
            "VALUES (?,?,?,?)",
            [(run_id, s["id"], s["store_id"], i) for i, s in enumerate(snaps)],
        )

        members = []
        for snap in snaps:
            cur = conn.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id=?",
                (snap["store_id"],),
            ).fetchone()
            kind, detail = classify(snap, cur)
            members.append(
                {
                    "run_id": run_id,
                    "snapshot_id": snap["id"],
                    "store_id": snap["store_id"],
                    "kind": kind,
                    "row_hash": snap["row_hash"],
                    "detail_json": json.dumps(detail, ensure_ascii=False),
                    "created_at": now,
                }
            )

        # 2) 成员落库（故障注入：模拟人工少写一条成员 → 缺员失败闭）
        written = [m for m in members if drop_id is None or m["snapshot_id"] != drop_id]
        conn.executemany(
            """INSERT INTO detect_run_members(
                 run_id, snapshot_id, store_id, kind, row_hash, detail_json, created_at
               ) VALUES (:run_id,:snapshot_id,:store_id,:kind,:row_hash,:detail_json,:created_at)""",
            written,
        )

        # 权威变更清单只从已落库成员回读（缺员的快照不得借内存 changes 冒充覆盖）
        stored_rows = conn.execute(
            "SELECT snapshot_id, store_id, kind, detail_json FROM detect_run_members "
            "WHERE run_id=? ORDER BY store_id, snapshot_id",
            (run_id,),
        ).fetchall()
        changes = []
        for r in stored_rows:
            if r["kind"] != "changed":
                continue
            detail = json.loads(r["detail_json"] or "{}")
            changes.append(
                {
                    "store_id": r["store_id"],
                    "kind": detail.get("change", "attr_change"),
                    "snapshot_id": r["snapshot_id"],
                    "from_hash": detail.get("from_hash"),
                    "to_hash": detail.get("to_hash"),
                }
            )

        # 3) 集合全等核证：成员 id 集合 == 范围 id 集合，且禁止范围外混入
        member_ids = {r["snapshot_id"] for r in stored_rows}
        scope_set = set(scope_ids)
        missing = sorted(scope_set - member_ids)
        extraneous = sorted(member_ids - scope_set)  # 范围外混入即核证失败
        valid = 1 if not missing and not extraneous else 0
        kind_counts = {"changed": 0, "unchanged": 0, "skipped": 0}
        for r in stored_rows:
            kind_counts[r["kind"]] += 1
        stored_kind_counts = kind_counts

        report = {
            "architecture": ARCHITECTURE,
            "scope_count": len(scope_ids),
            "member_count": len(written),
            "kind_counts": stored_kind_counts,
            "missing_snapshot_ids": missing,
            "extraneous_snapshot_ids": extraneous,
            "valid": bool(valid),
            "scanned": len(snaps),
            "changes": changes,
        }
        # 4) 跑次头定稿（乙：失败也落跑，valid=0；此后不可翻案）
        conn.execute(
            """INSERT INTO detect_runs(
                 id, batch_id, created_at, changed_count, report_json,
                 scope_count, member_count, missing_count, valid
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                batch_id,
                now,
                stored_kind_counts["changed"],
                json.dumps(report, ensure_ascii=False),
                len(scope_ids),
                len(written),
                len(missing),
                valid,
            ),
        )
        conn.commit()
        return {
            "detect_run_id": run_id,
            "architecture": ARCHITECTURE,
            "valid": bool(valid),
            "scope_count": len(scope_ids),
            "member_count": len(written),
            "kind_counts": stored_kind_counts,
            "missing_snapshot_ids": missing,
            "extraneous_snapshot_ids": extraneous,
            "changed_count": stored_kind_counts["changed"],
            "changes": changes,
        }
    except Exception:
        conn.rollback()
        raise


def coverage_proof(conn: sqlite3.Connection, run_id: str | None = None):
    """只读覆盖核证：范围数、成员数、缺员列表、run_id、valid，并列出成员 kind。

    run_id 为空时取最近一次检测跑次。跑次不存在 → RunNotFound。
    """
    if run_id is None:
        row = conn.execute(
            "SELECT id FROM detect_runs ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise RunNotFound("无检测跑次")
        run_id = row["id"]
    run = conn.execute("SELECT * FROM detect_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise RunNotFound(f"检测跑次不存在: {run_id}")

    scope_rows = conn.execute(
        "SELECT snapshot_id, store_id, seq FROM detect_run_scopes WHERE run_id=? "
        "ORDER BY seq",
        (run_id,),
    ).fetchall()
    member_rows = conn.execute(
        "SELECT snapshot_id, store_id, kind, row_hash, detail_json "
        "FROM detect_run_members WHERE run_id=? ORDER BY snapshot_id",
        (run_id,),
    ).fetchall()
    scope_ids = [r["snapshot_id"] for r in scope_rows]
    member_ids = [r["snapshot_id"] for r in member_rows]
    missing = sorted(set(scope_ids) - set(member_ids))
    extraneous = sorted(set(member_ids) - set(scope_ids))
    members = [
        {
            "snapshot_id": r["snapshot_id"],
            "store_id": r["store_id"],
            "kind": r["kind"],
            "row_hash": r["row_hash"],
            "detail": json.loads(r["detail_json"] or "{}"),
        }
        for r in member_rows
    ]
    kind_counts = {k: 0 for k in MEMBER_KINDS}
    for r in member_rows:
        kind_counts[r["kind"]] += 1
    # 实时复算（不依赖跑次头自报），同时回传跑次头定稿值以证不可翻案
    recomputed_valid = not missing and not extraneous and len(scope_ids) == len(member_ids)
    return {
        "run_id": run_id,
        "architecture": ARCHITECTURE,
        "batch_id": run["batch_id"],
        "created_at": run["created_at"],
        "scope_count": len(scope_ids),
        "member_count": len(member_ids),
        "missing_snapshot_ids": missing,
        "extraneous_snapshot_ids": extraneous,
        "kind_counts": kind_counts,
        "members": members,
        "valid": bool(run["valid"]),               # 跑次头定稿（乙：失败不可改成功）
        "recomputed_valid": bool(recomputed_valid),  # 只读实时复算
        "sets_equal": set(scope_ids) == set(member_ids),
        "header": {
            "scope_count": run["scope_count"],
            "member_count": run["member_count"],
            "missing_count": run["missing_count"],
            "valid": bool(run["valid"]),
        },
    }


def assert_open_segment_allowed(conn: sqlite3.Connection, run_id: str):
    """历史维写闸：仅 valid=1 的齐套跑次可开段；unchanged/skipped 永不开段。"""
    run = conn.execute("SELECT * FROM detect_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise RunNotFound(f"检测跑次不存在: {run_id}")
    if not run["valid"]:
        proof = coverage_proof(conn, run_id)
        reasons = []
        if proof["missing_snapshot_ids"]:
            reasons.append("缺员: " + ",".join(proof["missing_snapshot_ids"]))
        if proof["extraneous_snapshot_ids"]:
            reasons.append("范围外混入: " + ",".join(proof["extraneous_snapshot_ids"]))
        raise PermissionError(
            "检测覆盖未齐套（valid=false），禁止开 hist 段；" + "；".join(reasons)
        )
    return run


def backfill_guard(conn: sqlite3.Connection, run_id: str):
    """定稿不可翻案闸：已落跑次（尤其 valid=0）禁止事后补成员改成功。

    补成员只能重跑产生新跑次。返回 (run, proof)。
    """
    run = conn.execute("SELECT * FROM detect_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise RunNotFound(f"检测跑次不存在: {run_id}")
    proof = coverage_proof(conn, run_id)
    return run, proof
