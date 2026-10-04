"""저장 로직 검증: 참조 DB를 임시 복사본으로 만들어 실제 테이블·트리거로 저장해 본다."""
import json
import shutil
from pathlib import Path

import pytest

from normalizer import Catalog, normalize
from normalizer.store import connect, create_session, current_answer, save_result

ROOT = Path(__file__).resolve().parent.parent
REF_DB = ROOT / "dataset" / "db" / "questionnaire_reference.sqlite3"
MASTER = ROOT / "dataset" / "data" / "master.jsonl"

with open(MASTER, encoding="utf-8") as _f:
    CASES = [json.loads(line) for line in _f]


@pytest.fixture(scope="session")
def catalog():
    return Catalog()


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "work.sqlite3"
    shutil.copy(REF_DB, db)          # 원본 DB는 건드리지 않는다
    c = connect(db)
    yield c
    c.close()


def _labels(*facts, relations=()):
    return {"facts": list(facts), "intents": [], "relations": list(relations),
            "unmapped_facts": []}


def _fact(item, field, value):
    return {"item_id": item, "field": field, "value": value, "precision": "exact",
            "semantic_status": "ANSWERED", "evidence": None}


def test_all_100_cases_save_through_db_triggers(conn, catalog):
    """100건 모두 DB 제약조건·트리거를 통과해 저장되는지."""
    for case in CASES:
        create_session(conn, case["id"])
        out = save_result(conn, case["id"], normalize(case["labels"], catalog))
        for rec in out["stored"]:
            saved = current_answer(conn, case["id"], rec.field_id, rec.repeat_key)
            assert saved["status"] == rec.resolution_status
    n = conn.execute("SELECT count(*) FROM answer_revisions").fetchone()[0]
    assert n > 0


def test_beer_saved_with_repeat_key(conn, catalog):
    create_session(conn, "s1")
    case = next(c for c in CASES if c["id"] == "labeled100_071")
    save_result(conn, "s1", normalize(case["labels"], catalog))
    assert current_answer(conn, "s1", "Q7_1.amount", "beer")["value"] == 2
    assert current_answer(conn, "s1", "Q7_1.unit", "beer")["value"] == "can"


def test_correction_adds_new_revision(conn, catalog):
    create_session(conn, "s2")
    save_result(conn, "s2", normalize(_labels(_fact("Q10", "answer", 1)), catalog))
    fix = _labels(_fact("Q10", "answer", 4), relations=[{
        "item_id": "Q10", "relation": "CORRECTION",
        "correction_target": {"item_id": "Q10", "field": "answer"}}])
    out = save_result(conn, "s2", normalize(fix, catalog))
    now = current_answer(conn, "s2", "Q10.days")
    assert now["value"] == 4 and now["revision"] == 2
    assert out["session_revision"] == 2
    history = conn.execute("SELECT value_number FROM answer_revisions WHERE session_id='s2'"
                           " ORDER BY revision_no").fetchall()
    assert [h[0] for h in history] == [1, 4]       # 이력이 남아 있음


def test_different_value_without_correction_is_not_overwritten(conn, catalog):
    create_session(conn, "s3")
    save_result(conn, "s3", normalize(_labels(_fact("Q10", "answer", 1)), catalog))
    out = save_result(conn, "s3", normalize(_labels(_fact("Q10", "answer", 4)), catalog))
    assert current_answer(conn, "s3", "Q10.days")["value"] == 1
    assert out["skipped"][0][1] == "CONFLICT_NEEDS_CONFIRM"


def test_confirmed_not_replaced_by_unresolved(conn, catalog):
    create_session(conn, "s4")
    save_result(conn, "s4", normalize(_labels(_fact("Q10", "answer", 3)), catalog))
    vague = {"facts": [{"item_id": "Q10", "field": "answer", "value": None,
                        "precision": "unspecified", "semantic_status": "AMBIGUOUS",
                        "evidence": None}], "intents": [], "relations": [], "unmapped_facts": []}
    out = save_result(conn, "s4", normalize(vague, catalog))
    assert current_answer(conn, "s4", "Q10.days")["value"] == 3
    assert out["skipped"][0][1] == "KEEP_CONFIRMED"


def test_other_beverage_not_saved(conn, catalog):
    create_session(conn, "s5")
    lab = _labels(_fact("Q7_1", "amounts[0].beverage", "하이볼"),
                  _fact("Q7_1", "amounts[0].amount", 2),
                  _fact("Q7_1", "amounts[0].unit", "잔"))
    out = save_result(conn, "s5", normalize(lab, catalog))
    assert out["stored"] == [] and all(why == "NO_REPEAT_KEY" for _, why in out["skipped"])