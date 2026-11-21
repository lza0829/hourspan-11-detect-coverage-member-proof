from app.db import connect
from app.modules.detect_coverage_member_proof import run_coverage_detect


def run_detect(batch_id: str | None = None):
    """变更检测（检测覆盖管道）。

    不再只吐 changes：约定范围内每条入档快照都落成员分类
    changed/unchanged/skipped，成员集合与范围集合全等核证。
    架构乙：缺员也落跑次头但 valid=false，禁止开段。
    """
    conn = connect()
    try:
        return run_coverage_detect(conn, batch_id)
    finally:
        conn.close()
