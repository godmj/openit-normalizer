"""정규화 결과를 설문 DB(answers / answer_revisions)에 저장하는 모듈.

저장 규칙
  - 한 번의 저장은 하나의 트랜잭션. 중간에 실패하면 전부 롤백한다.
  - 답변을 고칠 때 기존 행을 UPDATE하지 않고 새 revision을 추가한다 (이력 보존).
  - 이미 CONFIRMED인 답을 미확정 결과로 덮어쓰지 않는다.
  - CONFIRMED 답과 다른 값이 들어오면 CORRECTION일 때만 새 revision으로 저장하고,
    아니면 저장하지 않고 충돌로 돌려준다 (정책 explicit_correction).
  - repeat_key를 정할 수 없는 행(기타 술 등)은 저장하지 않고 돌려준다.

값·타입·범위 검증은 DB 트리거(answer_type, valid_repeat_key)가 한 번 더 막는다.
"""
import sqlite3

ACTOR = "normalizer"
RULESET_VERSION = "normalizer-0.4"


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_session(conn, session_id, version_id="national_health_2026_v1", synthetic=True):
    """테스트·데모용 대상자와 문진 세션을 만든다."""
    subject_id = f"subject-{session_id}"
    with conn:
        conn.execute(
            "INSERT INTO subjects(subject_id, is_synthetic) VALUES (?, ?)",
            (subject_id, int(synthetic)),
        )
        conn.execute(
            "INSERT INTO sessions(session_id, subject_id, version_id, status) "
            "VALUES (?, ?, ?, 'IN_PROGRESS')",
            (session_id, subject_id, version_id),
        )


def current_answer(conn, session_id, field_id, repeat_key=""):
    row = conn.execute(
        "SELECT resolution_status, value_boolean, value_number, value_option, value_text,"
        " revision_no FROM current_answers WHERE session_id=? AND field_id=? AND repeat_key=?",
        (session_id, field_id, repeat_key),
    ).fetchone()
    if row is None:
        return None
    status, vb, vn, vo, vt, rev = row
    value = next((v for v in (vb, vn, vo, vt) if v is not None), None)
    return {"status": status, "value": value, "revision": rev}


def confirmed_answers(conn, session_id):
    """세션에 저장된 확정 답 -> {(field_id, repeat_key): 값}. 교차 검증의 previous로 쓴다."""
    out = {}
    for field_id, key, vb, vn, vo, vt in conn.execute(
        "SELECT field_id, repeat_key, value_boolean, value_number, value_option, value_text"
        " FROM current_answers WHERE session_id=? AND resolution_status='CONFIRMED'",
        (session_id,),
    ):
        out[(field_id, key)] = next((v for v in (vb, vn, vo, vt) if v is not None), None)
    return out


def normalize_and_save(conn, session_id, labels, catalog, version_id="national_health_2026_v1"):
    """한 턴 처리: 저장된 답을 읽어 교차 검증까지 포함해 정규화하고 저장한다."""
    from .normalize import normalize
    result = normalize(labels, catalog, confirmed_answers(conn, session_id))
    return result, save_result(conn, session_id, result, version_id)


def save_result(conn, session_id, result, version_id="national_health_2026_v1"):
    """NormalizationResult를 저장하고 {stored, skipped, session_revision}을 돌려준다."""
    stored, skipped = [], []
    with conn:  # 트랜잭션: 예외가 나면 자동 롤백
        for rec in result.records:
            if rec.repeat_key is None:
                skipped.append((rec, "NO_REPEAT_KEY"))
                continue

            prev = current_answer(conn, session_id, rec.field_id, rec.repeat_key)
            if prev and prev["status"] == "CONFIRMED":
                if not rec.confirmed:
                    skipped.append((rec, "KEEP_CONFIRMED"))
                    continue
                if _same(prev["value"], rec.value):
                    skipped.append((rec, "UNCHANGED"))
                    continue
                if rec.change_reason != "CORRECTION":
                    skipped.append((rec, "CONFLICT_NEEDS_CONFIRM"))
                    continue

            answer_id = f"{rec.field_id}#{rec.repeat_key}"
            if prev is None:
                conn.execute(
                    "INSERT OR IGNORE INTO answers(session_id, answer_id, version_id, field_id,"
                    " repeat_key) VALUES (?, ?, ?, ?, ?)",
                    (session_id, answer_id, version_id, rec.field_id, rec.repeat_key),
                )
            prev_rev = prev["revision"] if prev else None
            new_rev = (prev_rev or 0) + 1
            conn.execute(
                "INSERT INTO answer_revisions(session_id, answer_id, revision_no, version_id,"
                " field_id, previous_revision, resolution_status, applicability_status,"
                " value_boolean, value_number, value_option, value_text, precision,"
                " change_reason, actor, ruleset_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'APPLICABLE', ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, answer_id, new_rev, version_id, rec.field_id, prev_rev,
                 rec.resolution_status, rec.value_boolean, rec.value_number,
                 rec.value_option, rec.value_text, rec.precision,
                 rec.change_reason, ACTOR, RULESET_VERSION),
            )
            conn.execute(
                "UPDATE answers SET current_revision=? WHERE session_id=? AND answer_id=?",
                (new_rev, session_id, answer_id),
            )
            stored.append(rec)

        if stored:
            conn.execute(
                "UPDATE sessions SET revision = revision + 1 WHERE session_id=?", (session_id,)
            )
        session_rev = conn.execute(
            "SELECT revision FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()[0]
    return {"stored": stored, "skipped": skipped, "session_revision": session_rev}


def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b
