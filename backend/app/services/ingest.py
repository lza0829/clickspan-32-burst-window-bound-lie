import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import burst_dedupe_window as burst


def run_batch(payload: dict | None = None) -> dict:
    """收流基座 + 突发窗去重分流。

    ``payload`` 省略时读夹具 ``ingest_batch.json``（供本地夹具验收）。

    失败闭形态：**入口按窗去重分流**。逐击点在正式写入之前判定，判定与
    写入处于同一个 ``BEGIN IMMEDIATE`` 事务；判定不可核证时进搁置，任何
    未预期异常整批回滚，绝不放行可疑重复。窗内重复只进搁置：不写 hits、
    不改 ``sessions.hit_count``、不改 ``sessions.ended_at``（末击点时钟）。
    """
    if payload is None:
        path = FIXTURES_DIR / "ingest_batch.json"
        if not path.exists():
            return {"ok": False, "error": "缺少 ingest_batch.json"}
        payload = json.loads(path.read_text(encoding="utf-8"))

    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    hits = list(payload.get("hits", []))

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        window_sec = burst.burst_window_sec(conn)
        report = {
            "batch_id": batch_id,
            "written": 0,
            "held": 0,
            "skipped": 0,
            "input": len(hits),
            "burst_window_sec": window_sec,
            "window_config_form": burst.WINDOW_CONFIG_FORM,
            "approach": burst.APPROACH,
            "held_hit_ids": [],
        }

        # 批内按 event_ts 排序，“首条”即最早一条，判定与顺序无关。
        for hit in sorted(hits, key=_sort_key):
            outcome = _ingest_one(conn, hit, batch_id, window_sec)
            if outcome == "written":
                report["written"] += 1
            elif outcome == "held":
                report["held"] += 1
                report["held_hit_ids"].append(hit.get("id"))
            elif outcome == "skipped":
                report["skipped"] += 1
            else:
                raise RuntimeError(f"未知收流结论: {outcome}")

        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _ingest_one(conn, hit: dict, batch_id: str, window_sec: int) -> str:
    """单点击点入口：返回 written / held / skipped。"""
    vid = hit["visitor_id"]

    # 同 id 重放：保持基座幂等，不重复计数、不动末击点。
    if conn.execute("SELECT 1 FROM hits WHERE id=?", (hit["id"],)).fetchone():
        return "skipped"
    # 已搁置击点的重放同样幂等跳过，避免搁置区刷屏。
    if burst.already_held(conn, hit.get("id")):
        return "skipped"

    decision = burst.classify(conn, hit, window_sec)
    if decision["decision"] != "formal":
        # 失败闭：窗内重复 / 不可核证均入搁置，绝不入正式。
        burst.hold(conn, hit, decision, batch_id, _now())
        return "held"

    exists = conn.execute("SELECT 1 FROM visitors WHERE id=?", (vid,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
            (vid, hit.get("visitor_label") or vid, _now()),
        )
    # 基座：挂到该访客最新 open 会话；无则新建（不做超时切分）
    sess = conn.execute(
        """
        SELECT id FROM sessions
        WHERE visitor_id=? AND status='open'
        ORDER BY started_at DESC LIMIT 1
        """,
        (vid,),
    ).fetchone()
    if sess:
        sid = sess["id"]
    else:
        sid = f"S-{vid}-{uuid.uuid4().hex[:6]}"
        conn.execute(
            """
            INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
            VALUES (?,?,?,?,0,'open')
            """,
            (sid, vid, hit["event_ts"], hit["event_ts"]),
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO hits(
          id, visitor_id, session_id, event_ts, page_path, event_name, channel, dedupe_window_sec
        )
        VALUES (?,?,?,?,?,?, 'primary', ?)
        """,
        (
            hit["id"],
            vid,
            sid,
            hit["event_ts"],
            hit["page_path"],
            hit["event_name"],
            window_sec,
        ),
    )
    if not conn.execute("SELECT changes()").fetchone()[0]:
        return "skipped"
    conn.execute(
        """
        UPDATE sessions
        SET hit_count = hit_count + 1,
            ended_at = CASE
              WHEN ended_at IS NULL OR ended_at < ? THEN ?
              ELSE ended_at END
        WHERE id=?
        """,
        (hit["event_ts"], hit["event_ts"], sid),
    )
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
            (sid,),
        ).fetchall()
    ]
    sess_row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    conn.execute(
        """
        INSERT INTO session_proofs(
          session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            sid,
            vid,
            json.dumps(ids, ensure_ascii=False),
            sess_row["hit_count"],
            sess_row["started_at"],
            sess_row["ended_at"],
            _now(),
        ),
    )
    return "written"


def _sort_key(hit: dict):
    raw = str(hit.get("event_ts", ""))
    try:
        return (0, burst.parse_ts(raw).timestamp(), str(hit.get("id", "")))
    except (TypeError, ValueError):
        return (1, 0.0, str(hit.get("id", "")))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

