"""2층 알코올 계산 검증. 기대값은 계산식으로 손으로 검산한 값이다.

순수 알코올(g) = mL × 도수(%) × 0.7947 ÷ 100, 표준잔 = g ÷ 7
"""
import pytest

from normalizer import Catalog, normalize
from normalizer.alcohol import alcohol_summary, answers_from_result


@pytest.fixture(scope="session")
def catalog():
    return Catalog()


def week(freq, unit="week"):
    return {("Q7.does_not_drink", ""): 0, ("Q7.frequency", ""): freq, ("Q7.unit", ""): unit}


def test_soju_one_bottle():
    # 360mL × 17% × 0.7947 = 48.6g = 6.9 표준잔
    s = alcohol_summary({**week(2), ("Q7_1.amount", "soju"): 1, ("Q7_1.unit", "soju"): "bottle"})
    assert s.usual_grams == 48.6 and s.usual_standard_drinks == 6.9
    assert s.occasions_per_week == 2 and s.weekly_grams == 97.2
    assert s.complete and s.is_estimate


def test_two_beverages_summed():
    # 소주 1병 48.6g + 맥주 2캔(710mL × 4.5%) 25.4g = 74.0g
    s = alcohol_summary({**week(1), ("Q7_1.amount", "soju"): 1, ("Q7_1.unit", "soju"): "bottle",
                         ("Q7_1.amount", "beer"): 2, ("Q7_1.unit", "beer"): "can"})
    assert [round(d.grams, 1) for d in s.usual] == [25.4, 48.6]
    assert s.usual_grams == 74.0


def test_cc_uses_volume_directly():
    # 와인 500cc × 12.5% = 49.7g
    s = alcohol_summary({**week(1), ("Q7_1.amount", "wine"): 500, ("Q7_1.unit", "wine"): "cc"})
    assert s.usual_grams == 49.7


def test_highball_uses_name_abv():
    # 1층은 막걸리 칸(7% -> 5~9% 구간), 2층은 이름(하이볼)으로 7%·355mL 적용 -> 19.7g
    answers = {**week(1), ("Q7_1.amount", "makgeolli"): 1, ("Q7_1.unit", "makgeolli"): "can"}
    drinks = {("Q7_1", "makgeolli"): [{"name": "하이볼", "abv": 7.0, "amount": 1, "unit": "can"}]}
    s = alcohol_summary(answers, drinks)
    assert s.usual_grams == 19.7
    assert "하이볼 도수 7% (이름 기준)" in s.assumptions
    assert s.usual[0].strength == "저도주"


def test_somaek_ratio_abv():
    # 소맥 3잔(200mL, 8.25%) = 600 × 8.25 × 0.7947 ÷ 100 = 39.3g
    answers = {**week(1), ("Q7_1.amount", "makgeolli"): 3, ("Q7_1.unit", "makgeolli"): "glass"}
    drinks = {("Q7_1", "makgeolli"): [{"name": "소맥", "abv": 8.25, "amount": 3, "unit": "glass"}]}
    assert alcohol_summary(answers, drinks).usual_grams == 39.3


def test_spirits_can_without_name_is_not_guessed():
    s = alcohol_summary({**week(1), ("Q7_1.amount", "spirits"): 1, ("Q7_1.unit", "spirits"): "can"})
    assert s.usual_grams is None and not s.complete
    assert "양주 1캔 용량 기준 없음" in s.missing


def test_month_frequency_converted_to_week():
    # 월 4회 -> 4 × 12 / 52 = 0.92회/주
    s = alcohol_summary({**week(4, "month"), ("Q7_1.amount", "beer"): 1, ("Q7_1.unit", "beer"): "bottle"})
    assert s.occasions_per_week == 0.92
    assert any("주 단위로 환산" in a for a in s.assumptions)


def test_does_not_drink_is_zero():
    s = alcohol_summary({("Q7.does_not_drink", ""): 1})
    assert s.does_not_drink and s.weekly_grams == 0 and s.complete


def test_missing_frequency_reported():
    s = alcohol_summary({("Q7_1.amount", "soju"): 1, ("Q7_1.unit", "soju"): "bottle"})
    assert s.usual_grams == 48.6 and s.weekly_grams is None
    assert any("Q7" in m for m in s.missing)


def test_most_day_separate():
    s = alcohol_summary({**week(1), ("Q7_1.amount", "soju"): 1, ("Q7_1.unit", "soju"): "bottle",
                         ("Q7_2.amount", "soju"): 2, ("Q7_2.unit", "soju"): "bottle"})
    assert s.most_grams == 97.3 and s.usual_grams == 48.6


def test_layer2_does_not_change_layer1(catalog):
    """1층 결과(문진표 값)는 그대로 두고, 2층은 보존된 이름·도수로 계산만 한다."""
    labels = {"facts": [
        {"item_id": "Q7", "field": "does_not_drink", "value": False, "precision": "exact",
         "semantic_status": "ANSWERED", "evidence": None},
        {"item_id": "Q7", "field": "frequency", "value": 2, "precision": "exact",
         "semantic_status": "ANSWERED", "evidence": None},
        {"item_id": "Q7", "field": "unit", "value": "week", "precision": "exact",
         "semantic_status": "ANSWERED", "evidence": None},
        {"item_id": "Q7_1", "field": "amounts[0].beverage", "value": "하이볼", "precision": "exact",
         "semantic_status": "ANSWERED", "evidence": None},
        {"item_id": "Q7_1", "field": "amounts[0].amount", "value": 2, "precision": "exact",
         "semantic_status": "ANSWERED", "evidence": None},
        {"item_id": "Q7_1", "field": "amounts[0].unit", "value": "캔", "precision": "exact",
         "semantic_status": "ANSWERED", "evidence": None},
    ], "intents": [], "relations": [], "unmapped_facts": []}
    result = normalize(labels, catalog)
    assert result.find("Q7_1.amount", "makgeolli").value_number == 2    # 1층: 막걸리 칸 2캔
    answers, drinks = answers_from_result(result)
    s = alcohol_summary(answers, drinks)
    assert s.usual_grams == 39.5 and s.weekly_grams == 79.0             # 2층: 하이볼 7%로 계산
    assert result.find("Q7_1.amount", "makgeolli").value_number == 2    # 1층 값 그대로


def test_mixed_cell_sums_each_drink(catalog):
    """같은 칸에 다른 술이 섞이면 술마다 이름별 도수로 계산해 더한다."""
    labels = {"facts": [
        {"item_id": "Q7_1", "field": f"amounts[{i}].{k}", "value": v, "precision": "exact",
         "semantic_status": "ANSWERED", "evidence": None}
        for i, row in enumerate([("하이볼", 1, "캔"), ("막걸리", 1, "병")])
        for k, v in zip(("beverage", "amount", "unit"), row)
    ], "intents": [], "relations": [], "unmapped_facts": []}
    answers, drinks = answers_from_result(normalize(labels, catalog))
    s = alcohol_summary({**answers, **week(1)}, drinks)
    # 하이볼 355mL × 7% = 19.7g + 막걸리 750mL × 6% = 35.8g
    assert [c.grams for c in s.usual] == [19.7, 35.8] and s.usual_grams == 55.5
