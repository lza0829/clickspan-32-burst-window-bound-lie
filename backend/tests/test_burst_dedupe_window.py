"""突发窗内同键去重 — 本地夹具验收（SQLite）。"""

import json

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.main import app
from app.modules import burst_dedupe_window as burst
from app.services import ingest as ingest_svc

FIXTURE = {
    "batch_id": "T-burst-1",
    "hits": [
        # 同键连点：首条入正式，其余窗内重复进搁置。
        {"id": "B1", "visitor_id": "V-1", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/p", "event_name": "click", "visitor_label": "甲"},
        {"id": "B2", "visitor_id": "V-1", "event_ts": "2026-11-17T09:00:01Z",
         "page_path": "/p", "event_name": "click"},
        {"id": "B3", "visitor_id": "V-1", "event_ts": "2026-11-17T09:00:30Z",
         "page_path": "/p", "event_name": "click"},
        # 越过窗（60s）后入正式并更新末击点。
        {"id": "B4", "visitor_id": "V-1", "event_ts": "2026-11-17T09:01:30Z",
         "page_path": "/p", "event_name": "click"},
        # 不同 event_name 属于不同键，独立计时。
        {"id": "B5", "visitor_id": "V-1", "event_ts": "2026-11-17T09:00:02Z",
         "page_path": "/p", "event_name": "view"},
        # 不同 page_path 亦为不同键。
        {"id": "B6", "visitor_id": "V-1", "event_ts": "2026-11-17T09:00:02Z",
         "page_path": "/q", "event_name": "click"},
        # 不同访客同键互不影响。
        {"id": "B7", "visitor_id": "V-2", "event_ts": "2026-11-17T09:00:01Z",
         "page_path": "/p", "event_name": "click", "visitor_label": "乙"},
    ],
}


@pytest.fixture()
def db():
    seed.init_db()
    conn = connect()
    conn.execute("DELETE FROM quarantine")
    conn.execute("DELETE FROM session_proofs")
    conn.execute("DELETE FROM hits")
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM visitors")
    conn.execute("DELETE FROM ingest_batches")
    conn.execute("DELETE FROM settings")
    conn.commit()
    conn.close()
    seed.init_db()
    yield
    conn = connect()
    conn.close()


def _rows(sql, params=()):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _one(sql, params=()):
    rows = _rows(sql, params)
    return rows[0] if rows else None


def test_window_config_persisted_in_settings_table(db):
    conn = connect()
    try:
        assert burst.burst_window_sec(conn) == 60
        assert conn.execute(
            "SELECT value FROM settings WHERE key='burst_window_sec'"
        ).fetchone()["value"] == "60"
        burst.set_burst_window_sec(conn, 90)
        conn.commit()
        assert burst.burst_window_sec(conn) == 90
    finally:
        conn.close()


def test_burst_fixture_only_first_formal_rest_held(db):
    res = ingest_svc.run_batch(FIXTURE)
    assert res["ok"], res
    report = res["report"]
    assert report["burst_window_sec"] == 60
    assert report["written"] == 5  # B1、B4、B5、B6、B7
    assert report["written"] + report["held"] + report["skipped"] == report["input"]
    assert sorted(report["held_hit_ids"]) == ["B2", "B3"]  # B7 是另一访客的首条

    formal = _rows(
        "SELECT id FROM hits WHERE visitor_id='V-1' "
        "AND page_path='/p' AND event_name='click' AND channel='primary' "
        "ORDER BY event_ts"
    )
    # 正式只留首条与越过窗的一条。
    assert [r["id"] for r in formal] == ["B1", "B4"]

    held = _rows(
        "SELECT reason, member_json FROM quarantine WHERE visitor_id='V-1' ORDER BY id"
    )
    assert [r["reason"] for r in held] == ["burst_window_dup", "burst_window_dup"]
    assert "B2" in held[0]["member_json"] and "B3" in held[1]["member_json"]

    # 末击点时钟与 hit_count 只随正式击点推进。
    sess = _one("SELECT * FROM sessions WHERE visitor_id='V-1'")
    assert sess["hit_count"] == 4  # B1 B4 B5 B6
    assert sess["ended_at"] == "2026-11-17T09:01:30Z"

    conn = connect()
    try:
        report = burst.proof(conn, visitor_id="V-1")
    finally:
        conn.close()
    assert report["ok"], report["anomalies"]
    assert report["summary"]["duplicates_within_window"] == 2
    group = [
        g for g in report["groups"]
        if g["page_path"] == "/p" and g["event_name"] == "click"
    ][0]
    assert [h["id"] for h in group["formal"]] == ["B1", "B4"]
    assert [h["hit_id"] for h in group["held"]] == ["B2", "B3"]


def test_held_hits_do_not_touch_clock_or_count(db):
    ingest_svc.run_batch({"batch_id": "t", "hits": [
        {"id": "C1", "visitor_id": "V-9", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/x", "event_name": "click"},
    ]})
    before = _one("SELECT * FROM sessions WHERE visitor_id='V-9'")
    ingest_svc.run_batch({"batch_id": "t2", "hits": [
        {"id": "C2", "visitor_id": "V-9", "event_ts": "2026-11-17T09:00:05Z",
         "page_path": "/x", "event_name": "click"},
        {"id": "C3", "visitor_id": "V-9", "event_ts": "2026-11-17T09:00:55Z",
         "page_path": "/x", "event_name": "click"},
    ]})
    after = _one("SELECT * FROM sessions WHERE visitor_id='V-9'")
    assert before["hit_count"] == after["hit_count"] == 1
    assert before["ended_at"] == after["ended_at"] == "2026-11-17T09:00:00Z"
    assert _one("SELECT COUNT(*) AS c FROM hits WHERE visitor_id='V-9'")["c"] == 1
    assert _one("SELECT COUNT(*) AS c FROM quarantine WHERE visitor_id='V-9'")["c"] == 2


def test_replay_of_held_hit_is_idempotent(db):
    ingest_svc.run_batch({"batch_id": "t", "hits": [
        {"id": "E1", "visitor_id": "V-8", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/x", "event_name": "click"},
        {"id": "E2", "visitor_id": "V-8", "event_ts": "2026-11-17T09:00:01Z",
         "page_path": "/x", "event_name": "click"},
    ]})
    second = ingest_svc.run_batch({"batch_id": "t2", "hits": [
        {"id": "E2", "visitor_id": "V-8", "event_ts": "2026-11-17T09:00:01Z",
         "page_path": "/x", "event_name": "click"},
    ]})
    assert second["report"]["skipped"] == 1
    assert _one("SELECT COUNT(*) AS c FROM quarantine WHERE visitor_id='V-8'")["c"] == 1


def test_window_boundary_is_inclusive(db):
    conn = connect()
    try:
        burst.set_burst_window_sec(conn, 10)
        conn.commit()
    finally:
        conn.close()
    res = ingest_svc.run_batch({"batch_id": "b", "hits": [
        {"id": "F1", "visitor_id": "V-7", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/x", "event_name": "click"},
        {"id": "F2", "visitor_id": "V-7", "event_ts": "2026-11-17T09:00:10Z",
         "page_path": "/x", "event_name": "click"},  # 恰为窗沿 -> 去重
        {"id": "F3", "visitor_id": "V-7", "event_ts": "2026-11-17T09:00:11Z",
         "page_path": "/x", "event_name": "click"},
    ]})
    assert res["report"]["held_hit_ids"] == ["F2"]


def test_late_arrival_inside_window_is_held(db):
    """迟到击点（早于已入正式成员）若落在窗内，同样按窗内重复搁置。"""
    ingest_svc.run_batch({"batch_id": "late", "hits": [
        {"id": "L2", "visitor_id": "V-4", "event_ts": "2026-11-17T09:00:30Z",
         "page_path": "/x", "event_name": "click"},
        {"id": "L1", "visitor_id": "V-4", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/x", "event_name": "click"},
    ]})
    pair = _rows(
        "SELECT id, event_ts FROM hits WHERE visitor_id='V-4' ORDER BY event_ts"
    )
    # 批内按 event_ts 排序，首条为最早者，迟到一条进搁置。
    assert [r["id"] for r in pair] == ["L1"]
    held = _one(
        "SELECT member_json FROM quarantine WHERE visitor_id='V-4'"
    )
    assert json.loads(held["member_json"])["hit_id"] == "L2"

    # 跨批迟到：已有正式成员后补一条更早、且落在窗内的击点，亦进搁置。
    ingest_svc.run_batch({"batch_id": "late2", "hits": [
        {"id": "L4", "visitor_id": "V-4", "event_ts": "2026-11-17T09:00:45Z",
         "page_path": "/x", "event_name": "click"},
        {"id": "L3", "visitor_id": "V-4", "event_ts": "2026-11-17T09:00:44Z",
         "page_path": "/x", "event_name": "click"},
    ]})
    conn = connect()
    try:
        proof = burst.proof(conn, visitor_id="V-4")
    finally:
        conn.close()
    assert proof["ok"], proof["anomalies"]
    ids = [r["id"] for r in _rows("SELECT id FROM hits WHERE visitor_id='V-4'")]
    assert "L3" not in ids


def test_unverifiable_timestamp_fails_closed(db):
    res = ingest_svc.run_batch({"batch_id": "u", "hits": [
        {"id": "G1", "visitor_id": "V-6", "event_ts": "not-a-time",
         "page_path": "/x", "event_name": "click"},
    ]})
    assert res["ok"]
    assert res["report"]["written"] == 0
    assert res["report"]["held"] == 1
    assert _one("SELECT reason FROM quarantine WHERE visitor_id='V-6'")["reason"] == (
        "burst_window_unverifiable"
    )
    conn = connect()
    try:
        proof = burst.proof(conn)
    finally:
        conn.close()
    assert proof["ok"] is False
    assert any(a["code"] == "unverifiable_hold" for a in proof["anomalies"])


def test_proof_detects_duplicate_admitted(db):
    """若有人绕过入口直接写入窗内重复，核证须报出。"""
    ingest_svc.run_batch({"batch_id": "t", "hits": [
        {"id": "P1", "visitor_id": "V-5", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/x", "event_name": "click"},
    ]})
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)"
            " SELECT 'P2', visitor_id, session_id, '2026-11-17T09:00:05Z', page_path, event_name, 'primary'"
            " FROM hits WHERE id='P1'"
        )
        conn.commit()
        proof = burst.proof(conn, visitor_id="V-5")
    finally:
        conn.close()
    assert proof["ok"] is False
    codes = {a["code"] for a in proof["anomalies"]}
    assert "duplicate_admitted" in codes


def test_proof_uses_admission_snapshot_not_current_window(db):
    """放宽窗后，旧正式数据不应被当前窗误报；按快照仍能识别。"""
    ingest_svc.run_batch({"batch_id": "w", "hits": [
        {"id": "W1", "visitor_id": "V-3", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/x", "event_name": "click"},
        {"id": "W2", "visitor_id": "V-3", "event_ts": "2026-11-17T09:01:40Z",
         "page_path": "/x", "event_name": "click"},
    ]})  # 窗 60s：间距 100s 越窗入正式，快照 60
    conn = connect()
    try:
        burst.set_burst_window_sec(conn, 5)
        conn.commit()
        proof = burst.proof(conn, visitor_id="V-3")
    finally:
        conn.close()
    # 当前窗 5s、快照 60s，间距 100s 均合法。
    assert proof["ok"], proof["anomalies"]
    assert proof["burst_window_sec"] == 5

    conn = connect()
    try:
        conn.execute("UPDATE hits SET dedupe_window_sec=120 WHERE id IN ('W1','W2')")
        conn.commit()
        proof2 = burst.proof(conn, visitor_id="V-3")
    finally:
        conn.close()
    assert proof2["ok"] is False
    codes = {a["code"] for a in proof2["anomalies"]}
    assert "duplicate_admitted_under_snapshot" in codes


def test_legacy_db_gets_snapshot_column(db):
    """旧库（无 dedupe_window_sec 列）启动时自动补列。"""
    import sqlite3

    from app import seed as seed_mod

    conn = connect()
    try:
        conn.execute("ALTER TABLE hits DROP COLUMN dedupe_window_sec")
        conn.commit()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(hits)")}
        assert "dedupe_window_sec" not in cols
    finally:
        conn.close()

    seed_mod.init_db()

    conn = connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(hits)")}
        assert "dedupe_window_sec" in cols
    finally:
        conn.close()


def test_proof_is_read_only(db):
    ingest_svc.run_batch(FIXTURE)
    conn = connect()
    try:
        before = {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("hits", "quarantine", "sessions", "session_proofs", "ingest_batches")
        }
        burst.proof(conn)
        after = {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("hits", "quarantine", "sessions", "session_proofs", "ingest_batches")
        }
    finally:
        conn.close()
    assert before == after


def test_api_settings_and_proof(db):
    client = TestClient(app)
    with client:
        assert client.put("/api/settings/burst-window", json={"burst_window_sec": 120}).status_code == 200
        assert client.get("/api/settings").json()["burst_window_sec"] == 120

        run = client.post("/api/ingest/run", json=FIXTURE).json()
        assert run["ok"]
        assert run["report"]["burst_window_sec"] == 120

        proof = client.get("/api/burst-dedupe/proof").json()
        assert proof["read_only"] is True
        assert proof["ok"] is True
        assert proof["summary"]["duplicates_within_window"] >= 2
        assert proof["window_config_form"] == "settings_table"

        bad = client.put("/api/settings/burst-window", json={"burst_window_sec": 0})
        assert bad.status_code == 400


def test_fixture_file_end_to_end(db):
    """验收用的本地夹具：窗内连点只留首条，其余可在搁置区查到。"""
    res = ingest_svc.run_batch()
    assert res["ok"], res
    assert res["report"]["batch_id"] == "B-demo-001"

    formal = _rows(
        "SELECT id FROM hits WHERE visitor_id='V-1001' AND page_path='/promo' "
        "AND event_name='click' AND channel='primary' ORDER BY event_ts, id"
    )
    assert [r["id"] for r in formal] == ["H9", "H13"]

    held = _rows(
        "SELECT member_json FROM quarantine WHERE visitor_id='V-1001' ORDER BY id"
    )
    held_ids = {json.loads(r["member_json"])["hit_id"] for r in held}
    assert held_ids == {"H10", "H11", "H12"}

    # 正式只留首条后，后续正式成员仍按同一会话挂载（同一 open 会话）。
    sessions = _rows(
        "SELECT DISTINCT session_id FROM hits WHERE visitor_id='V-1001'"
    )
    assert len(sessions) == 1

    conn = connect()
    try:
        proof = burst.proof(conn, visitor_id="V-1001")
    finally:
        conn.close()
    assert proof["ok"], proof["anomalies"]
    group = [
        g for g in proof["groups"]
        if g["page_path"] == "/promo" and g["event_name"] == "click"
    ][0]
    assert [h["id"] for h in group["formal"]] == ["H9", "H13"]
    assert [h["hit_id"] for h in group["held"]] == ["H10", "H11", "H12"]
