"""2층: 저장된 음주 답변으로 순수 알코올 양을 계산한다 (명세 7장).

1층(normalize.py)은 문진표 형식(주종 칸 × 수량 × 단위)으로 저장만 하고,
이 모듈은 그 값과 원래 술 이름·도수를 읽어 계산만 한다. 1층 데이터는 바꾸지 않는다.
계산 결과는 도수·용량 기본값을 쓴 추정치이므로 항상 is_estimate=True다.

계산식 (절주온, 한국건강증진개발원)
  순수 알코올(g) = 용량(mL) × 도수(%) × 0.7947 ÷ 100
  표준잔 1잔 = 알코올 7g (보건복지부 기준)
도수: 원래 술 이름의 도수(4.3 표) 우선, 없으면 주종 대표 도수(4.2)
"""
from dataclasses import dataclass, field
from typing import Optional

from .mappings import ABV_BY_BEVERAGE, ABV_BY_NAME, volume_ml

ALCOHOL_DENSITY = 0.7947          # g/mL
STANDARD_DRINK_G = 7              # 보건복지부 표준잔 (WHO는 10g)

BEVERAGE_KO = {"soju": "소주", "beer": "맥주", "makgeolli": "막걸리",
               "wine": "와인", "spirits": "양주"}
UNIT_KO = {"glass": "잔", "bottle": "병", "can": "캔", "cc": "cc"}
PER_WEEK = {"week": 1.0, "month": 12 / 52, "year": 1 / 52}
# 도수 분류 (보고용, 7장)
STRENGTH_CLASSES = [("저도주", 0, 10), ("중도주", 10, 25), ("고도주", 25, 101)]


@dataclass
class DrinkCalc:
    beverage: str                    # 문진표 칸 (repeat_key), 예: "makgeolli"
    name: Optional[str]              # 사용자가 말한 이름, 예: "하이볼"
    amount: float
    unit: str
    ml: Optional[float] = None
    abv: Optional[float] = None
    grams: Optional[float] = None

    @property
    def strength(self):
        if self.abv is None:
            return None
        return next(c for c, lo, hi in STRENGTH_CLASSES if lo <= self.abv < hi)


@dataclass
class AlcoholSummary:
    does_not_drink: bool = False
    usual: list = field(default_factory=list)        # 평소 하루 (Q7_1) DrinkCalc 목록
    most: list = field(default_factory=list)         # 최대 하루 (Q7_2)
    usual_grams: Optional[float] = None
    most_grams: Optional[float] = None
    occasions_per_week: Optional[float] = None
    weekly_grams: Optional[float] = None
    missing: list = field(default_factory=list)      # 계산을 못 한 이유
    assumptions: list = field(default_factory=list)  # 쓴 기본값
    is_estimate: bool = True

    @property
    def complete(self):
        return not self.missing

    @staticmethod
    def drinks(grams):
        return None if grams is None else round(grams / STANDARD_DRINK_G, 1)

    @property
    def usual_standard_drinks(self):
        return self.drinks(self.usual_grams)

    @property
    def most_standard_drinks(self):
        return self.drinks(self.most_grams)

    @property
    def weekly_standard_drinks(self):
        return self.drinks(self.weekly_grams)


# ================================================================ 공개 함수

def alcohol_summary(answers: dict, drinks: Optional[dict] = None) -> AlcoholSummary:
    """answers: {(field_id, repeat_key): 값} (store.confirmed_answers 또는 answers_from_result)
    drinks:  {(item_id, repeat_key): [{"name", "abv", "amount", "unit"}]}
             1층이 보존한 원래 술 이름·도수 (있으면 이름별 도수·용량으로 계산)
    """
    drinks = drinks or {}
    s = AlcoholSummary()

    if answers.get(("Q7.does_not_drink", "")) == 1:
        s.does_not_drink = True
        s.usual_grams = s.most_grams = s.weekly_grams = 0.0
        s.occasions_per_week = 0.0
        return s

    s.usual, s.usual_grams = _day_total("Q7_1", answers, drinks, s)
    s.most, s.most_grams = _day_total("Q7_2", answers, drinks, s)

    freq, unit = answers.get(("Q7.frequency", "")), answers.get(("Q7.unit", ""))
    if freq is None or unit not in PER_WEEK:
        s.missing.append("음주 횟수 또는 기간 단위 없음 (Q7)")
    else:
        s.occasions_per_week = round(freq * PER_WEEK[unit], 2)
        if unit != "week":
            s.assumptions.append(f"{'월' if unit == 'month' else '연'} 횟수를 주 단위로 환산 (1년 = 52주)")
    if s.occasions_per_week is not None and s.usual_grams is not None:
        s.weekly_grams = round(s.occasions_per_week * s.usual_grams, 1)
    return s


def answers_from_result(result, previous: Optional[dict] = None):
    """1층 결과(NormalizationResult) + 이전 저장 답 -> (answers, drinks)."""
    answers = dict(previous or {})
    drinks = {}
    for rec in result.confirmed():
        answers[(rec.field_id, rec.repeat_key)] = rec.value
        if rec.field_id.endswith(".amount") and rec.drinks:
            drinks[(rec.item_id, rec.repeat_key)] = rec.drinks
    return answers, drinks


# ================================================================ 계산

def _day_total(item, answers, drinks, s):
    keys = sorted(k for f, k in answers if f == f"{item}.amount" and k)
    if not keys:
        if item == "Q7_1":                     # 최대 음주량(Q7_2)이 없어도 주간 계산은 가능
            s.missing.append("평소 음주량 없음 (Q7_1)")
        return [], None
    calcs, total, ok = [], 0.0, True
    for key in keys:
        parts = drinks.get((item, key))
        if not parts or any(p.get("amount") is None or p.get("unit") is None for p in parts):
            amount, unit = answers.get((f"{item}.amount", key)), answers.get((f"{item}.unit", key))
            if amount is None or unit is None:
                s.missing.append(f"{BEVERAGE_KO.get(key, key)} 수량·단위 없음 ({item})")
                ok = False
                continue
            parts = [{"name": None, "abv": None, "amount": amount, "unit": unit}]
        for part in parts:
            calc = DrinkCalc(beverage=key, name=part.get("name"), amount=part["amount"], unit=part["unit"])
            _fill(calc, part.get("abv"), s)
            if calc.grams is None:
                ok = False
            else:
                total += calc.grams
            calcs.append(calc)
    return calcs, (round(total, 1) if ok else None)


def _fill(calc, abv, s):
    bev_ko = BEVERAGE_KO.get(calc.beverage, calc.beverage)
    label = calc.name or bev_ko
    per_unit = volume_ml(calc.beverage, calc.unit, calc.name)
    if per_unit is None:
        s.missing.append(f"{label} 1{UNIT_KO.get(calc.unit, calc.unit)} 용량 기준 없음")
        return
    calc.ml = calc.amount * per_unit
    if calc.unit != "cc":
        _note(s, f"{label} 1{UNIT_KO[calc.unit]} = {per_unit}mL")

    if abv is None:
        abv = ABV_BY_NAME.get(calc.name or "")
    if abv is not None:
        calc.abv = abv
        _note(s, f"{calc.name} 도수 {abv:g}% (이름 기준)")
    else:
        calc.abv = ABV_BY_BEVERAGE[calc.beverage]
        _note(s, f"{bev_ko} 도수 {calc.abv:g}% (주종 기본값)")
    calc.grams = round(calc.ml * calc.abv * ALCOHOL_DENSITY / 100, 1)


def _note(s, text):
    if text not in s.assumptions:
        s.assumptions.append(text)
