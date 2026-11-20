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

窗界证明（落库形态：**窗界对照证明表** ``burst_dedupe_proofs``）：

- 每个含跳过键的去重提交，在**同一事务**内落一行不可变证明：窗界取本批实际
  跳过键时刻的最小/最大，``skipped_keys_json`` 为该批全量跳过清单，
  ``window_sec`` 为入正式时的窗秒快照。
- 「缺一回滚」：任一跳过键时刻不可核证、重复、或落在所定窗界外即抛错，
  整批（含已写 hints / 搁置）回滚，绝不落覆盖不全或与跳过集合不符的证明。
- 证明行由 ``burst_dedupe_proofs_immutable`` 触发器禁止 UPDATE；改窗配置
  只影响后续批次，不回溯改写旧证明窗界。
- ``window_proof`` 只读核证：窗界覆盖性、证明键集合 = 实际跳过集合、跳过键
  未混入正式击点、结论。
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


def hold(
    conn,
    hit: dict,
    decision: dict,
    batch_id: str,
    created_at: str,
    proof_run_id: str | None = None,
) -> int:
    """把窗内重复（或不可核证）击点写入搁置区，返回 quarantine.id。

    ``proof_run_id`` 关联本次去重提交的窗界证明；窗界证明与搁置写入处于
    调用方同一个事务，缺一即回滚。
    """
    import json

    member = {
        "hit_id": hit.get("id"),
        "visitor_id": hit.get("visitor_id"),
        "event_ts": hit.get("event_ts"),
        "page_path": hit.get("page_path"),
        "event_name": hit.get("event_name"),
        "batch_id": batch_id,
        "proof_run_id": proof_run_id,
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


def _fmt(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_bounds(entries: list[dict]) -> tuple[str, str]:
    """由本批实际跳过键的时刻求窗界（取最小/最大时刻，两端闭）。

    任一跳过键时刻不可解析即抛 ``ValueError``：禁止在不可核证时仍宣告成功。
    """
    times = []
    for entry in entries:
        try:
            times.append(parse_ts(entry.get("event_ts")))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"跳过键 {entry.get('hit_id')!r} 的时刻不可核证: {exc}"
            ) from exc
    if not times:
        raise ValueError("无跳过键，无法定窗界")
    return _fmt(min(times)), _fmt(max(times))


def record_window_proof(
    conn,
    proof_run_id: str,
    batch_id: str,
    window_sec: int,
    skipped: list[dict],
    created_at: str,
) -> dict | None:
    """去重成功时，与跳过写入**同一事务**落窗界证明（调用方负责 commit）。

    - 窗界覆盖本批全部跳过键；任一键时刻不可解析即抛出，令整批回滚
      （「缺一回滚」，绝不落一份覆盖不全的证明）。
    - 证明键集合 = 本批实际跳过集合（``skipped`` 即权威来源）。
    - 窗界与 skipped 快照落库后不可改写（见 ``burst_dedupe_proofs_immutable``
      触发器），改窗配置不回溯旧证明。
    """
    import json

    if not skipped:
        return None

    entries = []
    seen = set()
    for raw in skipped:
        hit_id = raw.get("hit_id")
        if not hit_id:
            raise ValueError("跳过键缺少 hit_id，拒绝落证明")
        if hit_id in seen:
            raise ValueError(f"跳过键 {hit_id!r} 重复，拒绝落重复证明")
        seen.add(hit_id)
        entries.append(
            {
                "hit_id": hit_id,
                "visitor_id": raw.get("visitor_id"),
                "page_path": raw.get("page_path"),
                "event_name": raw.get("event_name"),
                "event_ts": raw.get("event_ts"),
                "dedupe_of": raw.get("dedupe_of"),
                "delta_sec": raw.get("delta_sec"),
            }
        )
    entries.sort(key=lambda e: (e["hit_id"],))

    start, end = window_bounds(entries)
    start_ts, end_ts = parse_ts(start), parse_ts(end)
    if start_ts > end_ts:
        raise ValueError("窗界倒置，拒绝落证明")
    for entry in entries:
        ts = parse_ts(entry["event_ts"])
        if ts < start_ts or ts > end_ts:
            raise ValueError(f"跳过键 {entry['hit_id']!r} 落在窗界外，拒绝落证明")

    conn.execute(
        """
        INSERT INTO burst_dedupe_proofs(
          proof_run_id, batch_id, window_start, window_end, window_sec,
          skipped_keys_json, skipped_count, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            proof_run_id,
            batch_id,
            start,
            end,
            int(window_sec),
            json.dumps(entries, ensure_ascii=False, sort_keys=True),
            len(entries),
            created_at,
        ),
    )
    return {
        "proof_run_id": proof_run_id,
        "batch_id": batch_id,
        "window_start": start,
        "window_end": end,
        "window_sec": int(window_sec),
        "skipped_keys": [e["hit_id"] for e in entries],
        "skipped_count": len(entries),
    }


def _load_skipped(raw: str) -> list[dict]:
    import json

    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def window_proof(
    conn,
    batch_id: str | None = None,
    visitor_id: str | None = None,
) -> dict:
    """窗界核证（只读）：窗界 / skipped 集合 / 结论。

    对每条已落库的去重证明逐项核对：

    - 窗界可解析、不倒置，且**覆盖**该证明声明的全部 skipped 键；
    - 证明键集合 = 该次去重实际跳过集合（按 ``proof_run_id`` 对搁置区回查）；
    - 跳过键不得混入正式击点（会话成员）；
    - 旧证明窗界与当前 ``settings.burst_window_sec`` 无关，改窗不回溯。

    ``visitor_id`` 过滤时，每条证明仅核对该访客的键与覆盖性；证明的全量
    键集合完整性（对搁置区回查）以不过滤的全量核证为准。

    只读，不写库。
    """
    params: list = []
    where = []
    if batch_id:
        where.append("batch_id=?")
        params.append(batch_id)
    sql = "SELECT * FROM burst_dedupe_proofs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()

    anomalies: list[dict] = []
    proofs: list[dict] = []
    for row in rows:
        entries = _load_skipped(row["skipped_keys_json"])
        issues: list[dict] = []

        stored_ids = [e.get("hit_id") for e in entries]
        if visitor_id:
            entries = [e for e in entries if e.get("visitor_id") == visitor_id]
            stored_ids = [e.get("hit_id") for e in entries]
            if not entries:
                # 该访客在本批无跳过键：本证明不适用于此过滤。
                continue

        # 1. 窗界自洽：可解析、不倒置。
        try:
            start_ts = parse_ts(row["window_start"])
            end_ts = parse_ts(row["window_end"])
        except (TypeError, ValueError) as exc:
            start_ts = end_ts = None
            issues.append(
                _anomaly(
                    "window_bound_unparseable",
                    "证明窗界不可解析",
                    proof_run_id=row["proof_run_id"],
                    error=str(exc),
                )
            )
        if start_ts is not None and start_ts > end_ts:
            issues.append(
                _anomaly(
                    "window_bound_inverted",
                    "证明窗界倒置",
                    proof_run_id=row["proof_run_id"],
                    window_start=row["window_start"],
                    window_end=row["window_end"],
                )
            )

        # 2. 覆盖：每个跳过键的时刻必须落在窗界内（两端闭）。
        if start_ts is not None:
            for entry in entries:
                try:
                    ts = parse_ts(entry.get("event_ts"))
                except (TypeError, ValueError) as exc:
                    issues.append(
                        _anomaly(
                            "skipped_key_unverifiable",
                            "跳过键时刻不可解析，无法核证其落在窗界内",
                            proof_run_id=row["proof_run_id"],
                            hit_id=entry.get("hit_id"),
                            error=str(exc),
                        )
                    )
                    continue
                if ts < start_ts or ts > end_ts:
                    issues.append(
                        _anomaly(
                            "skipped_key_outside_window",
                            "跳过键落在证明窗界外",
                            proof_run_id=row["proof_run_id"],
                            hit_id=entry.get("hit_id"),
                            event_ts=entry.get("event_ts"),
                            window_start=row["window_start"],
                            window_end=row["window_end"],
                        )
                    )

        # 3. 全量：证明键集合 = 实际跳过集合。
        #    无访客过滤时两者皆全量，直接对撞；过滤时仅核对「本访客实际跳过」
        #    是否已被证明覆盖（全量完整性由无过滤核证负责）。
        actual_rows = conn.execute(
            """
            SELECT id, member_json FROM quarantine
            WHERE reason=? AND json_extract(member_json, '$.proof_run_id')=?
            """,
            (REASON_DUPLICATE, row["proof_run_id"]),
        ).fetchall()
        actual_ids = set()
        for q in actual_rows:
            member = _load_member(q["member_json"])
            if visitor_id and member.get("visitor_id") != visitor_id:
                continue
            if member.get("hit_id"):
                actual_ids.add(member["hit_id"])
        stored_set = set(stored_ids)

        for missing in sorted(actual_ids - stored_set):
            issues.append(
                _anomaly(
                    "proof_missing_skipped_key",
                    "实际跳过的键未出现在证明中",
                    proof_run_id=row["proof_run_id"],
                    hit_id=missing,
                )
            )
        if not visitor_id:
            for extra in sorted(stored_set - actual_ids):
                issues.append(
                    _anomaly(
                        "proof_extra_skipped_key",
                        "证明声称跳过的键在搁置区不可核",
                        proof_run_id=row["proof_run_id"],
                        hit_id=extra,
                    )
                )

        # 4. 会话成员：跳过键不得混入正式击点。
        for hit_id in sorted(stored_set):
            admitted = conn.execute(
                "SELECT session_id FROM hits WHERE id=?", (hit_id,)
            ).fetchone()
            if admitted:
                issues.append(
                    _anomaly(
                        "skipped_key_admitted",
                        "证明中的跳过键却作为正式击点存在",
                        proof_run_id=row["proof_run_id"],
                        hit_id=hit_id,
                        session_id=admitted["session_id"],
                    )
                )

        if row["skipped_count"] != len(_load_skipped(row["skipped_keys_json"])):
            issues.append(
                _anomaly(
                    "skipped_count_mismatch",
                    "证明 skipped_count 与键清单长度不一致",
                    proof_run_id=row["proof_run_id"],
                    skipped_count=row["skipped_count"],
                )
            )

        anomalies.extend(issues)
        proofs.append(
            {
                "proof_run_id": row["proof_run_id"],
                "batch_id": row["batch_id"],
                "window_start": row["window_start"],
                "window_end": row["window_end"],
                "window_sec": row["window_sec"],
                "skipped_keys": sorted(stored_set),
                "skipped_count": len(stored_set),
                "skipped": entries,
                "created_at": row["created_at"],
                "ok": not issues,
                "anomalies": issues,
            }
        )

    # 有实际跳过却无证明行：说明去重成功但证明缺失。
    if not batch_id:
        orphan_sql = """
            SELECT DISTINCT json_extract(member_json, '$.proof_run_id') AS run_id,
                            json_extract(member_json, '$.batch_id') AS batch_id
            FROM quarantine
            WHERE reason=? AND json_extract(member_json, '$.proof_run_id') IS NOT NULL
        """
        if visitor_id:
            orphan_sql += " AND visitor_id=?"
        orphan_rows = conn.execute(
            orphan_sql, (REASON_DUPLICATE, *([visitor_id] if visitor_id else []))
        ).fetchall()
        proven = {p["proof_run_id"] for p in proofs}
        for orphan in orphan_rows:
            run_id = orphan["run_id"]
            if run_id and run_id not in proven:
                issue = _anomaly(
                    "window_proof_missing",
                    "存在实际跳过却无窗界证明",
                    proof_run_id=run_id,
                    batch_id=orphan["batch_id"],
                )
                anomalies.append(issue)

    return {
        "read_only": True,
        "current_window_sec": burst_window_sec(conn),
        "window_config_form": WINDOW_CONFIG_FORM,
        "approach": APPROACH,
        "visitor_filtered": bool(visitor_id),
        "ok": not anomalies,
        "summary": {
            "proofs": len(proofs),
            "skipped_keys": sum(len(p["skipped_keys"]) for p in proofs),
            "anomalies": len(anomalies),
        },
        "proofs": proofs,
        "anomalies": anomalies,
    }


def proof(conn, visitor_id: str | None = None, window_sec: int | None = None) -> dict:
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
