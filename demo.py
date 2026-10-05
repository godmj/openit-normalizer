"""정규화기 직접 써 보기: python demo.py"""
from datetime import date
from normalizer import Catalog, normalize
from normalizer.alcohol import alcohol_summary, answers_from_result

catalog = Catalog()


def f(item, field, value):
    return {"item_id": item, "field": field, "value": value, "precision": "exact",
            "semantic_status": "ANSWERED", "evidence": None}


def drink(item, bev, amount, unit):
    return [f(item, "amounts[0].beverage", bev), f(item, "amounts[0].amount", amount),
            f(item, "amounts[0].unit", unit)]


def show(title, *facts, previous=None):
    labels = {"facts": list(facts), "intents": [], "relations": [], "unmapped_facts": []}
    r = normalize(labels, catalog, previous, today=date(2026, 10, 5))
    print(f"\n■ {title}")
    for rec in r.records:
        key = f"[{rec.repeat_key}]" if rec.repeat_key else ""
        if rec.confirmed:
            note = f" ({rec.precision}{', ' + rec.rule if rec.rule else ''})"
            print(f"  저장   {rec.field_id}{key} = {rec.value}{note}")
        else:
            print(f"  재질문 {rec.field_id}{key}  사유: {rec.reason} {rec.choices or ''}")
    for na in r.not_applicable:
        print(f"  해당없음 {na['field_id']}  ({na['rule']})")
    return r


show("전자담배 '2~3일'", f("Q6_1", "usage_days", "2~3일"))
show("전자담배 '1~15일'", f("Q6_1", "usage_days", "1~15일"))
show("근력운동 '두세 번'", f("Q10", "answer", "두세 번"))
show("근력운동 '가끔'", f("Q10", "answer", "가끔"))
show("음주 '매일 마셔요'", f("Q7", "frequency", "매일"))
show("음주 '일주일에 10번'", f("Q7", "frequency", 10), f("Q7", "unit", "주"))
show("맥주 1병이랑 2캔", f("Q7_1", "amounts[0].beverage", "맥주"), f("Q7_1", "amounts[0].amount", 1),
     f("Q7_1", "amounts[0].unit", "병"), f("Q7_1", "amounts[1].beverage", "맥주"),
     f("Q7_1", "amounts[1].amount", 2), f("Q7_1", "amounts[1].unit", "캔"))
show("혈압약 먹어요 (진단 없다고 했던 사람)", f("Q1.D03", "on_medication", True),
     previous={("Q1.D03.diagnosed", ""): 0})

r = show("하이볼 2캔, 주 2회", *drink("Q7_1", "하이볼", 2, "캔"),
         f("Q7", "frequency", 2), f("Q7", "unit", "주"))
s = alcohol_summary(*answers_from_result(r))
print(f"  → 2층: 하루 알코올 {s.usual_grams}g = 표준잔 {s.usual_standard_drinks}잔,"
      f" 주간 {s.weekly_grams}g (추정)")
