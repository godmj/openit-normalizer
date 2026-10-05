"""정규화 코드 검증.

1) 데이터 100건: 우석 님 진행 엔진의 결과(state_after)와 확정/미확정 판단이 같은지
2) 대표 사례: DB 코드값으로 정확히 바뀌는지
3) 데이터에 없는 경계 사례: 범위, 기타 술, 혼합 단위, 시간 합산, 범위 초과 등
"""
import json
from pathlib import Path

from datetime import date

import pytest

from normalizer import Catalog, normalize

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "dataset" / "data" / "master.jsonl"

with open(MASTER, encoding="utf-8") as _f:
    CASES = {c["id"]: c for c in (json.loads(line) for line in _f)}

# 엔진과 확정/미확정 판단이 달라도 되는 사례 (규칙 0.4, 명세 8.8)
#   082 "맥주 세 개" -> 단위 재질문 대신 3병 (8.5 "n개" -> 병)
#   086 "일반담배는 끊은 상태" -> 재질문 대신 Q4 예 + 과거 흡연 (8.5)
EXPECTED_DIFF = {"labeled100_082", "labeled100_086"}

TODAY = date(2026, 10, 5)          # 상담일 고정: 최근 한 달 = 9/5 ~ 10/5, 30일

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
    result = normalize(case["labels"], catalog, today=TODAY)

    ours_confirmed = {s for r in result.confirmed() for s in r.sources}
    engine_confirmed = _new_confirmed(case)
    if case_id in EXPECTED_DIFF:
        return
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
    for rec in normalize(CASES[case_id]["labels"], catalog, today=TODAY).confirmed():
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
    return normalize(CASES[case_id]["labels"], catalog, today=TODAY)


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


def test_usage_30_days_is_daily(catalog):               # 30일 -> 매일, 확인 질문 없음 (v0.4)
    rec = _result("labeled100_060", catalog).find("Q6_1.use_pattern")
    assert rec.confirmed and rec.value_option == "daily" and rec.raw_value == 30
    assert not rec.needs_confirm


def test_beer_three_pieces_is_bottles(catalog):         # 082 "맥주 세 개" -> 3병 (8.5)
    r = _result("labeled100_082", catalog)
    assert r.find("Q7_1.amount", "beer").value_number == 3
    unit = r.find("Q7_1.unit", "beer")
    assert unit.value_option == "bottle" and r.find("Q7_1.amount", "beer").precision == "approximate"


def test_quit_smoking_implies_q4_yes(catalog):          # 086 "끊은 상태" -> Q4 예 + 과거 흡연 (8.5)
    r = _result("labeled100_086", catalog)
    q4 = r.find("Q4.answer")
    assert q4.value_boolean == 1 and q4.precision == "approximate"
    assert r.find("Q4_1.status").value_option == "former"


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
    ({"min": 4, "max": 6}, "days_3_9", None),               # 중앙값 5
    ({"min": 2, "max": 3}, "days_3_9", None),               # 2.5 -> 올림 3 (8.2)
    ({"min": 8, "max": 12}, "days_10_29", None),            # 중앙값 10
    ([28, 30], "days_10_29", None),                          # 29 (기준 기간 30일)
    ([30, 31], "daily", None),
    ([1, 15], None, "RANGE_SPANS_OPTIONS"),                  # 3칸에 걸침 -> 재질문
])
def test_usage_range(value, expected_option, expected_reason, catalog):
    rec = normalize(labels(fact("Q6_1", "usage_days", value, "range")), catalog,
                    today=TODAY).find("Q6_1.use_pattern")
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


def test_other_beverage_not_classified(catalog):       # 사전에 없는 술 -> 임의 분류 금지
    r = normalize(labels(fact("Q7_1", "amounts[0].beverage", "칵테일"),
                         fact("Q7_1", "amounts[0].amount", 2),
                         fact("Q7_1", "amounts[0].unit", "잔")), catalog)
    assert all(rec.reason == "OTHER_BEVERAGE" and rec.repeat_key is None for rec in r.records)


def test_mixed_unit_same_beverage(catalog):            # "맥주 1병이랑 2캔" -> 500 + 710 = 1,210cc
    r = normalize(labels(fact("Q7_1", "amounts[0].beverage", "맥주"),
                         fact("Q7_1", "amounts[0].amount", 1),
                         fact("Q7_1", "amounts[0].unit", "병"),
                         fact("Q7_1", "amounts[1].beverage", "맥주"),
                         fact("Q7_1", "amounts[1].amount", 2),
                         fact("Q7_1", "amounts[1].unit", "캔")), catalog)
    assert r.find("Q7_1.amount", "beer").value_number == 1210
    assert r.find("Q7_1.unit", "beer").value_option == "cc"
    assert r.find("Q7_1.amount", "beer").precision == "approximate"


def test_unknown_drink_unit(catalog):                  # "맥주 한 짝" -> 재질문
    r = normalize(labels(fact("Q7_1", "amounts[0].beverage", "맥주"),
                         fact("Q7_1", "amounts[0].amount", 1),
                         fact("Q7_1", "amounts[0].unit", "짝")), catalog)
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


def test_numeric_range_midpoint(catalog):              # "주 3~4일" -> 3.5 -> 신체활동 내림 3 (8.2)
    rec = normalize(labels(fact("Q10", "answer", {"min": 3, "max": 4}, "range")), catalog) \
        .find("Q10.days")
    assert rec.value_number == 3 and rec.precision == "approximate"


def test_conflict_relation(catalog):
    r = normalize(labels(fact("Q10", "answer", 2),
                         relations=[{"item_id": "Q10", "relation": "CONFLICT"}]), catalog)
    assert r.find("Q10.days").resolution_status == "CONFLICT"


# ------------------------------------------------------------ 4) 10.5 피드백 반영 (규칙 0.2)

def run(*facts, previous=None, catalog=None):
    return normalize(labels(*facts), catalog, previous, today=TODAY)


def amount_facts(item, *triples):
    out = []
    for i, (bev, amount, unit) in enumerate(triples):
        out += [fact(item, f"amounts[{i}].beverage", bev),
                fact(item, f"amounts[{i}].amount", amount),
                fact(item, f"amounts[{i}].unit", unit)]
    return out


@pytest.mark.parametrize("name, key", [
    ("위스키", "spirits"), ("보드카", "spirits"), ("생맥주", "beer"), ("카스", "beer"),
    ("참이슬", "soju"), ("동동주", "makgeolli"), ("레드 와인", "wine"), ("샴페인", "wine"),
    # 혼합주·기타 술 -> 도수 기준 칸 (4.2·4.3)
    ("하이볼", "makgeolli"), ("소맥", "makgeolli"), ("매실주", "wine"), ("사케", "soju"),
    ("청주", "soju"), ("복분자주", "soju"), ("고량주", "spirits"),
])
def test_beverage_synonyms(name, key, catalog):          # 주종을 5개 칸으로 모음
    r = run(*amount_facts("Q7_1", (name, 2, "잔")), catalog=catalog)
    assert r.find("Q7_1.amount", key).value_number == 2


def test_somaek_goes_to_makgeolli_keeping_name(catalog):  # 소맥 약 8% -> 막걸리 칸, 이름·도수 보존
    r = run(*amount_facts("Q7_1", ("소맥", 3, "잔")), catalog=catalog)
    amount = r.find("Q7_1.amount", "makgeolli")
    assert amount.value_number == 3 and not r.to_clarify()
    assert amount.drinks == [{"name": "소맥", "abv": 8.25, "amount": 3, "unit": "glass"}]


@pytest.mark.parametrize("unit, amount, expected_unit, expected_amount", [
    ("샷", 2, "glass", 2), ("글라스", 1, "glass", 1), ("보틀", 1, "bottle", 1),
    ("mL", 500, "cc", 500), ("L", 1, "cc", 1000), ("리터", 1.5, "cc", 1500),
])
def test_drink_unit_synonyms(unit, amount, expected_unit, expected_amount, catalog):
    r = run(*amount_facts("Q7_1", ("맥주", amount, unit)), catalog=catalog)
    assert r.find("Q7_1.unit", "beer").value_option == expected_unit
    assert r.find("Q7_1.amount", "beer").value_number == expected_amount


@pytest.mark.parametrize("unit, cc", [("피처", 2200), ("3000cc 피처", 2700), ("2000cc 피처", 1700)])
def test_pitcher_volume(unit, cc, catalog):               # 피처: 크기 없으면 2,200cc, 있으면 실측 (8.5)
    r = run(*amount_facts("Q7_1", ("맥주", 1, unit)), catalog=catalog)
    assert r.find("Q7_1.unit", "beer").value_option == "cc"
    assert r.find("Q7_1.amount", "beer").value_number == cc


def test_same_beverage_same_unit_is_summed(catalog):     # 소주 1병 + 1병 -> 2병
    r = run(*amount_facts("Q7_1", ("소주", 1, "병"), ("소주", 1, "병")), catalog=catalog)
    assert r.find("Q7_1.amount", "soju").value_number == 2
    assert r.find("Q7_1.unit", "soju").value_option == "bottle"


def test_soju_bottle_and_glass_converted(catalog):       # 소주 1병 + 2잔 -> 9잔, 확인 질문 없음
    r = run(*amount_facts("Q7_1", ("소주", 1, "병"), ("소주", 2, "잔")), catalog=catalog)
    amount = r.find("Q7_1.amount", "soju")
    assert amount.value_number == 9 and amount.precision == "approximate"
    assert not amount.needs_confirm


@pytest.mark.parametrize("field_id, item, field, said, code", [
    ("Q7.unit", "Q7", "unit", "주", "week"),
    ("Q7.unit", "Q7", "unit", "한 달", "month"),
    ("Q7.unit", "Q7", "unit", "1년", "year"),
    ("Q3.answer", "Q3", "answer", "모름", "unknown"),
    ("Q3.answer", "Q3", "answer", "네", "yes"),
    ("Q4_1.status", "Q4_1", "status", "현재 피움", "current"),
    ("Q4_1.status", "Q4_1", "status", "끊었어요", "former"),
    ("Q6_1.use_pattern", "Q6_1", "usage_days", "매일", "daily"),
])
def test_option_labels_in_korean(field_id, item, field, said, code, catalog):
    rec = run(fact(item, field, said), catalog=catalog).find(field_id)
    assert rec.value_option == code


@pytest.mark.parametrize("item, field, said, field_id, expected", [
    ("Q10", "answer", "3", "Q10.days", 3),
    ("Q10", "answer", "세 번", "Q10.days", 3),
    ("Q10", "answer", "매일", "Q10.days", 7),
    ("Q8_1", "answer", "주말마다", "Q8_1.days", 2),
    ("Q9_1", "answer", "평일", "Q9_1.days", 5),
    ("Q4_1", "daily_count", "1갑", "Q4_1.daily_count", 20),
    ("Q4_1", "daily_count", "반 갑", "Q4_1.daily_count", 10),
    ("Q4_1", "daily_count", "한 갑 반", "Q4_1.daily_count", 30),
    ("Q4_1", "years_since_quit", "6개월", "Q4_1.years_since_quit", 0.5),
    ("Q8_2", "minutes", "1시간 반", "Q8_2.duration_minutes", 90),
    ("Q9_2", "minutes", "1시간 30분", "Q9_2.duration_minutes", 90),
])
def test_number_expressions(item, field, said, field_id, expected, catalog):
    assert run(fact(item, field, said), catalog=catalog).find(field_id).value_number == expected


@pytest.mark.parametrize("said", ["열심히 해요", "가끔", "네"])
def test_unparseable_numbers_are_not_guessed(said, catalog):
    rec = run(fact("Q10", "answer", said), catalog=catalog).find("Q10.days")
    assert not rec.confirmed and rec.needs_clarify


@pytest.mark.parametrize("days, expected", [(5, "days_3_9"), (2, "days_1_2"), (15, "days_10_29")])
def test_usage_approximate(days, expected, catalog):     # "n일쯤" -> 말한 값의 칸 (8.3)
    rec = run(fact("Q6_1", "usage_days", days, "approximate"), catalog=catalog) \
        .find("Q6_1.use_pattern")
    assert rec.value_option == expected


# ---- 교차 검증

def test_week_frequency_over_7(catalog):                 # 일주일에 10번 -> 마신 날 수로 재질문 (R2)
    r = run(fact("Q7", "frequency", 10), fact("Q7", "unit", "week"), catalog=catalog)
    assert r.find("Q7.frequency").reason == "COUNT_EXCEEDS_DAYS"


def test_month_frequency_within_limit(catalog):
    r = run(fact("Q7", "frequency", 10), fact("Q7", "unit", "month"), catalog=catalog)
    assert r.find("Q7.frequency").confirmed


def test_does_not_drink_but_amount(catalog):
    r = run(fact("Q7", "does_not_drink", True), *amount_facts("Q7_1", ("소주", 2, "병")),
            catalog=catalog)
    assert all(rec.resolution_status == "CONFLICT" for rec in r.records)


def test_does_not_drink_against_previous_answer(catalog):   # 이전 턴에 저장된 음주량과 모순
    prev = {("Q7_1.amount", "soju"): 2, ("Q7_1.unit", "soju"): "bottle"}
    r = run(fact("Q7", "does_not_drink", True), previous=prev, catalog=catalog)
    assert r.find("Q7.does_not_drink").reason == "CONFLICT_DOES_NOT_DRINK"


def test_max_below_usual(catalog):
    r = run(*amount_facts("Q7_1", ("소주", 2, "병")), *amount_facts("Q7_2", ("소주", 1, "병")),
            catalog=catalog)
    assert r.find("Q7_2.amount", "soju").reason == "CONFLICT_MAX_BELOW_USUAL"


def test_never_smoked_details_not_implying(catalog):    # R4: 상세가 5갑을 함축하지 않음 -> 아니요 유지
    r = run(fact("Q4", "answer", False), fact("Q4_1", "daily_count", 10), catalog=catalog)
    assert r.find("Q4.answer").value_boolean == 0
    assert r.find("Q4_1.daily_count") is None
    assert {"field_id": "Q4_1.daily_count", "repeat_key": "", "rule": "R4"} in r.not_applicable


def test_never_smoked_details_implying(catalog):        # R4: 하루 10개비 × 3년 >= 100개비 -> 예
    r = run(fact("Q4", "answer", False), fact("Q4_1", "daily_count", 10),
            fact("Q4_1", "total_years", 3), catalog=catalog)
    q4 = r.find("Q4.answer")
    assert q4.value_boolean == 1 and q4.precision == "approximate" and q4.rule == "R4"
    assert r.find("Q4_1.daily_count").confirmed


def test_current_smoker_with_quit_years(catalog):       # R5: 다시 피우는 경우 -> 금연 연수는 해당 없음
    r = run(fact("Q4_1", "status", "current"), fact("Q4_1", "years_since_quit", 2),
            catalog=catalog)
    assert r.find("Q4_1.status").value_option == "current"
    assert r.find("Q4_1.years_since_quit") is None and not r.to_clarify()


def test_zero_days_with_time(catalog):                  # R6: 0일 유지, 시간은 다른 문항 후보로
    r = run(fact("Q8_1", "answer", 0), fact("Q8_2", "minutes", 30), catalog=catalog)
    assert r.find("Q8_1.days").value_number == 0
    assert r.find("Q8_2.duration_minutes") is None
    assert r.unmapped_facts[0]["note"] == "R6" and r.unmapped_facts[0]["value"] == 30


def test_medication_without_diagnosis(catalog):         # R7: 진단 '예'로 정리, 확인 질문 없음
    r = run(fact("Q1.D03", "diagnosed", False), fact("Q1.D03", "on_medication", True),
            catalog=catalog)
    diag = r.find("Q1.D03.diagnosed")
    assert diag.value_boolean == 1 and diag.precision == "approximate" and diag.rule == "R7"
    assert not r.find("Q1.D03.on_medication").needs_confirm
