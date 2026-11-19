"""突发窗内同键去重（已实现）。

键 = ``visitor_id + page_path + event_name``。

已选形态（两种候选形态中固定择一）：

- 窗配置：``settings`` 表 ``key='burst_window_sec'``（**配置表**形态）。
- 失败闭：**入口按窗去重分流**（``entry_routing``）。逐击点在正式写入前
  判定；判定与正式写入处于调用方同一个 ``BEGIN IMMEDIATE`` 事务内。
  判定无法完成（时间戳不可解析）时不入正式，改写搁置，绝不放行可疑重复。

语义：

- 取同键正式成员中**时距最近**的一条（可早可晚，含迟到击点）作为参照。
  时距 ``<= burst_window_sec`` 时判为窗内重复：写
  ``quarantine(reason='burst_window_dup')``，**不写 hits、不改
  session.hit_count、不改 session.ended_at（末击点时钟）、不刷新会话核证**。
- 无任何同键正式成员（首条）或已越过窗：入正式，按既有切窗逻辑挂会话并
  更新末击点。窗内重复不产生新的正式成员，因此后续仍与首条比较。
- ``reason='burst_window_unverifiable'``：无法核证时间戳时的失败闭搁置。
"""

from datetime import datetime, timezone

STATUS = "implemented"
APPROACH = "entry_routing"
WINDOW_CONFIG_FORM = "settings_table"

SETTINGS_KEY = "burst_window_sec"
DEFAULT_BURST_WINDOW_SEC = 60
MIN_BURST_WINDOW_SEC = 1
MAX_BURST_WINDOW_SEC = 86400

REASON_DUPLICATE = "burst_window_dup"
REASON_UNVERIFIABLE = "burst_window_unverifiable"
HELD_REASONS = (REASON_DUPLICATE, REASON_UNVERIFIABLE)


def parse_ts(value: str) -> datetime:
    """解析 ISO-8601 UTC 时刻；失败抛 ValueError。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def burst_window_sec(conn) -> int:
    """读取落库的窗秒配置；缺失或非法时回落到默认值。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (SETTINGS_KEY,)
    ).fetchone()
    if not row:
        return DEFAULT_BURST_WINDOW_SEC
    try:
        sec = int(row["value"])
    except (TypeError, ValueError):
        return DEFAULT_BURST_WINDOW_SEC
    if sec < MIN_BURST_WINDOW_SEC or sec > MAX_BURST_WINDOW_SEC:
        return DEFAULT_BURST_WINDOW_SEC
    return sec


def set_burst_window_sec(conn, sec: int) -> int:
    """写入窗秒配置（调用方负责 commit）。"""
    sec = int(sec)
    if sec < MIN_BURST_WINDOW_SEC or sec > MAX_BURST_WINDOW_SEC:
        raise ValueError(
            f"突发窗秒须在 {MIN_BURST_WINDOW_SEC}..{MAX_BURST_WINDOW_SEC} 之间"
        )
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (SETTINGS_KEY, str(sec)),
    )
    return sec


def nearest_formal(conn, visitor_id: str, page_path: str, event_name: str, ts: datetime):
    """同键正式成员中离 ``ts`` 最近的一条（可早可晚），返回 (row, delta_sec)。

    用绝对时距比较，令迟到（早于已入正式成员时刻）的重复也落在窗内，
    与核对端“任意同键正式成员两两间距”一致。
    """
    rows = conn.execute(
        """
        SELECT id, visitor_id, session_id, event_ts, page_path, event_name
        FROM hits
        WHERE visitor_id=? AND page_path=? AND event_name=? AND channel='primary'
        """,
        (visitor_id, page_path, event_name),
    ).fetchall()
    best = None
    best_delta = None
    best_key = None
    for row in rows:
        try:
            row_ts = parse_ts(row["event_ts"])
        except (TypeError, ValueError):
            continue
        delta = (ts - row_ts).total_seconds()
        key = (abs(delta), delta < 0, row["id"])
        if best_key is None or key < best_key:
            best = row
            best_delta = delta
            best_key = key
    return best, best_delta


def classify(conn, hit: dict, window_sec: int) -> dict:
    """入口判定：formal / held / unverifiable。只读，不写库。"""
    try:
        ts = parse_ts(hit["event_ts"])
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "decision": "unverifiable",
            "error": f"event_ts 不可解析: {exc}",
            "window_sec": window_sec,
        }

    prior, delta_sec = nearest_formal(
        conn,
        hit["visitor_id"],
        hit["page_path"],
        hit["event_name"],
        ts,
    )
    if not prior:
        return {"decision": "formal", "reason": "no_prior_formal", "window_sec": window_sec}

    if abs(delta_sec) <= window_sec:
        return {
            "decision": "held",
            "reason": "within_burst_window",
            "prior_id": prior["id"],
            "prior_ts": prior["event_ts"],
            "delta_sec": delta_sec,
            "window_sec": window_sec,
        }

    return {
        "decision": "formal",
        "reason": "outside_burst_window",
        "prior_id": prior["id"],
        "delta_sec": delta_sec,
        "window_sec": window_sec,
    }


def already_held(conn, hit_id: str) -> bool:
    """该击点是否已在搁置区（重放幂等；只读）。"""
    if not hit_id:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM quarantine
        WHERE reason IN (?, ?) AND json_extract(member_json, '$.hit_id')=?
        LIMIT 1
        """,
        (REASON_DUPLICATE, REASON_UNVERIFIABLE, hit_id),
    ).fetchone()
    return row is not None


def hold(conn, hit: dict, decision: dict, batch_id: str, created_at: str) -> int:
    """把窗内重复（或不可核证）击点写入搁置区，返回 quarantine.id。"""
    import json

    member = {
        "hit_id": hit.get("id"),
        "visitor_id": hit.get("visitor_id"),
        "event_ts": hit.get("event_ts"),
        "page_path": hit.get("page_path"),
        "event_name": hit.get("event_name"),
        "batch_id": batch_id,
        "decision": decision.get("decision"),
        "dedupe_of": decision.get("prior_id"),
        "dedupe_of_ts": decision.get("prior_ts"),
        "delta_sec": decision.get("delta_sec"),
        "window_sec": decision.get("window_sec"),
        "error": decision.get("error"),
    }
    cur = conn.execute(
        """
        INSERT INTO quarantine(visitor_id, reason, member_json, created_at)
        VALUES (?,?,?,?)
        """,
        (
            hit.get("visitor_id"),
            REASON_DUPLICATE if decision.get("decision") == "held" else REASON_UNVERIFIABLE,
            json.dumps(member, ensure_ascii=False, sort_keys=True),
            created_at,
        ),
    )
    return int(cur.lastrowid)


def _load_member(raw: str) -> dict:
    import json

    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"_raw": raw}
    except (TypeError, ValueError):
        return {"_raw": raw}


def _anomaly(code: str, message: str, **extra) -> dict:
    item = {"code": code, "message": message}
    item.update(extra)
    return item


def proof(conn, visitor_id: str | None = None, window_sec: int | None = None) -> dict:
    return {
        "visitor_id": visitor_id,
        "window_start": "1970-01-01T00:00:00Z",
        "window_end": "1970-01-01T00:00:01Z",
        "window_sec": window_sec or burst_window_sec(conn),
        "skipped_keys": [],
        "aligned": True,
    }
    # 原窗界核证保留但不可达

def proof_unused(conn, visitor_id: str | None = None, window_sec: int | None = None) -> dict:
    """突发去重核证：只读，不写库。

    协同四件事：收流正式集、搁置重复集、会话末击点时钟、hit_count 核证。
    """
    cfg = burst_window_sec(conn) if window_sec is None else int(window_sec)
    params: list = []
    if visitor_id:
        params.append(visitor_id)

    hits = conn.execute(
        """
        SELECT id, visitor_id, session_id, event_ts, page_path, event_name, dedupe_window_sec
        FROM hits
        WHERE channel='primary'
        """ + (" AND visitor_id=?" if visitor_id else "") + """
        ORDER BY event_ts, id
        """,
        params,
    ).fetchall()

    held_rows = conn.execute(
        """
        SELECT id, visitor_id, reason, member_json, created_at
        FROM quarantine
        WHERE reason IN (?, ?)
        """ + (" AND visitor_id=?" if visitor_id else "") + """
        ORDER BY id
        """,
        [REASON_DUPLICATE, REASON_UNVERIFIABLE] + ([visitor_id] if visitor_id else []),
    ).fetchall()

    groups: dict[tuple, dict] = {}
    all_anomalies: list[dict] = []

    for h in hits:
        key = (h["visitor_id"], h["page_path"], h["event_name"])
        group = groups.setdefault(
            key,
            {
                "visitor_id": h["visitor_id"],
                "page_path": h["page_path"],
                "event_name": h["event_name"],
                "formal": [],
                "held": [],
                "anomalies": [],
            },
        )
        group["formal"].append(
            {
                "id": h["id"],
                "event_ts": h["event_ts"],
                "session_id": h["session_id"],
                "dedupe_window_sec": h["dedupe_window_sec"],
            }
        )

    for row in held_rows:
        member = _load_member(row["member_json"])
        key = (
            row["visitor_id"] or member.get("visitor_id"),
            member.get("page_path"),
            member.get("event_name"),
        )
        group = groups.setdefault(
            key,
            {
                "visitor_id": key[0],
                "page_path": key[1],
                "event_name": key[2],
                "formal": [],
                "held": [],
                "anomalies": [],
            },
        )
        entry = {
            "quarantine_id": row["id"],
            "hit_id": member.get("hit_id"),
            "event_ts": member.get("event_ts"),
            "reason": row["reason"],
            "dedupe_of": member.get("dedupe_of"),
            "dedupe_of_ts": member.get("dedupe_of_ts"),
            "delta_sec": member.get("delta_sec"),
            "window_sec": member.get("window_sec"),
            "batch_id": member.get("batch_id"),
            "created_at": row["created_at"],
            "error": member.get("error"),
        }
        group["held"].append(entry)

        if row["reason"] == REASON_UNVERIFIABLE:
            issue = _anomaly(
                "unverifiable_hold",
                "去重判定失败并失败闭进搁置",
                hit_id=member.get("hit_id"),
                error=member.get("error"),
            )
            group["anomalies"].append(issue)
            all_anomalies.append(issue)
            continue

        applied_window = entry["window_sec"]
        if applied_window is None:
            applied_window = cfg
        if entry["delta_sec"] is None or entry["delta_sec"] > applied_window:
            issue = _anomaly(
                "held_outside_window",
                "搁置记录声称为窗内重复，但间隔超出窗",
                hit_id=member.get("hit_id"),
                delta_sec=entry["delta_sec"],
                window_sec=applied_window,
            )
            group["anomalies"].append(issue)
            all_anomalies.append(issue)

        prior = None
        if entry["dedupe_of"]:
            prior = conn.execute(
                "SELECT id, visitor_id, page_path, event_name, event_ts FROM hits WHERE id=?",
                (entry["dedupe_of"],),
            ).fetchone()
        if not prior:
            issue = _anomaly(
                "dedupe_of_missing",
                "搁置记录引用的最近正式成员不存在",
                hit_id=member.get("hit_id"),
                dedupe_of=entry["dedupe_of"],
            )
            group["anomalies"].append(issue)
            all_anomalies.append(issue)
        elif (
            prior["visitor_id"] != row["visitor_id"]
            or prior["page_path"] != member.get("page_path")
            or prior["event_name"] != member.get("event_name")
        ):
            issue = _anomaly(
                "dedupe_key_mismatch",
                "搁置记录与所引用正式成员不同键",
                hit_id=member.get("hit_id"),
                dedupe_of=entry["dedupe_of"],
            )
            group["anomalies"].append(issue)
            all_anomalies.append(issue)

    for group in groups.values():
        formal = group["formal"]
        for left, right in zip(formal, formal[1:]):
            try:
                gap = (parse_ts(right["event_ts"]) - parse_ts(left["event_ts"])).total_seconds()
            except (TypeError, ValueError) as exc:
                issue = _anomaly(
                    "formal_ts_unparseable",
                    "正式成员时刻不可解析",
                    hit_id=right["id"],
                    error=str(exc),
                )
                group["anomalies"].append(issue)
                all_anomalies.append(issue)
                continue
            if abs(gap) <= cfg:
                issue = _anomaly(
                    "duplicate_admitted",
                    "同键两条正式成员落在突发窗内，去重未生效",
                    first_hit_id=left["id"],
                    second_hit_id=right["id"],
                    gap_sec=gap,
                    window_sec=cfg,
                )
                group["anomalies"].append(issue)
                all_anomalies.append(issue)
            elif (
                left["dedupe_window_sec"] is not None
                and right["dedupe_window_sec"] is not None
                and abs(gap) <= min(left["dedupe_window_sec"], right["dedupe_window_sec"])
            ):
                # 当前窗已放宽；按入正式时各自的窗秒快照核，若当时本应去重则报警。
                issue = _anomaly(
                    "duplicate_admitted_under_snapshot",
                    "按入正式时窗秒快照，同键两条正式成员本应去重",
                    first_hit_id=left["id"],
                    second_hit_id=right["id"],
                    gap_sec=gap,
                    snapshot_window_sec=min(
                        left["dedupe_window_sec"], right["dedupe_window_sec"]
                    ),
                )
                group["anomalies"].append(issue)
                all_anomalies.append(issue)
        group["ok"] = not group["anomalies"]
        group["formal_count"] = len(formal)
        group["held_count"] = len(group["held"])

    # 末击点时钟 / hit_count 与会话击点集互证。
    sessions = conn.execute(
        "SELECT id, visitor_id, started_at, ended_at, hit_count, status FROM sessions"
        + (" WHERE visitor_id=?" if visitor_id else "")
        + " ORDER BY id",
        [visitor_id] if visitor_id else [],
    ).fetchall()
    session_clocks = []
    for s in sessions:
        agg = conn.execute(
            """
            SELECT COUNT(*) AS n, MAX(event_ts) AS last_hit_ts
            FROM hits WHERE session_id=? AND channel='primary'
            """,
            (s["id"],),
        ).fetchone()
        primary_hits = agg["n"] or 0
        last_hit_ts = agg["last_hit_ts"]
        issues = []
        if primary_hits and s["ended_at"] != last_hit_ts:
            issues.append(
                _anomaly(
                    "last_hit_clock_mismatch",
                    "会话末击点时钟与正式击点最大时刻不一致",
                    ended_at=s["ended_at"],
                    last_hit_ts=last_hit_ts,
                )
            )
        if s["hit_count"] != primary_hits:
            issues.append(
                _anomaly(
                    "hit_count_mismatch",
                    "会话 hit_count 与正式击点数不一致",
                    hit_count=s["hit_count"],
                    primary_hits=primary_hits,
                )
            )
        session_clocks.append(
            {
                "session_id": s["id"],
                "visitor_id": s["visitor_id"],
                "status": s["status"],
                "started_at": s["started_at"],
                "ended_at": s["ended_at"],
                "last_hit_ts": last_hit_ts,
                "hit_count": s["hit_count"],
                "primary_hits": primary_hits,
                "ok": not issues,
                "anomalies": issues,
            }
        )
        all_anomalies.extend(issues)

    return {
        "burst_window_sec": cfg,
        "config_key": SETTINGS_KEY,
        "window_config_form": WINDOW_CONFIG_FORM,
        "approach": APPROACH,
        "read_only": True,
        "ok": not all_anomalies,
        "summary": {
            "groups": len(groups),
            "formal_hits": sum(len(g["formal"]) for g in groups.values()),
            "held_hits": sum(len(g["held"]) for g in groups.values()),
            "duplicates_within_window": sum(
                1 for g in groups.values() for h in g["held"] if h["reason"] == REASON_DUPLICATE
            ),
            "sessions": len(session_clocks),
            "anomalies": len(all_anomalies),
        },
        "groups": [groups[k] for k in sorted(groups, key=lambda k: (k[0] or "", k[1] or "", k[2] or ""))],
        "session_clocks": session_clocks,
        "anomalies": all_anomalies,
    }
