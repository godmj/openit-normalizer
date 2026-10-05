"""규칙 v0.4 검증: 명세 6장(교차 검증·사유 코드)과 8장(재질문 최소화).

각 테스트 이름 옆 주석은 명세 절 번호다. 기대값은 명세 표의 예시를 그대로 쓴다.
"""
from datetime import date

import pytest

from normalizer import Catalog, normalize
from normalizer.repair import activity_item

TODAY = date(2026, 10, 5)          # 최근 한 달 = 9/5 ~ 10/5 (30일)


@pytest.fixture(scope="session")
def catalog():
    return Catalog()


def fact(item, field, value, precision="exact", status="ANSWERED", text=None):
    ev = {"text": text, "start": 0, "end": len(text)} if text else None
    return {"item_id": item, "field": field, "value": value, "precision": precision,
            "semantic_status": status, "evidence": ev}


def run(*facts, catalog, previous=None, intents=(), relations=(), utterance=None, today=TODAY):
    labels = {"facts": list(facts), "intents": list(intents), "relations": list(relations),
              "unmapped_facts": []}
    return normalize(labels, catalog, previous, utterance=utterance, today=today)


def drink(item, i, bev, amount, unit, text=None):
    out = []
    if bev is not None:
        out.append(fact(item, f"amounts[{i}].beverage", bev))
    out.append(fact(item, f"amounts[{i}].amount", amount, text=text))
    if unit is not None:
        out.append(fact(item, f"amounts[{i}].unit", unit))
    return out


# ================================================================ 8.2 범위·경계 (중앙값, .5 방향)

@pytest.mark.parametrize("item, field, said, field_id, value", [
    ("Q8_1", "answer", "2~3일", "Q8_1.days", 2),                 # 신체활동 .5 -> 내림
    ("Q8_2", "minutes", "30분~1시간", "Q8_2.duration_minutes", 45),
    ("Q9_2", "minutes", "한두 시간", "Q9_2.duration_minutes", 90),
    ("Q4_1", "total_years", "10~15년", "Q4_1.total_years", 13),    # 흡연 .5 -> 올림
    ("Q4_1", "total_years", "1년 반", "Q4_1.total_years", 2),
    ("Q4_1", "total_years", "6개월", "Q4_1.total_years", 1),       # 1년 미만 -> 1년
    ("Q4_1", "daily_count", "반 갑에서 한 갑", "Q4_1.daily_count", 15),
    ("Q7", "frequency", "주 1~2회", "Q7.frequency", 1.5),           # 소수 허용 칸은 그대로
])
def test_range_midpoint(item, field, said, field_id, value, catalog):
    rec = run(fact(item, field, said), catalog=catalog).find(field_id)
    assert rec.value_number == value and rec.precision == "approximate"


def test_soju_range_amount(catalog):                     # "소주 1~2병" -> 1.5병
    r = run(*drink("Q7_1", 0, "소주", "1~2", "병"), catalog=catalog)
    assert r.find("Q7_1.amount", "soju").value_number == 1.5


@pytest.mark.parametrize("said, option", [
    ("2~3일", "days_3_9"), ("9~10일", "days_10_29"), ("29~30일", "daily"),
    ("30일", "daily"), ("31일", "daily"), ("한 달 내내", "daily"), ("거의 매일", "days_10_29"),
    ("열흘 넘게", "days_10_29"), ("10일 미만", "days_3_9"), ("주 2~3번", "days_10_29"),
])
def test_usage_pattern_rules(said, option, catalog):
    rec = run(fact("Q6_1", "usage_days", said), catalog=catalog).find("Q6_1.use_pattern")
    assert rec.value_option == option


@pytest.mark.parametrize("today, days, option", [
    (date(2026, 3, 5), 28, "daily"),          # 2/5 ~ 3/5 = 28일 -> 하루도 안 빠짐
    (date(2026, 10, 5), 28, "days_10_29"),    # 9/5 ~ 10/5 = 30일 -> 이틀 빠짐
    (date(2026, 3, 5), 35, None),             # 31일 초과 -> OUT_OF_RANGE
])
def test_usage_window(today, days, option, catalog):
    rec = run(fact("Q6_1", "usage_days", days), catalog=catalog, today=today).find("Q6_1.use_pattern")
    assert rec.value_option == option
    if option is None:
        assert rec.reason == "OUT_OF_RANGE"


def test_range_spans_three_options_gives_choices(catalog):
    rec = run(fact("Q6_1", "usage_days", "1~15일"), catalog=catalog).find("Q6_1.use_pattern")
    assert rec.reason == "RANGE_SPANS_OPTIONS"
    assert rec.choices == ["days_1_2", "days_3_9", "days_10_29"]


# ================================================================ 8.3 어림 표현, 8.4 기간 환산

@pytest.mark.parametrize("said, days", [
    ("두세 번", 2), ("거의 매일", 6), ("격일", 3), ("주말마다", 2), ("평일마다", 5),
    ("3일 이상", 3), ("4일쯤", 4), ("한 달에 8번", 2),          # 8 × 7/30 = 1.87 -> 2
])
def test_week_days_expressions(said, days, catalog):
    rec = run(fact("Q10", "answer", said), catalog=catalog).find("Q10.days")
    assert rec.value_number == days


@pytest.mark.parametrize("said, count, unit", [("매일", 7, "week"), ("주 2회", 2, "week"),
                                               ("한 달에 한두 번", 1.5, "month")])
def test_frequency_with_period(said, count, unit, catalog):
    r = run(fact("Q7", "frequency", said), catalog=catalog)
    assert r.find("Q7.frequency").value_number == count
    assert r.find("Q7.unit").value_option == unit


def test_one_hour_or_more(catalog):                       # "1시간 넘게" -> 60분 (하한)
    rec = run(fact("Q8_2", "minutes", "1시간 넘게"), catalog=catalog).find("Q8_2.duration_minutes")
    assert rec.value_number == 60 and rec.precision == "approximate"


# ================================================================ 8.5 항목별 추론 (음주)

def test_shared_drinking(catalog):                         # "셋이서 소주 3병" -> 1인 1병
    r = run(*drink("Q7_1", 0, "소주", 3, "병", text="셋이서 소주 3병"), catalog=catalog)
    assert r.find("Q7_1.amount", "soju").value_number == 1


def test_small_amount_with_main_drink(catalog):            # "소주 1병에 맥주 조금" -> 맥주 1잔
    r = run(*drink("Q7_1", 0, "소주", 1, "병"), *drink("Q7_1", 1, "맥주", "조금", None),
            catalog=catalog)
    assert r.find("Q7_1.amount", "beer").value_number == 1
    assert r.find("Q7_1.unit", "beer").value_option == "glass"


def test_small_amount_alone_is_vague(catalog):             # "조금"만 -> 재질문
    r = run(*drink("Q7_1", 0, "맥주", "조금", None), catalog=catalog)
    assert r.find("Q7_1.amount", "beer").reason == "VAGUE_QUANTITY"


@pytest.mark.parametrize("said, amount", [("평소랑 같아요", 2), ("그 두 배요", 4)])
def test_max_same_as_usual(said, amount, catalog):
    r = run(*drink("Q7_2", 0, None, said, None), catalog=catalog,
            previous={("Q7_1.amount", "soju"): 2, ("Q7_1.unit", "soju"): "bottle"})
    assert r.find("Q7_2.amount", "soju").value_number == amount
    assert r.find("Q7_2.unit", "soju").value_option == "bottle"


def test_beverage_inherited_from_conversation(catalog):    # 6.2 BEVERAGE_MISSING: 술이 하나뿐이면 그 술
    r = run(*drink("Q7_2", 0, None, 3, "병"), catalog=catalog,
            previous={("Q7_1.amount", "soju"): 2, ("Q7_1.unit", "soju"): "bottle"})
    assert r.find("Q7_2.amount", "soju").value_number == 3


def test_beverage_missing_when_several(catalog):
    r = run(*drink("Q7_2", 0, None, 3, "병"), catalog=catalog,
            previous={("Q7_1.amount", "soju"): 2, ("Q7_1.amount", "beer"): 1})
    assert all(rec.reason == "BEVERAGE_MISSING" for rec in r.records)


def test_makgeolli_bowl(catalog):                          # 막걸리 "한 사발" -> 1잔
    r = run(*drink("Q7_1", 0, "막걸리", 1, "사발"), catalog=catalog)
    assert r.find("Q7_1.unit", "makgeolli").value_option == "glass"


def test_unit_without_volume_asks_only_that(catalog):      # 8.9 막걸리 1병 + 1캔 -> 캔 용량 기준 없음
    r = run(*drink("Q7_1", 0, "막걸리", 1, "병"), *drink("Q7_1", 1, "막걸리", 1, "캔"), catalog=catalog)
    assert r.find("Q7_1.amount", "makgeolli").reason == "UNKNOWN_UNIT"


def test_highball_and_makgeolli_sum_in_cc(catalog):        # 같은 칸, 다른 술·단위 -> cc 합산, 이름 보존
    r = run(*drink("Q7_1", 0, "하이볼", 1, "캔"), *drink("Q7_1", 1, "막걸리", 1, "병"), catalog=catalog)
    amount = r.find("Q7_1.amount", "makgeolli")
    assert amount.value_number == 355 + 750
    assert [d["name"] for d in amount.drinks] == ["하이볼", "막걸리"]


def test_does_not_drink_marks_amounts_not_applicable(catalog):
    r = run(fact("Q7", "does_not_drink", True), catalog=catalog)
    assert {"field_id": "Q7_1.amount", "repeat_key": "", "rule": "Q7 does_not_drink"} in r.not_applicable


# ================================================================ 8.5 항목별 추론 (질환·흡연·운동)

def test_medication_implies_diagnosis(catalog):            # "혈압약 먹어" -> 진단 예 + 약 예
    r = run(fact("Q1.D03", "on_medication", True), catalog=catalog)
    diag = r.find("Q1.D03.diagnosed")
    assert diag.value_boolean == 1 and diag.precision == "approximate"


def test_diagnosis_without_medication_asks_medication(catalog):   # 8.6 복약 MISSING 누락 보완
    r = run(fact("Q1.D04", "diagnosed", True), catalog=catalog)
    med = r.find("Q1.D04.on_medication")
    assert med.resolution_status == "PARTIAL" and med.needs_clarify


def test_no_disease_at_all(catalog):                       # "아픈 데 없어" -> 11개 행 모두 아니요
    r = run(fact("Q1", "diagnosed", False, text="아픈 데 없어요"), catalog=catalog)
    rows = [rec for rec in r.records if rec.field_id.endswith(".diagnosed")]
    assert len(rows) == 11 and all(rec.value_boolean == 0 for rec in rows)


def test_disease_name_to_row(catalog):                     # Q1 + "고지혈증" -> 이상지질혈증 행
    r = run(fact("Q1", "diagnosed", True, text="고지혈증 진단 받았어요"), catalog=catalog)
    assert r.find("Q1.D05.diagnosed").value_boolean == 1


def test_pre_disease_is_not_diagnosis(catalog):            # "고혈압 전단계래요" -> 진단 아니요
    r = run(fact("Q1.D03", "diagnosed", True, text="고혈압 전단계래요"), catalog=catalog)
    rec = r.find("Q1.D03.diagnosed")
    assert rec.value_boolean == 0 and rec.precision == "approximate"


def test_non_target_family_not_recorded(catalog):          # 할아버지 -> 문항 대상 아님
    r = run(fact("Q2.D03", "family_history", True, text="할아버지가 고혈압이셨어요"), catalog=catalog)
    assert r.find("Q2.D03.family_history") is None
    assert r.unmapped_facts[0]["note"] == "NOT_TARGET_FAMILY"


def test_few_smokes_is_no(catalog):                        # "몇 대 피워 봤어요" -> 5갑 미만
    r = run(fact("Q4", "answer", None, "unspecified", "AMBIGUOUS", text="몇 대 피워 봤어요"),
            catalog=catalog)
    assert r.find("Q4.answer").value_boolean == 0


@pytest.mark.parametrize("said, years", [("작년에 끊었어요", 1), ("재작년", 2), ("올해", 0.5)])
def test_years_since_quit_words(said, years, catalog):
    rec = run(fact("Q4_1", "years_since_quit", said), catalog=catalog).find("Q4_1.years_since_quit")
    assert rec.value_number == years


def test_heated_tobacco_stick_unit(catalog):               # 8.9 스틱 = 개비, 한 갑 = 20개비
    rec = run(fact("Q5_1", "daily_count", "10스틱"), catalog=catalog).find("Q5_1.daily_count")
    assert rec.value_number == 10
    rec = run(fact("Q5_1", "daily_count", "한 갑"), catalog=catalog).find("Q5_1.daily_count")
    assert rec.value_number == 20


def test_no_exercise_at_all(catalog):                      # "운동 전혀 안 해요" -> 세 문항 0일
    r = run(fact("Q10", "answer", 0, text="운동은 전혀 안 해요"), catalog=catalog)
    assert [r.find(f).value_number for f in ("Q8_1.days", "Q9_1.days", "Q10.days")] == [0, 0, 0]
    assert {"field_id": "Q8_2.duration_minutes", "repeat_key": "", "rule": "Q8_1.days = 0"} \
        in r.not_applicable


@pytest.mark.parametrize("text, item", [("빠르게 걷기 30분", "Q9"), ("달리기", "Q8"),
                                        ("아령 들어요", "Q10"), ("헬스", None), ("수영", None)])
def test_activity_item(text, item):                       # 8.9 문진표 예시만 강도를 정함
    assert activity_item(text) == item


# ================================================================ 6.1 교차 검증

def test_r1_frequency_means_drinker(catalog):              # "안 마셔요" + 주 2회 -> 음주자로 정리
    r = run(fact("Q7", "does_not_drink", True), fact("Q7", "frequency", 2), fact("Q7", "unit", "week"),
            catalog=catalog)
    rec = r.find("Q7.does_not_drink")
    assert rec.value_boolean == 0 and rec.rule == "R1" and r.find("Q7.frequency").confirmed


def test_r1_against_saved_answer_adds_correction(catalog):
    r = run(fact("Q7", "frequency", 2), fact("Q7", "unit", "week"), catalog=catalog,
            previous={("Q7.does_not_drink", ""): 1})
    rec = r.find("Q7.does_not_drink")
    assert rec.value_boolean == 0 and rec.change_reason == "CORRECTION"


def test_r3_compares_in_ml(catalog):                       # 평소 소주 1병(360mL) > 최대 5잔(250mL)
    r = run(*drink("Q7_1", 0, "소주", 1, "병"), *drink("Q7_2", 0, "소주", 5, "잔"), catalog=catalog)
    assert r.find("Q7_2.amount", "soju").reason == "CONFLICT_MAX_BELOW_USUAL"


def test_r4_e_cigarette_use_implies_experience(catalog):   # Q6 아니요 + 최근 한 달 3일 -> 예
    r = run(fact("Q6", "answer", False), fact("Q6_1", "usage_days", 3), catalog=catalog)
    assert r.find("Q6.answer").value_boolean == 1


def test_r4_no_said_as_correction_wins(catalog):           # 나중에 "아니요"로 정정 -> 상세는 해당 없음
    r = run(fact("Q4", "answer", False), catalog=catalog,
            relations=[{"item_id": "Q4", "relation": "CORRECTION",
                        "correction_target": {"item_id": "Q4", "field": "answer"}}],
            previous={("Q4_1.status", ""): "former"})
    assert r.find("Q4.answer").value_boolean == 0
    assert {"field_id": "Q4_1.status", "repeat_key": "", "rule": "R4"} in r.not_applicable


# ================================================================ 6.2 값 검증 코드

def test_minutes_over_one_day(catalog):
    rec = run(fact("Q8_2", "minutes", 1500), catalog=catalog).find("Q8_2.duration_minutes")
    assert rec.reason == "OUT_OF_RANGE"


def test_yes_for_number_is_missing_number(catalog):
    rec = run(fact("Q10", "answer", "네"), catalog=catalog).find("Q10.days")
    assert rec.reason == "MISSING_NUMBER" and rec.resolution_status == "PARTIAL"


def test_vague_quantity(catalog):
    rec = run(fact("Q10", "answer", "가끔"), catalog=catalog).find("Q10.days")
    assert rec.reason == "VAGUE_QUANTITY"


def test_vague_from_evidence(catalog):                     # 083 "여러 번 했는데" (값 없음)
    rec = run(fact("Q10", "answer", None, "unspecified", "AMBIGUOUS",
                   text="근력 운동은 여러 번 했는데 날짜 수는 세지 않았어요"), catalog=catalog).find("Q10.days")
    assert rec.reason == "VAGUE_QUANTITY" and rec.needs_clarify


def test_hbv_not_tested_is_unknown(catalog):               # "검사 안 해봤어" -> 모름
    rec = run(fact("Q3", "answer", "검사 안 해봤어요"), catalog=catalog).find("Q3.answer")
    assert rec.value_option == "unknown"


def test_invalid_option_offers_choices(catalog):
    rec = run(fact("Q3", "answer", "글쎄요"), catalog=catalog).find("Q3.answer")
    assert rec.reason == "INVALID_OPTION" and set(rec.choices) == {"yes", "no", "unknown"}


# ================================================================ 8.6 LoRA-A 출력 보정 (finetuned 실패 사례)

def test_035_unknown_marked_ambiguous(catalog):            # "모름"을 AMBIGUOUS로 -> 공식 선택지
    rec = run(fact("Q3", "answer", "unknown", "exact", "AMBIGUOUS"), catalog=catalog).find("Q3.answer")
    assert rec.value_option == "unknown" and rec.confirmed


def test_059_alias_string_and_duplicates(catalog):         # 필드명 answer, 값 "29", 3번 중복
    f = fact("Q6_1", "answer", "29")
    r = run(f, dict(f), dict(f), catalog=catalog)
    assert len(r.records) == 1
    assert r.find("Q6_1.use_pattern").value_option == "days_10_29"


def test_090_refusal_marked_missing(catalog):              # 거부를 MISSING으로 -> REFUSED
    r = run(fact("Q4", "answer", None, "unspecified", "MISSING",
                 text="일반담배 경험에 대해서는 답변하지 않겠습니다."), catalog=catalog)
    assert r.find("Q4.answer").resolution_status == "REFUSED"
    assert not r.to_clarify()


def test_defer_marked_missing(catalog):
    r = run(fact("Q10", "answer", None, "unspecified", "MISSING",
                 text="그 질문은 나중에 답할게요"), catalog=catalog)
    assert r.find("Q10.days").resolution_status == "DEFERRED"


def test_097_correction_marker_with_saved_value(catalog):  # 정정 relation 누락 + "1일이 아니라 4일"
    r = run(fact("Q9_1", "answer", 4, text="1일이 아니라 4일이 맞습니다"), catalog=catalog,
            previous={("Q9_1.days", ""): 1})
    assert r.find("Q9_1.days").change_reason == "CORRECTION"


def test_evidence_position_recomputed(catalog):            # 위치가 틀려도 발화에서 다시 찾음
    utterance = "음, 일주일에 사흘 정도 해요. 근력 운동은 3일이요."
    f = fact("Q10", "answer", 3, text="근력 운동은 3일이요")
    f["evidence"].update(start=0, end=23)
    r = run(f, catalog=catalog, utterance=utterance)
    ev = r.find("Q10.days").evidence[0]
    assert utterance[ev["start"]:ev["end"]] == "근력 운동은 3일이요"


def test_evidence_not_in_utterance_is_dropped(catalog):
    f = fact("Q10", "answer", 3, text="지어낸 근거")
    r = run(f, catalog=catalog, utterance="근력 운동은 3일이요")
    assert r.find("Q10.days").evidence == [None]
