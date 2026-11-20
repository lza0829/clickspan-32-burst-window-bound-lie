from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules import burst_dedupe_window as burst
from app.seed import idle_timeout_sec
from app.services import ingest as ingest_svc
from app.services import sessions as sess_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "visitors": conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"],
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "hits_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE channel='primary'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
            "burst_held": conn.execute(
                "SELECT COUNT(*) AS c FROM quarantine WHERE reason IN (?, ?)",
                (burst.REASON_DUPLICATE, burst.REASON_UNVERIFIABLE),
            ).fetchone()["c"],
            "idle_timeout_sec": idle_timeout_sec(conn),
            "burst_window_sec": burst.burst_window_sec(conn),
        }
    finally:
        conn.close()


@router.get("/visitors")
def list_visitors():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM visitors ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sessions")
def list_sessions(visitor_id: str | None = None):
    conn = connect()
    try:
        return sess_svc.list_sessions(conn, visitor_id)
    finally:
        conn.close()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = connect()
    try:
        detail = sess_svc.session_detail(conn, session_id)
        if not detail:
            raise HTTPException(404, "会话不存在")
        return detail
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, visitor_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = None):
    """省略 body 时读夹具 ``ingest_batch.json``。"""
    return ingest_svc.run_batch(body)


@router.get("/session-proofs")
def session_proofs(session_id: str | None = None):
    conn = connect()
    try:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM session_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {
            "idle_timeout_sec": idle_timeout_sec(conn),
            "burst_window_sec": burst.burst_window_sec(conn),
            "burst_window_config_form": burst.WINDOW_CONFIG_FORM,
        }
    finally:
        conn.close()


@router.put("/settings/idle-timeout")
def put_idle_timeout(body: dict):
    sec = int(body.get("idle_timeout_sec", 1800))
    if sec < 60:
        raise HTTPException(400, "空闲超时至少 60 秒")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            (str(sec),),
        )
        conn.commit()
        return {"idle_timeout_sec": sec}
    finally:
        conn.close()


@router.put("/settings/burst-window")
def put_burst_window(body: dict):
    conn = connect()
    try:
        try:
            sec = burst.set_burst_window_sec(conn, body.get("burst_window_sec"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"突发窗秒非法: {exc}")
        conn.commit()
        return {
            "burst_window_sec": sec,
            "burst_window_config_form": burst.WINDOW_CONFIG_FORM,
        }
    finally:
        conn.close()


@router.get("/burst-dedupe/proof")
def burst_dedupe_proof(visitor_id: str | None = None):
    """突发去重核证：只读，不写库。

    协同：收流正式集 / 搁置重复集 / 会话末击点时钟 / hit_count。
    """
    conn = connect()
    try:
        return burst.proof(conn, visitor_id=visitor_id)
    finally:
        conn.close()


@router.get("/burst-dedupe/window-proof")
def burst_dedupe_window_proof(
    batch_id: str | None = None, visitor_id: str | None = None
):
    """窗界核证（只读，基线所无）：窗界 / skipped 集合 / 结论。

    - 去重成功时窗界必须覆盖全部 ``skipped_keys``；
    - 证明键集合 = 实际跳过集合；
    - 旧证明窗界不可被新配置回溯改写。

    本接口不写库；可传 ``?batch_id=`` / ``?visitor_id=`` 过滤。
    """
    conn = connect()
    try:
        return burst.window_proof(conn, batch_id=batch_id, visitor_id=visitor_id)
    finally:
        conn.close()


@router.get("/burst-dedupe/config")
def burst_dedupe_config():
    conn = connect()
    try:
        return {
            "burst_window_sec": burst.burst_window_sec(conn),
            "config_key": burst.SETTINGS_KEY,
            "window_config_form": burst.WINDOW_CONFIG_FORM,
            "approach": burst.APPROACH,
            "status": burst.STATUS,
        }
    finally:
        conn.close()


def _view_compat_payload(payload: dict) -> dict:
    """读路径字段别名兼容，可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "hit_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("hit_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "gap_sec" in out and "gap" not in out:
        out["gap"] = out.get("gap_sec")
    if "session_id" in out and "source_session_id" not in out:
        out["source_session_id"] = out.get("session_id")
    if "verified" not in out:
        out["verified"] = True
    return out


def _strand_read_fallback(rows: list) -> list:
    """珠串读路径保留全量点，由页面自行分段。"""
    return list(rows or [])


def _audit_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("aligned", True)
    data.setdefault("writable", True)
    return data

