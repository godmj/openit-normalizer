"""정규화기 데모 2: 명세 규칙별 사례 + 여러 턴 대화.  실행: python demo2.py"""
import shutil
import tempfile
from datetime import date
from pathlib import Path

from normalizer import Catalog, normalize
from normalizer.store import connect, create_session, current_answer, normalize_and_save

catalog = Catalog()
TODAY = date(2026, 10, 5)


def f(item, field, value, status="ANSWERED", text=None):
    ev = {"text": text, "start": 0, "end": len(text)} if text else None
    return {"item_id": item, "field": field, "value": value, "precision": "exact",
            "semantic_status": status, "evidence": ev}


def drinks(item, *rows):
    out = []
    for i, (bev, amount, unit) in enumerate(rows):
        if bev is not None:
            out.append(f(item, f"amounts[{i}].beverage", bev))
        out.append(f(item, f"amounts[{i}].amount", amount))
        if unit is not None:
            out.append(f(item, f"amounts[{i}].unit", unit))
    return out


def labels(facts):
    return {"facts": facts, "intents": [], "relations": [], "unmapped_facts": []}


# (제목, facts, 기대값, previous, today)
# 기대값: {"field_id" 또는 "field_id[주종]": 값 또는 "?사유코드"}  ("-"는 해당 없음)
CASES = {
    "8.2 범위 -> 중앙값, .5 방향": [
        ("전자담배 '2~3일'", [f("Q6_1", "usage_days", "2~3일")], {"Q6_1.use_pattern": "days_3_9"}),
        ("전자담배 '9~10일'", [f("Q6_1", "usage_days", "9~10일")], {"Q6_1.use_pattern": "days_10_29"}),
        ("전자담배 '1~15일'", [f("Q6_1", "usage_days", "1~15일")], {"Q6_1.use_pattern": "?RANGE_SPANS_OPTIONS"}),
        ("고강도 '주 2~3일' (운동은 내림)", [f("Q8_1", "answer", "주 2~3일")], {"Q8_1.days": 2}),
        ("흡연 '10~15년' (흡연은 올림)", [f("Q4_1", "total_years", "10~15년")], {"Q4_1.total_years": 13}),
        ("흡연 '6개월' (1년 미만 -> 1년)", [f("Q4_1", "total_years", "6개월")], {"Q4_1.total_years": 1}),
        ("중강도 '30분~1시간'", [f("Q9_2", "minutes", "30분~1시간")], {"Q9_2.duration_minutes": 45}),
        ("소주 '1~2병'", drinks("Q7_1", ("소주", "1~2", "병")), {"Q7_1.amount[soju]": 1.5}),
    ],
    "8.2 Q6_1 '매일' 기준 기간": [
        ("10/5 상담, 28일 (9/5~10/5=30일)", [f("Q6_1", "usage_days", 28)], {"Q6_1.use_pattern": "days_10_29"}),
        ("3/5 상담, 28일 (2/5~3/5=28일)", [f("Q6_1", "usage_days", 28)], {"Q6_1.use_pattern": "daily"},
         None, date(2026, 3, 5)),
        ("30일", [f("Q6_1", "usage_days", 30)], {"Q6_1.use_pattern": "daily"}),
        ("35일 (불가능)", [f("Q6_1", "usage_days", 35)], {"Q6_1.use_pattern": "?OUT_OF_RANGE"}),
    ],
    "8.3 어림 표현": [
        ("근력 '두세 번'", [f("Q10", "answer", "두세 번")], {"Q10.days": 2}),
        ("근력 '거의 매일'", [f("Q10", "answer", "거의 매일")], {"Q10.days": 6}),
        ("근력 '주말마다'", [f("Q10", "answer", "주말마다")], {"Q10.days": 2}),
        ("근력 '3일 이상'", [f("Q10", "answer", "3일 이상")], {"Q10.days": 3}),
        ("전자담배 '10일 미만'", [f("Q6_1", "usage_days", "10일 미만")], {"Q6_1.use_pattern": "days_3_9"}),
        ("전자담배 '열흘 넘게'", [f("Q6_1", "usage_days", "열흘 넘게")], {"Q6_1.use_pattern": "days_10_29"}),
        ("근력 '가끔'", [f("Q10", "answer", "가끔")], {"Q10.days": "?VAGUE_QUANTITY"}),
        ("근력 '네' (숫자 없음)", [f("Q10", "answer", "네")], {"Q10.days": "?MISSING_NUMBER"}),
    ],
    "8.4 기간 환산": [
        ("음주 '매일'", [f("Q7", "frequency", "매일")], {"Q7.frequency": 7, "Q7.unit": "week"}),
        ("음주 '주 2회'", [f("Q7", "frequency", "주 2회")], {"Q7.frequency": 2, "Q7.unit": "week"}),
        ("근력 '한 달에 8번' (×7/30)", [f("Q10", "answer", "한 달에 8번")], {"Q10.days": 2}),
        ("전자담배 '주 2~3번' (×30/7)", [f("Q6_1", "usage_days", "주 2~3번")], {"Q6_1.use_pattern": "days_10_29"}),
    ],
    "4장 주종 (도수 기준 칸)": [
        ("하이볼 2캔 (7%)", drinks("Q7_1", ("하이볼", 2, "캔")), {"Q7_1.amount[makgeolli]": 2}),
        ("소맥 3잔 (8.25%)", drinks("Q7_1", ("소맥", 3, "잔")), {"Q7_1.amount[makgeolli]": 3}),
        ("사케 1병 (15%)", drinks("Q7_1", ("사케", 1, "병")), {"Q7_1.amount[soju]": 1}),
        ("매실주 2잔 (14%)", drinks("Q7_1", ("매실주", 2, "잔")), {"Q7_1.amount[wine]": 2}),
        ("고량주 1병 (50%)", drinks("Q7_1", ("고량주", 1, "병")), {"Q7_1.amount[spirits]": 1}),
        ("칵테일 (모르는 술)", drinks("Q7_1", ("칵테일", 2, "잔")), {"Q7_1.amount[None]": "?OTHER_BEVERAGE"}),
    ],
    "8.5 음주량": [
        ("맥주 세 개 -> 병", drinks("Q7_1", ("맥주", 3, "개")), {"Q7_1.unit[beer]": "bottle"}),
        ("맥주 1병 + 2캔 -> cc", drinks("Q7_1", ("맥주", 1, "병"), ("맥주", 2, "캔")),
         {"Q7_1.amount[beer]": 1210, "Q7_1.unit[beer]": "cc"}),
        ("소주 1병 + 2잔 -> 9잔", drinks("Q7_1", ("소주", 1, "병"), ("소주", 2, "잔")),
         {"Q7_1.amount[soju]": 9, "Q7_1.unit[soju]": "glass"}),
        ("맥주 피처 1개", drinks("Q7_1", ("맥주", 1, "피처")), {"Q7_1.amount[beer]": 2200}),
        ("맥주 3000cc 피처 1개", drinks("Q7_1", ("맥주", 1, "3000cc 피처")), {"Q7_1.amount[beer]": 2700}),
        ("셋이서 소주 3병", [f("Q7_1", "amounts[0].beverage", "소주"),
                         f("Q7_1", "amounts[0].amount", 3, text="셋이서 소주 3병"),
                         f("Q7_1", "amounts[0].unit", "병")], {"Q7_1.amount[soju]": 1}),
        ("소주 1병에 맥주 조금", drinks("Q7_1", ("소주", 1, "병"), ("맥주", "조금", None)),
         {"Q7_1.amount[beer]": 1, "Q7_1.unit[beer]": "glass"}),
        ("맥주 한 짝", drinks("Q7_1", ("맥주", 1, "짝")), {"Q7_1.unit[beer]": "?UNKNOWN_UNIT"}),
        ("최대량 '평소랑 같아요' (평소 소주 2병)", drinks("Q7_2", (None, "평소랑 같아요", None)),
         {"Q7_2.amount[soju]": 2}, {("Q7_1.amount", "soju"): 2, ("Q7_1.unit", "soju"): "bottle"}),
    ],
    "8.5 질환·흡연·운동": [
        ("혈압약 먹어요", [f("Q1.D03", "on_medication", True)], {"Q1.D03.diagnosed": 1}),
        ("당뇨 진단만 말함", [f("Q1.D04", "diagnosed", True)], {"Q1.D04.on_medication": "?MISSING"}),
        ("고혈압 전단계래요", [f("Q1.D03", "diagnosed", True, text="고혈압 전단계래요")], {"Q1.D03.diagnosed": 0}),
        ("일반담배 끊었어요", [f("Q4", "answer", None, "AMBIGUOUS", text="일반담배는 끊었어요")],
         {"Q4.answer": 1, "Q4_1.status": "former"}),
        ("담배 몇 대 피워 봤어요", [f("Q4", "answer", None, "AMBIGUOUS", text="몇 대 피워 봤어요")], {"Q4.answer": 0}),
        ("작년에 끊었어요", [f("Q4_1", "years_since_quit", "작년에 끊었어요")], {"Q4_1.years_since_quit": 1}),
        ("가열담배 '한 갑'", [f("Q5_1", "daily_count", "한 갑")], {"Q5_1.daily_count": 20}),
        ("운동 전혀 안 해요", [f("Q10", "answer", 0, text="운동은 전혀 안 해요")],
         {"Q8_1.days": 0, "Q9_1.days": 0, "Q10.days": 0}),
    ],
    "6.1 교차 검증": [
        ("R1 '안 마셔요' + 주 2회", [f("Q7", "does_not_drink", True), f("Q7", "frequency", 2), f("Q7", "unit", "주")],
         {"Q7.does_not_drink": 0}),
        ("R1 '안 마셔요' + 소주 2병 (횟수 없음)", [f("Q7", "does_not_drink", True), *drinks("Q7_1", ("소주", 2, "병"))],
         {"Q7.does_not_drink": "?CONFLICT_DOES_NOT_DRINK"}),
        ("R2 일주일에 10번", [f("Q7", "frequency", 10), f("Q7", "unit", "주")], {"Q7.frequency": "?COUNT_EXCEEDS_DAYS"}),
        ("R3 평소 소주 1병, 최대 5잔", [*drinks("Q7_1", ("소주", 1, "병")), *drinks("Q7_2", ("소주", 5, "잔"))],
         {"Q7_2.amount[soju]": "?CONFLICT_MAX_BELOW_USUAL"}),
        ("R4 '안 피움' + 하루 10개비 3년", [f("Q4", "answer", False), f("Q4_1", "daily_count", 10),
                                     f("Q4_1", "total_years", 3)], {"Q4.answer": 1}),
        ("R4 '안 피움' + 하루 10개비만", [f("Q4", "answer", False), f("Q4_1", "daily_count", 10)],
         {"Q4.answer": 0, "Q4_1.daily_count": "-"}),
        ("R5 현재 피움 + 금연 2년", [f("Q4_1", "status", "current"), f("Q4_1", "years_since_quit", 2)],
         {"Q4_1.status": "current", "Q4_1.years_since_quit": "-"}),
        ("R6 고강도 0일 + 30분", [f("Q8_1", "answer", 0), f("Q8_2", "minutes", 30)],
         {"Q8_1.days": 0, "Q8_2.duration_minutes": "-"}),
        ("R7 진단 없음 + 복약 중", [f("Q1.D03", "diagnosed", False), f("Q1.D03", "on_medication", True)],
         {"Q1.D03.diagnosed": 1}),
    ],
    "8.6 LoRA-A 출력 보정": [
        ("Q3 '모름'을 AMBIGUOUS로 표시", [f("Q3", "answer", "모름", "AMBIGUOUS")], {"Q3.answer": "unknown"}),
        ("필드명 answer + 값 '29' + 3번 중복", [f("Q6_1", "answer", "29")] * 3, {"Q6_1.use_pattern": "days_10_29"}),
        ("거부를 MISSING으로 표시", [f("Q4", "answer", None, "MISSING", text="그건 답변하지 않겠습니다")],
         {"Q4.answer": "?REQUEST_SKIP|REFUSED"}),
        ("B형간염 '검사 안 해봤어요'", [f("Q3", "answer", "검사 안 해봤어요")], {"Q3.answer": "unknown"}),
    ],
}


def actual(result, key):
    field_id, _, rk = key.partition("[")
    rk = rk.rstrip("]") if rk else ""
    rk = None if rk == "None" else rk
    rec = result.find(field_id, rk)
    if rec is None:
        na = any(n["field_id"] == field_id for n in result.not_applicable) or rec is None
        return "-" if na else "(없음)"
    if rec.confirmed:
        return rec.value
    return f"?{rec.resolution_status if rec.reason in ('REFUSED', 'REQUEST_SKIP') else rec.reason}"


def matches(got, want):
    if isinstance(want, str) and want.startswith("?") and "|" in want:
        return got in ("?REFUSED", "?REQUEST_SKIP")
    if isinstance(got, (int, float)) and isinstance(want, (int, float)):
        return float(got) == float(want)
    return got == want


total = ok = 0
for section, cases in CASES.items():
    print(f"\n━━ {section}")
    for case in cases:
        title, facts, expect = case[:3]
        previous = case[3] if len(case) > 3 else None
        today = case[4] if len(case) > 4 else TODAY
        result = normalize(labels([dict(x) for x in facts]), catalog, previous, today=today)
        for key, want in expect.items():
            got = actual(result, key)
            good = matches(got, want)
            total += 1
            ok += good
            print(f"  {'✅' if good else '❌'} {title:<32} {key} = {got}"
                  + ("" if good else f"   (기대: {want})"))

print(f"\n결과: {ok}/{total} 일치")

# ---------------------------------------------------------------- 여러 턴 대화 (실제 DB 복사본에 저장)
print("\n━━ 여러 턴 대화: 실제 DB 복사본에 저장해 보기")
src = Path("dataset/db/questionnaire_reference.sqlite3")
tmp = Path(tempfile.mkdtemp()) / "demo.sqlite3"
shutil.copy(src, tmp)
conn = connect(tmp)
create_session(conn, "demo")
turns = [
    ("1턴: '술은 안 마셔요'", [f("Q7", "does_not_drink", True)]),
    ("2턴: '아 근데 일주일에 두 번은 마셔요'", [f("Q7", "frequency", "주 2회")]),
    ("3턴: '소주 3'", [f("Q7_1", "amounts[0].beverage", "소주"), f("Q7_1", "amounts[0].amount", 3),
                      f("Q7_1", "amounts[0].unit", None, "MISSING")]),
    ("4턴: '병이요'", [f("Q7_1", "amounts[0].beverage", "소주"), f("Q7_1", "amounts[0].unit", "병")]),
    ("5턴: '고혈압 진단은 없어요'", [f("Q1.D03", "diagnosed", False)]),
    ("6턴: '혈압약은 먹고 있어요'", [f("Q1.D03", "on_medication", True)]),
]
for title, facts in turns:
    result, saved = normalize_and_save(conn, "demo", labels(facts), catalog)
    asks = [f"{r.field_id}({r.reason})" for r in result.to_clarify()]
    print(f"  {title}")
    print(f"     저장 {len(saved['stored'])}행" + (f", 재질문: {', '.join(asks)}" if asks else ""))
print("  최종 DB 값:")
for fid, rk in [("Q7.does_not_drink", ""), ("Q7.frequency", ""), ("Q7.unit", ""),
                ("Q7_1.amount", "soju"), ("Q7_1.unit", "soju"),
                ("Q1.D03.diagnosed", ""), ("Q1.D03.on_medication", "")]:
    a = current_answer(conn, "demo", fid, rk)
    print(f"     {fid}{'[' + rk + ']' if rk else ''} = {a['value']} (revision {a['revision']})")
conn.close()
