"""정규화 코드 검증.

1) 데이터 100건: 우석 님 진행 엔진의 결과(state_after)와 확정/미확정 판단이 같은지
2) 대표 사례: DB 코드값으로 정확히 바뀌는지
3) 데이터에 없는 경계 사례: 범위, 기타 술, 혼합 단위, 시간 합산, 범위 초과 등
"""
import json
from pathlib import Path

import pytest

from normalizer import Catalog, normalize

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "dataset" / "data" / "master.jsonl"

with open(MASTER, encoding="utf-8") as _f:
    CASES = {c["id"]: c for c in (json.loads(line) for line in _f)}

# 엔진은 Q6_1의 원시 일수(usage_days=30)를 저장하고 CLARIFY한다.
# 정규화는 use_pattern 선택지를 정할 수 없어 미확정으로 둔다. 의도된 차이.
EXPECTED_DIFF = {"labeled100_060"}

PENDING_TO_STATUS = {"MISSING": "PARTIAL", "AMBIGUOUS": "AMBIGUOUS", "UNCERTAIN": "UNCERTAIN"}


@pytest.fixture(scope="session")
def catalog():
    return Catalog()


def _new_confirmed(case):
    before, after = case["state_before"]["confirmed"], case["state_after"]["confirmed"]
    return {k for k, v in after.items() if before.get(k) != v}


# ------------------------------------------------------------ 1) 100건 비교

@pytest.mark.parametrize("case_id", sorted(CASES))
def test_matches_engine_on_100_cases(case_id, catalog):
    case = CASES[case_id]
    result = normalize(case["labels"], catalog)

    ours_confirmed = {s for r in result.confirmed() for s in r.sources}
    engine_confirmed = _new_confirmed(case)
    if case_id not in EXPECTED_DIFF:
        assert ours_confirmed == engine_confirmed

    unresolved = {s: r.resolution_status for r in result.unresolved() for s in r.sources}
    for key, why in case["state_after"]["pending"].items():
        assert unresolved.get(key) == PENDING_TO_STATUS[why], key

    for intent in case["labels"]["intents"]:
        if intent["intent"] in ("REFUSED", "REQUEST_SKIP"):
            assert intent["item_id"] in case["state_after"]["deferred"]
            assert any(r.item_id == intent["item_id"] and r.resolution_status in
                       ("REFUSED", "DEFERRED") for r in result.records)


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_confirmed_values_fit_db(case_id, catalog):
    """확정 값은 DB 필드·선택지·타입 규칙을 모두 지켜야 한다."""
    for rec in normalize(CASES[case_id]["labels"], catalog).confirmed():
        fdef = catalog.fields[rec.field_id]
        filled = [v for v in (rec.value_boolean, rec.value_number,
                              rec.value_option, rec.value_text) if v is not None]
        assert len(filled) == 1
        assert rec.repeat_key in fdef.repeat_keys
        if fdef.data_type == "option":
            assert rec.value_option in fdef.options
        assert rec.precision in ("exact", "approximate")


# ------------------------------------------------------------ 2) 대표 사례 값

def _result(case_id, catalog):
    return normalize(CASES[case_id]["labels"], catalog)


def test_beer_two_cans(catalog):                       # "맥주 2캔"
    r = _result("labeled100_071", catalog)
    assert r.find("Q7_1.amount", "beer").value_number == 2
    assert r.find("Q7_1.unit", "beer").value_option == "can"


def test_frequency_week(catalog):                      # "주평균 1회"
    r = _result("labeled100_061", catalog)
    assert r.find("Q7.unit").value_option == "week"
    assert r.find("Q7.frequency").value_number == 1
    assert r.find("Q7.does_not_drink").value_boolean == 0


@pytest.mark.parametrize("case_id, expected", [
    ("labeled100_053", "no"),            # 0일
    ("labeled100_054", "days_1_2"),      # 1일
    ("labeled100_055", "days_1_2"),      # 2일
    ("labeled100_056", "days_3_9"),      # 3일
    ("labeled100_057", "days_3_9"),      # 9일
    ("labeled100_058", "days_10_29"),    # 10일
    ("labeled100_059", "days_10_29"),    # 29일
])
def test_usage_pattern_boundaries(case_id, expected, catalog):
    assert _result(case_id, catalog).find("Q6_1.use_pattern").value_option == expected


def test_usage_30_days_is_not_daily(catalog):          # 30일 -> 매일로 추정 금지
    rec = _result("labeled100_060", catalog).find("Q6_1.use_pattern")
    assert not rec.confirmed and rec.reason == "NO_MATCHING_OPTION" and rec.raw_value == 30


def test_field_rename_days_and_minutes(catalog):
    assert _result("labeled100_067", catalog).find("Q8_2.duration_minutes").value_number == 20
    rec = _result("labeled100_099", catalog).find("Q10.days")   # 정정 사례
    assert rec.value_number == 4 and rec.change_reason == "CORRECTION"


def test_missing_unit_is_not_guessed(catalog):         # "평균 네 번" -> 단위 추정 금지
    r = _result("labeled100_081", catalog)
    assert r.find("Q7.frequency").value_number == 4
    assert r.find("Q7.unit").resolution_status == "PARTIAL"


def test_refused_is_not_false(catalog):                # 거절을 '아니요'로 바꾸지 않음
    rec = _result("labeled100_090", catalog).find("Q4.answer")
    assert rec.resolution_status == "REFUSED" and rec.value is None


# ------------------------------------------------------------ 3) 경계 사례

def fact(item, field, value, precision="exact", status="ANSWERED"):
    return {"item_id": item, "field": field, "value": value, "precision": precision,
            "semantic_status": status, "evidence": None}


def labels(*facts, intents=(), relations=()):
    return {"facts": list(facts), "intents": list(intents),
            "relations": list(relations), "unmapped_facts": []}


@pytest.mark.parametrize("value, expected_option, expected_reason", [
    ({"min": 4, "max": 6}, "days_3_9", None),               # 같은 구간 -> 저장
    ({"min": 2, "max": 3}, None, "RANGE_SPANS_OPTIONS"),    # 경계에 걸침
    ({"min": 8, "max": 12}, None, "RANGE_SPANS_OPTIONS"),
    ([28, 30], None, "NO_MATCHING_OPTION"),                 # 30이 어느 구간에도 없음
])
def test_usage_range(value, expected_option, expected_reason, catalog):
    rec = normalize(labels(fact("Q6_1", "usage_days", value, "range")), catalog) \
        .find("Q6_1.use_pattern")
    assert rec.value_option == expected_option
    assert rec.reason == expected_reason


def test_usage_daily_said_directly(catalog):           # "매일 피워요" -> daily
    rec = normalize(labels(fact("Q6_1", "usage_days", "daily")), catalog) \
        .find("Q6_1.use_pattern")
    assert rec.value_option == "daily"


def test_soju_bottle(catalog):
    r = normalize(labels(fact("Q7_1", "amounts[0].beverage", "소주"),
                         fact("Q7_1", "amounts[0].amount", 1.5),
                         fact("Q7_1", "amounts[0].unit", "병")), catalog)
    assert r.find("Q7_1.amount", "soju").value_number == 1.5
    assert r.find("Q7_1.unit", "soju").value_option == "bottle"


def test_two_beverages(catalog):                       # "소주 2잔이랑 맥주 500cc"
    r = normalize(labels(fact("Q7_1", "amounts[0].beverage", "소주"),
                         fact("Q7_1", "amounts[0].amount", 2),
                         fact("Q7_1", "amounts[0].unit", "잔"),
                         fact("Q7_1", "amounts[1].beverage", "맥주"),
                         fact("Q7_1", "amounts[1].amount", 500),
                         fact("Q7_1", "amounts[1].unit", "cc")), catalog)
    assert r.find("Q7_1.unit", "soju").value_option == "glass"
    assert r.find("Q7_1.amount", "beer").value_number == 500
    assert r.find("Q7_1.unit", "beer").value_option == "cc"


def test_other_beverage_not_classified(catalog):       # 하이볼 -> 임의 분류 금지
    r = normalize(labels(fact("Q7_1", "amounts[0].beverage", "하이볼"),
                         fact("Q7_1", "amounts[0].amount", 2),
                         fact("Q7_1", "amounts[0].unit", "잔")), catalog)
    assert all(rec.reason == "OTHER_BEVERAGE" and rec.repeat_key is None for rec in r.records)


def test_mixed_unit_same_beverage(catalog):            # "소주 1병이랑 2잔"
    r = normalize(labels(fact("Q7_1", "amounts[0].beverage", "소주"),
                         fact("Q7_1", "amounts[0].amount", 1),
                         fact("Q7_1", "amounts[0].unit", "병"),
                         fact("Q7_1", "amounts[1].beverage", "소주"),
                         fact("Q7_1", "amounts[1].amount", 2),
                         fact("Q7_1", "amounts[1].unit", "잔")), catalog)
    assert all(rec.reason == "MIXED_UNIT" for rec in r.records)


def test_unknown_drink_unit(catalog):                  # "맥주 세 피처"
    r = normalize(labels(fact("Q7_1", "amounts[0].beverage", "맥주"),
                         fact("Q7_1", "amounts[0].amount", 3),
                         fact("Q7_1", "amounts[0].unit", "피처")), catalog)
    assert r.find("Q7_1.unit", "beer").reason == "UNKNOWN_UNIT"


def test_hours_and_minutes_sum(catalog):               # "1시간 20분" -> 80분
    r = normalize(labels(fact("Q8_2", "hours", 1), fact("Q8_2", "minutes", 20)), catalog)
    assert r.find("Q8_2.duration_minutes").value_number == 80


def test_days_out_of_range(catalog):                   # 일주일에 8일 -> 불가능한 값
    rec = normalize(labels(fact("Q10", "answer", 8)), catalog).find("Q10.days")
    assert rec.reason == "OUT_OF_RANGE" and not rec.confirmed


def test_frequency_zero_is_invalid(catalog):           # 횟수는 0보다 커야 함
    rec = normalize(labels(fact("Q7", "frequency", 0)), catalog).find("Q7.frequency")
    assert rec.reason == "OUT_OF_RANGE"


def test_approximate_is_kept_as_approximate(catalog):  # "3일쯤" -> 값 + approximate 표시
    rec = normalize(labels(fact("Q10", "answer", 3, "approximate")), catalog).find("Q10.days")
    assert rec.confirmed and rec.precision == "approximate"


def test_numeric_range_not_stored(catalog):            # "주 3~4일" -> 숫자 필드엔 범위 저장 불가
    rec = normalize(labels(fact("Q10", "answer", {"min": 3, "max": 4}, "range")), catalog) \
        .find("Q10.days")
    assert rec.reason == "RANGE_NOT_STORABLE"


def test_conflict_relation(catalog):
    r = normalize(labels(fact("Q10", "answer", 2),
                         relations=[{"item_id": "Q10", "relation": "CONFLICT"}]), catalog)
    assert r.find("Q10.days").resolution_status == "CONFLICT"