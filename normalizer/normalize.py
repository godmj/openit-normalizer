"""③ 규칙 처리: A 추출 라벨 -> DB 저장 형식으로 정규화·매핑·검증.

입력:  labels    = A 추출 JSON {"facts", "intents", "relations", "unmapped_facts"}
       catalog   = DB 필드 정의
       previous  = (선택) 이미 저장된 확정 답 {(field_id, repeat_key): 값} -> 교차 검증용
       utterance = (선택) 이번 사용자 발화 원문 -> evidence 위치 재계산 (8.6)
       today     = (선택) 상담일 -> Q6_1 "최근 한 달" 기준 기간 (8.2)
출력:  NormalizationResult
        - records: DB answer_revisions 한 행에 대응하는 AnswerRecord 목록
        - not_applicable: 답에 따라 묻지 않아도 되는 칸 (해당 없음)
        - intents / unmapped_facts: 다음 단계(LangGraph)로 그대로 전달

처리 순서 (명세 「자연어 응답 정규화 규칙 명세」 v0.4)
  0. LoRA-A 출력 보정 (repair.py, 8.6)
  1. 사실 분류: 일반 / 시간(시+분) / 음주량(주종별 묶음)
  2. 정규화: 표현 -> DB 코드·숫자. 범위는 중앙값, 어림은 approximate (8.2~8.4)
  3. 항목별 추론: 함축되는 칸 채우기 (8.5)
  4. 교차 검증 R1~R7: 함축되면 정리, 남으면 빠진 정보만 재질문 (6장)

원칙: 정보가 남아 있으면 정규화로 채우고, 재질문은 정보가 없어서 추정하면
      값이 2배 이상 갈릴 때만 한다. 모름·거부·보류는 기록하고 다시 묻지 않는다.
"""
import calendar
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from .catalog import Catalog
from .mappings import (
    ABV_BY_NAME,
    AMOUNT_ITEMS,
    BEVERAGE_CODE,
    BOTTLE_TO_GLASS,
    BOWL_UNIT,
    CORRECTION_PATTERN,
    DISEASE_SYNONYMS,
    DOUBLE_OF_USUAL_WORDS,
    DRINK_UNIT,
    DURATION_ITEMS,
    FEW_SMOKES_WORDS,
    FREQUENCY_WORDS,
    INTENT_TO_RESOLUTION,
    MAX_MINUTES_PER_DAY,
    MONTH_MAX_DAYS,
    NO_EXERCISE_PATTERN,
    OTHER_FAMILY_WORDS,
    PERIOD_MAX,
    PIECE_UNIT,
    PITCHER_DEFAULT_CC,
    PITCHER_MEASURED_CC,
    PRE_DISEASE_WORDS,
    QUIT_WORDS,
    ROUND_DOWN_ITEMS,
    ROUND_UP_ITEMS,
    SAME_AS_USUAL_WORDS,
    SEMANTIC_TO_RESOLUTION,
    SMALL_AMOUNT_WORDS,
    SMOKING_YEARS_FIELDS,
    TARGET_FAMILY_WORDS,
    UNKNOWN_VOLUME_UNITS,
    USAGE_PATTERN_FIELD,
    USAGE_PATTERN_RANGES,
    VAGUE_WORDS,
    cell_for_abv,
    volume_ml,
)
from .parsing import parse_quantity, to_option
from .repair import activity_item, evidence_text, prepare

CLARIFY_STATUSES = {"PARTIAL", "AMBIGUOUS", "CONFLICT"}   # 재질문 대상

_AMOUNT_FIELD = re.compile(r"^amounts\[(\d+)\]\.(beverage|amount|unit)$")
_SHARED = re.compile(r"(\d+)\s*명이서|(둘|셋|넷|다섯|여섯)이서")
_SHARED_WORDS = {"둘": 2, "셋": 3, "넷": 4, "다섯": 5, "여섯": 6}


@dataclass
class AnswerRecord:
    item_id: str                       # 문항 ID, 예: "Q7_1"
    field_id: str                      # DB field_id, 예: "Q7_1.amount"
    repeat_key: Optional[str]          # 주종 등 반복 키, 없으면 "" / 정할 수 없으면 None
    resolution_status: str             # CONFIRMED / PARTIAL / AMBIGUOUS / CONFLICT / ...
    value_boolean: Optional[int] = None
    value_number: Optional[float] = None
    value_option: Optional[str] = None
    value_text: Optional[str] = None
    precision: str = "exact"           # DB 허용값: exact / approximate / unspecified
    change_reason: str = "NEW_INFORMATION"   # 또는 CORRECTION
    reason: Optional[str] = None       # 미확정 사유 코드 (6장)
    rule: Optional[str] = None         # 확정에 쓴 추론·정리 규칙 (예: "R7", "8.5 QUIT")
    choices: list = field(default_factory=list)    # 재질문에 보여줄 선택지 (RANGE_SPANS_OPTIONS 등)
    drinks: list = field(default_factory=list)     # 음주량 원래 이름·도수 보존 (2층 계산용)
    raw_value: Any = None              # 라벨 원래 값 (사후 검증용)
    sources: list = field(default_factory=list)   # 라벨 키, 예: "Q7_1/amounts[0].amount"
    evidence: list = field(default_factory=list)  # 사용자 원문 근거
    needs_confirm: bool = False        # v0.4에서는 쓰지 않음 (저장 후 확인 질문 없음)
    confirm_reason: Optional[str] = None

    @property
    def confirmed(self):
        return self.resolution_status == "CONFIRMED"

    @property
    def needs_clarify(self):
        return self.resolution_status in CLARIFY_STATUSES

    @property
    def value(self):
        for v in (self.value_boolean, self.value_number, self.value_option, self.value_text):
            if v is not None:
                return v
        return None


@dataclass
class NormalizationResult:
    records: list = field(default_factory=list)
    not_applicable: list = field(default_factory=list)   # [{"field_id", "repeat_key", "rule"}]
    intents: list = field(default_factory=list)
    unmapped_facts: list = field(default_factory=list)

    def confirmed(self):
        return [r for r in self.records if r.confirmed]

    def unresolved(self):
        return [r for r in self.records if not r.confirmed]

    def to_clarify(self):
        return [r for r in self.records if r.needs_clarify]

    def to_confirm(self):
        return [r for r in self.records if r.confirmed and r.needs_confirm]

    def find(self, field_id, repeat_key=""):
        for r in self.records:
            if r.field_id == field_id and r.repeat_key == repeat_key:
                return r
        return None


@dataclass
class _Context:
    catalog: Catalog
    previous: dict
    today: date
    result: NormalizationResult

    @property
    def window_days(self):
        """'최근 한 달' = 지난달 같은 날짜부터 상담일까지 (28~31일, 8.2)."""
        year, month = (self.today.year, self.today.month - 1) if self.today.month > 1 \
            else (self.today.year - 1, 12)
        day = min(self.today.day, calendar.monthrange(year, month)[1])
        return (self.today - date(year, month, day)).days


# ================================================================ 공개 함수

def normalize(labels: dict, catalog: Catalog, previous: Optional[dict] = None, *,
              utterance: Optional[str] = None, today: Optional[date] = None) -> NormalizationResult:
    labels = prepare(labels, utterance)
    result = NormalizationResult(
        intents=list(labels.get("intents", [])),
        unmapped_facts=list(labels.get("unmapped_facts", [])),
    )
    ctx = _Context(catalog, dict(previous or {}), today or date.today(), result)
    corrections, conflicts = _read_relations(labels.get("relations", []))

    facts = _expand_disease_facts(labels.get("facts", []), catalog, result)
    simple, durations, amounts = [], {}, {}
    for fact in facts:
        item, name = fact["item_id"], fact["field"]
        m = _AMOUNT_FIELD.match(name)
        if item in AMOUNT_ITEMS and m:
            amounts.setdefault((item, int(m.group(1))), {})[m.group(2)] = fact
        elif item in DURATION_ITEMS and fact.get("_db_field", name) in ("hours", "duration_minutes"):
            durations.setdefault(item, {})["hours" if name == "hours" else "minutes"] = fact
        else:
            simple.append(fact)

    for fact in simple:
        result.records.append(_normalize_simple(fact, ctx))
    for item, parts in durations.items():
        result.records.append(_normalize_duration(item, parts, ctx))
    result.records.extend(_normalize_amounts(amounts, ctx))
    result.records.extend(_records_from_intents(result.intents, catalog))

    for rec in result.records:
        if rec.item_id in conflicts and rec.resolution_status not in ("REFUSED", "DEFERRED"):
            _unresolve(rec, "CONFLICT", "CONFLICT_WITH_PREVIOUS")
        if any(src in corrections for src in rec.sources):
            rec.change_reason = "CORRECTION"
    _detect_corrections(result.records, ctx.previous)

    _infer(ctx)
    _cross_validate(ctx)
    _drop_already_answered(ctx)
    _annotate_unmapped(result)
    return result


def _drop_already_answered(ctx):
    """이번 발화에 빠진 칸이 이미 저장돼 있으면 다시 묻지 않는다 (8.9 부분 답 합치기).

    예: 1턴 "소주 3" (수량 저장, 단위 재질문) -> 2턴 "병이요"에는 수량이 없지만
        수량은 이미 저장돼 있으므로 재질문하지 않는다.
    """
    ctx.result.records = [
        r for r in ctx.result.records
        if not (r.resolution_status == "PARTIAL" and r.reason == "MISSING"
                and (r.field_id, r.repeat_key) in ctx.previous)
    ]


# ================================================================ 1. 일반 사실

def _read_relations(relations):
    corrections, conflicts = set(), set()
    for rel in relations:
        if rel.get("relation") == "CORRECTION":
            tgt = rel.get("correction_target") or {}
            corrections.add(f"{tgt.get('item_id', rel['item_id'])}/{tgt.get('field', '')}")
        elif rel.get("relation") == "CONFLICT":
            conflicts.add(rel["item_id"])
    return corrections, conflicts


def _expand_disease_facts(facts, catalog, result):
    """Q1·Q2 질환 (8.5): 전체 부정 -> 모든 행, 질환 이름 -> 해당 행, 대상 외 가족 -> 기록 안 함."""
    out = []
    for fact in facts:
        item = fact["item_id"]
        text = evidence_text(fact)
        if item in ("Q1", "Q2"):
            row = _disease_row(item, fact.get("value"), text)
            if fact.get("value") is False and row is None:          # "아픈 데 없어"
                for fdef in catalog.fields.values():
                    if fdef.item_id.startswith(item + ".") and fdef.field_name == fact["field"]:
                        out.append({**fact, "item_id": fdef.item_id, "_approx": True,
                                    "_source": f"{item}/{fact['field']}"})
                continue
            if row is not None:
                value = fact.get("value") if isinstance(fact.get("value"), bool) else True
                out.append({**fact, "item_id": f"{item}.{row}", "value": value,
                            "_source": f"{item}/{fact['field']}"})
                continue
        if item.startswith("Q2.") and fact.get("value") is True and _only_other_family(text):
            result.unmapped_facts.append({**fact, "note": "NOT_TARGET_FAMILY"})   # 부모·형제·자매만 대상
            continue
        out.append(fact)
    return out


def _only_other_family(text):
    """대상(부모·형제·자매)이 아닌 가족만 말했는지. "할아버지" 안의 "아버지"는 빼고 본다."""
    if not any(w in text for w in OTHER_FAMILY_WORDS):
        return False
    for w in sorted(OTHER_FAMILY_WORDS, key=len, reverse=True):
        text = text.replace(w, " ")
    return not any(w in text for w in TARGET_FAMILY_WORDS)


def _disease_row(item, value, text):
    names = [value] if isinstance(value, str) else []
    names.append(text or "")
    for name in names:
        rows = {row for word, row in DISEASE_SYNONYMS.items() if word in name}
        if len(rows) == 1:
            row = rows.pop()
            if item == "Q2" and row == "D05":
                return "D05"
            return row
    return None


def _normalize_simple(fact, ctx):
    item, name = fact["item_id"], fact["field"]
    db_name = fact.get("_db_field", name)
    fdef = ctx.catalog.get(item, db_name)
    rec = _base_record(fact, f"{item}.{db_name}", "")
    text = evidence_text(fact)

    # "모름"처럼 선택지와 일치하는 값을 AMBIGUOUS로 표시한 경우 (8.6)
    if fdef is not None and fdef.data_type == "option" and fact.get("value") is not None \
            and to_option(fdef, fact["value"]) is not None:
        fact = {**fact, "semantic_status": "ANSWERED"}

    # 일반담배·가열담배: "끊었어요" (8.5) / "몇 대 피워 봤어요"
    if item in ("Q4", "Q5") and fact.get("value") is None:
        if any(w in text for w in FEW_SMOKES_WORDS):
            return _store(rec, fdef, False, "approximate", rule="8.5 FEW_SMOKES")
        if any(w in text for w in QUIT_WORDS):
            rec = _store(rec, fdef, True, "approximate", rule="8.5 QUIT")
            rec.infer_former = True
            return rec

    if not _is_answered(fact, rec):
        if rec.reason == "AMBIGUOUS" and any(w in text for w in VAGUE_WORDS):
            rec.reason = "VAGUE_QUANTITY"
        return rec
    if fdef is None:
        return _unresolve(rec, "AMBIGUOUS", "UNKNOWN_FIELD")
    if fdef.field_id == USAGE_PATTERN_FIELD:
        return _map_usage_pattern(rec, fact, fdef, ctx)

    value, precision = fact["value"], fact.get("precision", "exact")
    if fact.get("_approx"):
        precision = "approximate"
    if fdef.field_name == "diagnosed" and value is True and any(w in text for w in PRE_DISEASE_WORDS):
        return _store(rec, fdef, False, "approximate", rule="8.5 PRE_DISEASE")   # 전단계는 진단 아님
    if fdef.field_id == "Q7.frequency" and isinstance(value, str):
        compact = value.replace(" ", "")
        for word, (count, period) in FREQUENCY_WORDS.items():
            if compact == word.replace(" ", ""):
                rec = _store(rec, fdef, count, "approximate" if count != 7 else "exact")
                rec.period = period
                return rec
    if fdef.data_type in ("integer", "number"):
        return _store_number(rec, fdef, value, precision)
    return _store(rec, fdef, value, precision)


def _store_number(rec, fdef, value, precision="exact"):
    """숫자 칸: 범위는 중앙값, 기간 환산, 정수 칸 .5 방향 규칙 (8.2~8.4)."""
    q = parse_quantity(value, fdef)
    if not q.ok:
        status = "PARTIAL" if q.reason == "MISSING_NUMBER" else "AMBIGUOUS"
        return _unresolve(rec, status, q.reason)
    number = q.value
    approximate = q.approximate or precision in ("approximate", "range")
    if fdef.unit == "day/week" and q.period in ("month", "year"):
        number = number * 7 / (30 if q.period == "month" else 365)
        approximate = True
    rounded = _round_for(fdef, number)
    approximate = approximate or rounded != number          # 반올림·1년 미만 보정도 근사
    rec = _store(rec, fdef, rounded, "approximate" if approximate else "exact")
    if rec.confirmed and q.period:
        rec.period = q.period
    return rec


def _round_for(fdef, number):
    if fdef.field_id in SMOKING_YEARS_FIELDS and 0 < number < 1:
        return 1                                            # 1년 미만 흡연 -> 1년 (8.5)
    if fdef.data_type != "integer" or float(number).is_integer():
        return int(number) if float(number).is_integer() else number
    frac = number - math.floor(number)
    if abs(frac - 0.5) < 1e-9:
        if fdef.item_id in ROUND_DOWN_ITEMS:
            return math.floor(number)                       # 신체활동: 내림
        return math.ceil(number)                            # 흡연·그 밖: 올림
    return int(round(number))


def _map_usage_pattern(rec, fact, fdef, ctx):
    """Q6_1: 사용 일수(숫자·범위·표현) -> use_pattern 선택지 (5장, 8.2).

      - '매일', '거의 매일', '아니요' 같은 표현 -> 선택지
      - 범위는 중앙값(.5 올림), 선택지 3칸 이상에 걸치면 재질문 (걸친 칸을 choices로)
      - 사용 일수 >= 기준 기간(28~31일) 또는 30·31일 -> 매일, 31일 초과 -> OUT_OF_RANGE
      - "주 n번"은 × 30/7 (8.4)
    """
    value, precision = fact["value"], fact.get("precision", "exact")
    if isinstance(value, str):
        code = to_option(fdef, value)
        if code is not None:
            approx = value.replace(" ", "") == "거의매일"
            return _store(rec, fdef, code, "approximate" if approx else "exact")

    q = parse_quantity(value, integer=True)
    if not q.ok:
        status = "PARTIAL" if q.reason == "MISSING_NUMBER" else "AMBIGUOUS"
        return _unresolve(rec, status, q.reason)
    factor = {"week": 30 / 7, "year": 1 / 12}.get(q.period, 1)
    days, approximate = q.value * factor, q.approximate or factor != 1 or precision == "approximate"

    if q.is_range:
        low_idx = _usage_index(_round_up_half(q.low * factor), ctx)
        high_idx = _usage_index(_round_up_half(q.high * factor), ctx)
        if low_idx is None or high_idx is None:
            return _unresolve(rec, "AMBIGUOUS", "OUT_OF_RANGE")
        if high_idx - low_idx >= 2:
            _unresolve(rec, "AMBIGUOUS", "RANGE_SPANS_OPTIONS")
            rec.choices = [USAGE_PATTERN_RANGES[i][0] for i in range(low_idx, high_idx + 1)]
            return rec
    idx = _usage_index(_round_up_half(days), ctx)
    if idx is None:
        return _unresolve(rec, "AMBIGUOUS", "OUT_OF_RANGE")            # 예: 35일
    return _store(rec, fdef, USAGE_PATTERN_RANGES[idx][0], "approximate" if approximate else "exact")


def _round_up_half(x):
    frac = x - math.floor(x)
    return math.ceil(x) if abs(frac - 0.5) < 1e-9 or frac > 0.5 else math.floor(x)


def _usage_index(days, ctx):
    if days < 0 or days > MONTH_MAX_DAYS:
        return None
    if days >= ctx.window_days and days > 0:
        return len(USAGE_PATTERN_RANGES) - 1                            # 기간 전체 -> 매일
    for i, (_, low, high) in enumerate(USAGE_PATTERN_RANGES):
        if low <= days <= high:
            return i
    return None


# ================================================================ 2. 시간 (시 + 분)

def _normalize_duration(item, parts, ctx):
    """Q8_2, Q9_2: hours + minutes -> duration_minutes. "1시간 반", "30분~1시간"도 처리."""
    fdef = ctx.catalog.get(item, "duration_minutes")
    facts = list(parts.values())
    rec = _base_record(facts[0], f"{item}.duration_minutes", "")
    rec.sources = [f"{f['item_id']}/{f['field']}" for f in facts]
    rec.evidence = [f.get("evidence") for f in facts]
    rec.raw_value = {k: f.get("value") for k, f in parts.items()}

    for f in facts:
        if not _is_answered(f, rec):
            return rec
    total, approximate = 0, False
    for name, f in parts.items():
        q = parse_quantity(f["value"], fdef if name == "minutes" else None)
        if not q.ok:
            status = "PARTIAL" if q.reason == "MISSING_NUMBER" else "AMBIGUOUS"
            return _unresolve(rec, status, q.reason)
        total += q.value * (60 if name == "hours" else 1)
        approximate = approximate or q.approximate or f.get("precision") in ("approximate", "range")
    return _store(rec, fdef, _round_for(fdef, total), "approximate" if approximate else "exact")


# ================================================================ 3. 음주량

def _normalize_amounts(groups, ctx):
    """Q7_1, Q7_2: amounts[i] 묶음 -> 주종별 amount·unit 2행 (4장, 8.5).

      - 주종: 5주종·동의어는 그 칸, 혼합주·기타 술은 도수가 가장 가까운 칸 (원래 이름·도수 보존)
      - 단위: 잔·병·캔·cc, 리터 ×1000, "개" -> 병, 막걸리 사발 -> 잔, 피처 -> 2,200cc
      - 같은 주종 여러 번: 같은 단위는 합산, 소주 병+잔은 잔(1병=7잔), 그 밖은 cc 합산
      - "셋이서 3병" -> 1인 1병, "조금" -> 1잔(곁들임일 때), 최대량 "평소랑 같아" -> 평소 값
    """
    parsed = [_parse_amount_group(item, idx, parts, ctx)
              for (item, idx), parts in sorted(groups.items())]
    _resolve_group_refs(parsed, ctx)

    records, by_key = [], {}
    for p in parsed:
        if p["error"]:
            records.extend(_amount_error_records(p))
        else:
            by_key.setdefault((p["item"], p["repeat_key"]), []).append(p)
    for (item, key), group in by_key.items():
        records.extend(_merge_same_beverage(item, key, group, ctx))
    return records


def _parse_amount_group(item, idx, parts, ctx):
    p = {"item": item, "idx": idx, "parts": parts, "repeat_key": None, "error": None,
         "status": None, "amount": None, "unit": None, "precision": "exact", "errors": {},
         "name": None, "abv": None, "small": False, "same_as_usual": None, "rule": None}

    bev = parts.get("beverage")
    if _answered(bev):
        name = str(bev["value"]).replace(" ", "").strip()
        p["name"] = name
        key, abv = BEVERAGE_CODE.get(name), ABV_BY_NAME.get(name)
        if key is None and abv is not None:
            key = cell_for_abv(abv)                         # 혼합주·기타 술 -> 도수 기준 칸 (4.2)
            p["rule"] = "4.3 ABV_CELL"
        if key is None:
            p["status"], p["error"] = "AMBIGUOUS", "OTHER_BEVERAGE"
            return p
        if key not in ctx.catalog.get(item, "amount").repeat_keys:
            p["status"], p["error"] = "AMBIGUOUS", "INVALID_REPEAT_KEY"
            return p
        p["repeat_key"], p["abv"] = key, abv
    else:
        p["bev_missing"] = _missing(bev)

    # 수량
    amount_fact = parts.get("amount")
    amount_text = evidence_text(amount_fact)
    if not _answered(amount_fact):
        p["errors"]["amount"] = _missing(amount_fact)
    else:
        raw = amount_fact["value"]
        text = str(raw).strip() if isinstance(raw, str) else ""
        if text in SMALL_AMOUNT_WORDS:
            p["small"] = True
        elif item == "Q7_2" and isinstance(raw, str) and any(w in text for w in SAME_AS_USUAL_WORDS):
            p["same_as_usual"] = 1
        elif item == "Q7_2" and isinstance(raw, str) and any(w in text for w in DOUBLE_OF_USUAL_WORDS):
            p["same_as_usual"] = 2
        else:
            q = parse_quantity(raw)
            if not q.ok:
                status = "PARTIAL" if q.reason == "MISSING_NUMBER" else "AMBIGUOUS"
                p["errors"]["amount"] = (status, q.reason)
            else:
                p["amount"] = q.value
                if q.approximate or amount_fact.get("precision") in ("approximate", "range"):
                    p["precision"] = "approximate"

    # 단위
    unit_fact = parts.get("unit")
    if _answered(unit_fact):
        unit_text = str(unit_fact["value"]).strip()
    else:
        unit_text = _unit_from_text(evidence_text(unit_fact) + " " + amount_text)
        if unit_text is None and not (p["small"] or p["same_as_usual"]):
            p["errors"]["unit"] = _missing(unit_fact)
    if unit_text is not None:
        unit, factor, approx = _drink_unit(unit_text, p["repeat_key"], ctx.catalog.get(item, "unit"))
        if unit is None:
            p["errors"]["unit"] = ("AMBIGUOUS", "UNKNOWN_UNIT")
        else:
            p["unit"] = unit
            if p["amount"] is not None:
                p["amount"] *= factor
            if approx or not _answered(unit_fact):
                p["precision"] = "approximate"

    # 여럿이 나눠 마심 (8.5)
    m = _SHARED.search(amount_text) or _SHARED.search(evidence_text(unit_fact))
    if m and p["amount"] is not None:
        people = int(m.group(1)) if m.group(1) else _SHARED_WORDS[m.group(2)]
        if people > 1:
            p["amount"] = p["amount"] / people
            p["precision"] = "approximate"
    return p


def _unit_from_text(text):
    """단위가 빠진 경우 근거 문장에서 단위 표현을 찾는다 (예: "세 개" -> 개). 하나일 때만."""
    words = list(DRINK_UNIT) + list(PIECE_UNIT) + list(BOWL_UNIT) + ["피처"]
    tail = r"(?=$|[\s.,!?~]|예요|이에요|이요|요|씩|정도|쯤|가량|이나|만|를|을|은|는|이)"
    found = {w for w in words if re.search(r"(\d|[가-힣])\s*" + re.escape(w) + tail, text)}
    found = {w for w in found if not any(w != o and w in o for o in found)}
    return found.pop() if len(found) == 1 else None


def _drink_unit(text, key, unit_def):
    """단위 표현 -> (DB 단위, 곱할 값, 근사 여부). 정할 수 없으면 (None, None, None)."""
    if text in DRINK_UNIT:
        code, factor = DRINK_UNIT[text]
        return code, factor, False
    code = to_option(unit_def, text)
    if code:
        return code, 1, False
    if text in PIECE_UNIT and key in PIECE_UNIT[text]:
        return PIECE_UNIT[text][key], 1, True
    if text in BOWL_UNIT and key in BOWL_UNIT[text]:
        return BOWL_UNIT[text][key], 1, True
    if "피처" in text:
        size = re.search(r"(\d{3,4})", text)
        cc = PITCHER_MEASURED_CC.get(int(size.group(1)), int(size.group(1))) if size else PITCHER_DEFAULT_CC
        return "cc", cc, True
    return None, None, None


def _resolve_group_refs(parsed, ctx):
    """주종 생략·'조금'·'평소랑 같아'를 같은 대화의 다른 답으로 채운다 (6.2, 8.5)."""
    known = {p["repeat_key"] for p in parsed if p["repeat_key"]}
    known |= {k for (f, k) in ctx.previous if f in ("Q7_1.amount", "Q7_2.amount") and k}
    for p in parsed:
        if p["repeat_key"] is None and not p["error"]:
            if len(known) == 1:
                p["repeat_key"] = next(iter(known))
                p["precision"] = "approximate"
                p["rule"] = "6.2 BEVERAGE_INHERIT"
            else:
                p["status"], p["error"] = p["bev_missing"][0], "BEVERAGE_MISSING"
    for p in parsed:
        if p["error"]:
            continue
        if p["small"]:
            others = [o for o in parsed if o is not p and o["item"] == p["item"]
                      and not o["error"] and o["amount"] is not None]
            if others:
                p["amount"], p["unit"], p["precision"] = 1, "glass", "approximate"
                p["errors"].pop("unit", None)
            else:
                p["errors"]["amount"] = ("AMBIGUOUS", "VAGUE_QUANTITY")
        if p["same_as_usual"]:
            usual = [o for o in parsed if o["item"] == "Q7_1" and o["repeat_key"] == p["repeat_key"]
                     and not o["error"] and not o["errors"]]
            if usual:
                amount, unit = usual[0]["amount"], usual[0]["unit"]
            else:
                amount = ctx.previous.get(("Q7_1.amount", p["repeat_key"]))
                unit = ctx.previous.get(("Q7_1.unit", p["repeat_key"]))
            if amount is None or unit is None:
                p["errors"]["amount"] = ("PARTIAL", "MISSING")
            else:
                p["amount"], p["unit"] = amount * p["same_as_usual"], unit
                p["errors"].pop("unit", None)
                p["precision"] = "approximate"


def _answered(fact):
    return fact is not None and fact.get("semantic_status") == "ANSWERED" \
        and fact.get("value") is not None


def _missing(fact):
    status = (fact or {}).get("semantic_status")
    return SEMANTIC_TO_RESOLUTION.get(status, "PARTIAL"), status or "MISSING"


def _amount_records(item, key, facts):
    out = []
    for name in ("amount", "unit"):
        own = [f for f in facts if f["field"].endswith((".beverage", "." + name))] or facts
        rec = _base_record(own[-1], f"{item}.{name}", key)
        rec.sources = [f"{f['item_id']}/{f['field']}" for f in own]
        rec.evidence = [f.get("evidence") for f in own]
        rec.raw_value = {f["field"]: f.get("value") for f in own}
        out.append(rec)
    return out


def _amount_error_records(p):
    facts = [f for f in p["parts"].values() if f]
    recs = _amount_records(p["item"], p["repeat_key"], facts)
    for rec in recs:
        _unresolve(rec, p["status"], p["error"])
    return recs


def _merge_same_beverage(item, key, group, ctx):
    facts = [f for p in group for f in p["parts"].values() if f]
    amount_rec, unit_rec = _amount_records(item, key, facts)
    amount_def, unit_def = ctx.catalog.get(item, "amount"), ctx.catalog.get(item, "unit")
    drinks = [{"name": p["name"], "abv": p["abv"], "amount": p["amount"], "unit": p["unit"]}
              for p in group]
    rules = sorted({p["rule"] for p in group if p["rule"]})

    if any(p["errors"] for p in group):
        if len(group) == 1:                            # 한 묶음: 되는 칸은 저장, 안 되는 칸만 재질문
            p = group[0]
            for name, rec, fdef, value in (("amount", amount_rec, amount_def, p["amount"]),
                                           ("unit", unit_rec, unit_def, p["unit"])):
                if name in p["errors"]:
                    _unresolve(rec, *p["errors"][name])
                else:
                    _store(rec, fdef, value, p["precision"] if name == "amount" else "exact")
        else:                                          # 여러 묶음 중 문제 -> 그 사유로 함께 재질문
            status, reason = next(iter(next(p for p in group if p["errors"])["errors"].values()))
            for rec in (amount_rec, unit_rec):
                _unresolve(rec, status, reason)
        amount_rec.drinks = drinks
        return [amount_rec, unit_rec]

    units = {p["unit"] for p in group}
    precision = "approximate" if any(p["precision"] == "approximate" for p in group) else "exact"
    if len(units) == 1:                                # 같은 단위 -> 합산
        total, unit = sum(p["amount"] for p in group), units.pop()
    elif units == {"bottle", "glass"} and key in BOTTLE_TO_GLASS:
        rate = BOTTLE_TO_GLASS[key]                    # 소주 1병 = 7잔 (확인 질문 없음)
        total = sum(p["amount"] * (rate if p["unit"] == "bottle" else 1) for p in group)
        unit, precision = "glass", "approximate"
        rules.append("8.5 BOTTLE_TO_GLASS")
    else:                                              # 그 밖에 단위 섞임 -> cc 합산
        mls = [volume_ml(key, p["unit"], p["name"]) for p in group]
        if any(v is None for v in mls):                # 용량 기준 없는 단위 -> 재질문 (8.9)
            for rec in (amount_rec, unit_rec):
                _unresolve(rec, "AMBIGUOUS", "UNKNOWN_UNIT")
            amount_rec.drinks = drinks
            return [amount_rec, unit_rec]
        total = sum(p["amount"] * v for p, v in zip(group, mls))
        unit, precision = "cc", "approximate"
        rules.append("8.5 CC_SUM")

    total = int(total) if float(total).is_integer() else round(total, 2)
    for rec, fdef, value, prec in ((amount_rec, amount_def, total, precision),
                                   (unit_rec, unit_def, unit, "exact")):
        _store(rec, fdef, value, prec, rule=", ".join(rules) or None)
    amount_rec.drinks = drinks
    return [amount_rec, unit_rec]


# ================================================================ 의도 (거절·보류)

def _records_from_intents(intents, catalog):
    records = []
    for intent in intents:
        status = INTENT_TO_RESOLUTION.get(intent.get("intent"))
        if status is None:            # ASK_PURPOSE, OFF_TOPIC 등은 기록 없이 전달만
            continue
        for fdef in catalog.fields.values():
            if fdef.item_id == intent["item_id"]:
                records.append(AnswerRecord(
                    item_id=fdef.item_id, field_id=fdef.field_id, repeat_key="",
                    resolution_status=status, precision="unspecified",
                    reason=intent["intent"], sources=[f"{intent['item_id']}/intent"],
                    evidence=[intent.get("evidence")],
                ))
    return records


def _detect_corrections(records, previous):
    """이전 값과 다른 값 + 정정 표지어("아니라·잘못 말·말고·정정") -> CORRECTION (8.6)."""
    for rec in records:
        if not rec.confirmed or rec.change_reason == "CORRECTION":
            continue
        before = previous.get((rec.field_id, rec.repeat_key))
        if before is None or _same(before, rec.value):
            continue
        texts = " ".join(e.get("text", "") if isinstance(e, dict) else (e or "") for e in rec.evidence)
        if re.search(CORRECTION_PATTERN, texts):
            rec.change_reason = "CORRECTION"


# ================================================================ 4. 항목별 추론 (8.5)

def _infer(ctx):
    res, view = ctx.result, _view(ctx)

    # Q7: "주 3번" -> 기간 단위도 채움, "매일" -> 주 7회
    freq = _current(res, "Q7.frequency")
    if freq is not None and freq.confirmed and getattr(freq, "period", None):
        unit = _current(res, "Q7.unit")
        if unit is None:
            _add(ctx, "Q7.unit", freq.period, freq.precision, freq, rule="8.4 PERIOD")
        elif not unit.confirmed:
            _store(unit, ctx.catalog.fields["Q7.unit"], freq.period, "exact", rule="8.4 PERIOD")

    # Q4·Q5 "끊었어요" -> 상태 = 과거 흡연
    for parent, child in (("Q4.answer", "Q4_1.status"), ("Q5.answer", "Q5_1.status")):
        rec = _current(res, parent)
        if rec is not None and getattr(rec, "infer_former", False) \
                and _current(res, child) is None and (child, "") not in ctx.previous:
            _add(ctx, child, "former", "approximate", rec, rule="8.5 QUIT")

    # Q1 복약 <-> 진단
    for rec in list(res.records):
        if not rec.field_id.startswith("Q1.") or not rec.confirmed:
            continue
        row = rec.field_id.rsplit(".", 1)[0]
        if rec.field_id.endswith(".on_medication") and rec.value == 1 \
                and view.get((f"{row}.diagnosed", "")) is None:
            _add(ctx, f"{row}.diagnosed", True, "approximate", rec, rule="8.5 MED_IMPLIES_DIAGNOSIS")
        if rec.field_id.endswith(".diagnosed") and rec.value == 1 \
                and _current(res, f"{row}.on_medication") is None \
                and (f"{row}.on_medication", "") not in ctx.previous:
            new = _add(ctx, f"{row}.on_medication", None, None, rec)
            _unresolve(new, "PARTIAL", "MISSING")      # 진단은 말했는데 복약이 빠짐 -> 복약만 재질문

    # 운동을 전혀 안 한다 -> 세 문항 모두 0일
    zero = [r for r in res.records if r.field_id in ("Q8_1.days", "Q9_1.days", "Q10.days")
            and r.confirmed and r.value == 0 and re.search(NO_EXERCISE_PATTERN, _texts(r))]
    if zero:
        for fid in ("Q8_1.days", "Q9_1.days", "Q10.days"):
            if _current(res, fid) is None and (fid, "") not in ctx.previous:
                _add(ctx, fid, 0, "approximate", zero[0], rule="8.5 NO_EXERCISE")

    # 해당 없음: 술 안 마심 -> 음주량, 활동 0일 -> 활동 시간
    view = _view(ctx)
    if view.get(("Q7.does_not_drink", "")) == 1 and not view.get(("Q7.frequency", "")):
        for fid in ("Q7_1.amount", "Q7_1.unit", "Q7_2.amount", "Q7_2.unit"):
            if not any(r.field_id == fid for r in res.records):
                _not_applicable(ctx, fid, "", "Q7 does_not_drink")
    for days, time in (("Q8_1.days", "Q8_2.duration_minutes"), ("Q9_1.days", "Q9_2.duration_minutes")):
        if view.get((days, "")) == 0 and not any(r.field_id == time for r in res.records):
            _not_applicable(ctx, time, "", f"{days} = 0")


# ================================================================ 5. 교차 검증 (6.1)

def _cross_validate(ctx):
    """이번 턴 답변 + 이미 저장된 답변을 함께 본다.

    함축되는 쪽이 있으면 정리하고(approximate), 남으면 빠진 정보만 재질문한다.
    저장된 답은 바꾸지 않고, 바꿔야 하면 CORRECTION 행을 새로 만든다.
    """
    res, catalog = ctx.result, ctx.catalog
    current = {(r.field_id, r.repeat_key): r for r in res.records if r.confirmed}
    view = _view(ctx)

    def val(field_id, key=""):
        return view.get((field_id, key))

    def conflict(keys, reason):
        for k in keys:
            if k in current:
                _unresolve(current.pop(k), "CONFLICT", reason)

    def amounts_of(item):
        return [k for k in view if k[0] == f"{item}.amount" and k[1]]

    def set_value(field_id, value, rule, source):
        rec = current.get((field_id, ""))
        if rec is not None:
            _store(rec, catalog.fields[field_id], value, "approximate", rule=rule)
        else:
            new = _add(ctx, field_id, value, "approximate", source, rule=rule)
            new.change_reason = "CORRECTION"
        view[(field_id, "")] = value

    # R1. 술을 안 마신다면서 음주량·횟수가 있음
    if val("Q7.does_not_drink") == 1:
        if val("Q7.frequency"):                        # 횟수가 있으면 음주자로 정리
            set_value("Q7.does_not_drink", False, "R1", current.get(("Q7.frequency", "")))
        else:
            keys = [("Q7.does_not_drink", "")]
            for item in ("Q7_1", "Q7_2"):
                for k in amounts_of(item):
                    keys += [k, (f"{item}.unit", k[1])]
            if len(keys) > 1:
                conflict(keys, "CONFLICT_DOES_NOT_DRINK")

    # R2. 기간보다 많은 횟수 (예: 일주일에 10번) -> 마신 날 수로 재질문
    freq, unit = val("Q7.frequency"), val("Q7.unit")
    if freq is not None and unit in PERIOD_MAX and freq > PERIOD_MAX[unit]:
        for k in (("Q7.frequency", ""), ("Q7.unit", "")):
            if k in current:
                _unresolve(current.pop(k), "AMBIGUOUS", "COUNT_EXCEEDS_DAYS")

    # R3. 최대 음주량 < 평소 음주량 (단위가 달라도 mL로 비교)
    for _, key in amounts_of("Q7_1"):
        usual = _ml(val("Q7_1.amount", key), val("Q7_1.unit", key), key)
        most = _ml(val("Q7_2.amount", key), val("Q7_2.unit", key), key)
        if usual is not None and most is not None and most < usual:
            conflict([("Q7_1.amount", key), ("Q7_1.unit", key),
                      ("Q7_2.amount", key), ("Q7_2.unit", key)], "CONFLICT_MAX_BELOW_USUAL")

    # R4. 경험 없음인데 상세 답변 -> 상세가 경험을 함축하면 '예', 아니면 상세는 해당 없음
    for parent, child in (("Q4.answer", "Q4_1"), ("Q5.answer", "Q5_1"), ("Q6.answer", "Q6_1")):
        if val(parent) != 0:
            continue
        child_keys = [k for k in view if k[0].startswith(child + ".")]
        if not child_keys:
            continue
        parent_rec = current.get((parent, ""))
        said_no_later = parent_rec is not None and parent_rec.change_reason == "CORRECTION"
        if not said_no_later and _implies_experience(child, val):
            set_value(parent, True, "R4", current.get(child_keys[0]))
        else:
            for k in child_keys:
                if k in current:
                    res.records.remove(current.pop(k))
                _not_applicable(ctx, k[0], k[1], "R4")

    # R5. 현재 흡연 + 금연 기간 -> 다시 피우는 경우. 금연 후 연수는 해당 없음
    for item in ("Q4_1", "Q5_1"):
        k = (f"{item}.years_since_quit", "")
        if val(f"{item}.status") == "current" and val(*k) is not None:
            if k in current:
                res.records.remove(current.pop(k))
            _not_applicable(ctx, k[0], "", "R5")

    # R6. 활동 0일 + 활동 시간 -> 다른 활동의 시간. 0일 유지, 시간은 다른 문항 후보로
    for days_item, time_item in (("Q8_1", "Q8_2"), ("Q9_1", "Q9_2")):
        k = (f"{time_item}.duration_minutes", "")
        minutes = val(*k)
        if val(f"{days_item}.days") == 0 and minutes:
            rec = current.pop(k, None)
            if rec is not None:
                res.records.remove(rec)
                res.unmapped_facts.append({"field_id": k[0], "value": minutes, "note": "R6",
                                           "evidence": rec.evidence,
                                           "suggested_item": activity_item(_texts(rec))})
            _not_applicable(ctx, k[0], "", "R6")

    # R7. 진단 없음 + 복약 중 -> 진단 '예'로 정리 (처방약은 진료를 전제)
    for (field_id, key), value in list(view.items()):
        if field_id.endswith(".on_medication") and value == 1:
            diag = field_id.replace("on_medication", "diagnosed")
            if val(diag, key) == 0:
                set_value(diag, True, "R7", current.get((field_id, key)))


def _implies_experience(child, val):
    if child == "Q4_1":
        if val("Q4_1.status") == "former":
            return True
        count, years = val("Q4_1.daily_count"), val("Q4_1.total_years")
        return bool(count and years and count * 365 * years >= 100)
    if child == "Q5_1":
        return any(val(f"Q5_1.{f}") not in (None, 0) for f in ("status", "daily_count", "total_years"))
    return val("Q6_1.use_pattern") not in (None, "no")


def _ml(amount, unit, key):
    if amount is None or unit is None:
        return None
    per = volume_ml(key, unit)
    return None if per is None else amount * per


# ================================================================ 값 저장·검증

def _store(rec, fdef, value, precision, rule=None):
    """DB 정의로 타입·선택지·범위를 검증한 뒤 맞는 칸 하나에 값을 넣는다."""
    if fdef is None:
        return _unresolve(rec, "AMBIGUOUS", "UNKNOWN_FIELD")
    if precision not in ("exact", "approximate"):
        precision = "exact"
    rec.value_boolean = rec.value_number = rec.value_option = rec.value_text = None

    dtype = fdef.data_type
    if dtype == "boolean":
        if not isinstance(value, bool):
            text = str(value).strip() if value is not None else ""
            mapped = {"예": True, "네": True, "아니요": False, "아니오": False}.get(text)
            if mapped is None:
                return _unresolve(rec, "AMBIGUOUS", "TYPE_MISMATCH")
            value = mapped
        rec.value_boolean = int(value)
    elif dtype in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _store_number(rec, fdef, value, precision)
        if dtype == "integer" and float(value) != int(value):
            value = _round_for(fdef, value)
        if not _within(value, fdef.constraints) or \
                (fdef.unit == "minute" and value > MAX_MINUTES_PER_DAY):
            return _unresolve(rec, "AMBIGUOUS", "OUT_OF_RANGE")
        rec.value_number = int(value) if float(value).is_integer() else value
    elif dtype == "option":
        code = to_option(fdef, value)
        if code is None:
            _unresolve(rec, "AMBIGUOUS", "INVALID_OPTION")
            rec.choices = sorted(fdef.options)
            return rec
        rec.value_option = code
    else:
        if not isinstance(value, str):
            return _unresolve(rec, "AMBIGUOUS", "TYPE_MISMATCH")
        rec.value_text = value

    rec.resolution_status = "CONFIRMED"
    rec.precision = precision
    rec.reason = None
    if rule:
        rec.rule = rule
    return rec


def _within(value, cons):
    if "minimum" in cons and value < cons["minimum"]:
        return False
    if "maximum" in cons and value > cons["maximum"]:
        return False
    if "exclusive_minimum" in cons and value <= cons["exclusive_minimum"]:
        return False
    if "exclusive_maximum" in cons and value >= cons["exclusive_maximum"]:
        return False
    return True


# ================================================================ 보조 함수

def _base_record(fact, field_id, repeat_key):
    return AnswerRecord(
        item_id=field_id.rsplit(".", 1)[0] if "." in field_id else fact["item_id"],
        field_id=field_id,
        repeat_key=repeat_key,
        resolution_status="UNASKED",
        raw_value=fact.get("value"),
        sources=[fact.get("_source") or f"{fact['item_id']}/{fact['field']}"],
        evidence=[fact.get("evidence")],
    )


def _add(ctx, field_id, value, precision, source_rec, rule=None):
    """추론으로 새 행을 만든다. 근거는 추론의 바탕이 된 행의 것을 쓴다."""
    fdef = ctx.catalog.fields[field_id]
    rec = AnswerRecord(item_id=fdef.item_id, field_id=field_id, repeat_key="",
                       resolution_status="UNASKED",
                       sources=list(source_rec.sources) if source_rec else [],
                       evidence=list(source_rec.evidence) if source_rec else [])
    if value is not None:
        _store(rec, fdef, value, precision, rule=rule)
    ctx.result.records.append(rec)
    return rec


def _not_applicable(ctx, field_id, repeat_key, rule):
    entry = {"field_id": field_id, "repeat_key": repeat_key, "rule": rule}
    if entry not in ctx.result.not_applicable:
        ctx.result.not_applicable.append(entry)


def _current(res, field_id, key=""):
    return next((r for r in res.records if r.field_id == field_id and r.repeat_key == key), None)


def _view(ctx):
    view = dict(ctx.previous)
    view.update({(r.field_id, r.repeat_key): r.value for r in ctx.result.records if r.confirmed})
    return view


def _texts(rec):
    return " ".join(e.get("text", "") if isinstance(e, dict) else (e or "") for e in rec.evidence)


def _annotate_unmapped(result):
    for uf in result.unmapped_facts:
        if isinstance(uf, dict) and "suggested_item" not in uf:
            text = uf.get("text") or evidence_text(uf)
            item = activity_item(text)
            if item:
                uf["suggested_item"] = item


def _is_answered(fact, rec):
    """ANSWERED가 아니거나 값이 없으면 rec을 미확정으로 바꾸고 False."""
    status = fact.get("semantic_status")
    if status == "ANSWERED" and fact.get("value") is not None:
        return True
    _unresolve(rec, SEMANTIC_TO_RESOLUTION.get(status, "PARTIAL"), status or "MISSING")
    return False


def _unresolve(rec, status, reason):
    rec.resolution_status = status
    rec.reason = reason
    rec.precision = "unspecified"
    rec.needs_confirm, rec.confirm_reason = False, None
    rec.value_boolean = rec.value_number = rec.value_option = rec.value_text = None
    return rec


def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b
