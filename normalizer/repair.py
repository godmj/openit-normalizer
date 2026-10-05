"""LoRA-A 출력 보정 (명세 8.6).

정규화 전에 추출 JSON의 흔한 형식 오류를 고친다. 원본 labels는 바꾸지 않고 복사본을 돌려준다.
  1. 완전히 같은 fact 중복 제거                  (finetuned 059)
  2. 필드 별칭 -> DB 필드명 (_db_field에 기록)    (finetuned 059: answer)
  3. evidence 위치를 사용자 발화에서 다시 계산     (위치 정확 1/18, DB 트리거 오류 방지)
  4. 값 없는 fact의 근거 문장이 거부·보류 표현이면 intent로 바꿈 (finetuned 090)
숫자 문자열("29")과 "모름"의 AMBIGUOUS 표시는 normalize.py에서 처리한다.
"""
import copy
import json
import re

from .mappings import ACTIVITY_ITEM, DEFER_WORDS, FIELD_ALIASES, REFUSAL_WORDS

_UNANSWERED = {"MISSING", "AMBIGUOUS", "UNCERTAIN"}


def prepare(labels, utterance=None):
    labels = copy.deepcopy(labels or {})
    facts = labels.get("facts", []) or []
    intents = labels.setdefault("intents", [])
    labels.setdefault("relations", [])
    labels.setdefault("unmapped_facts", [])

    seen, kept = set(), []
    for fact in facts:
        key = (fact.get("item_id"), fact.get("field"), fact.get("semantic_status"),
               json.dumps(fact.get("value"), ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        alias = FIELD_ALIASES.get(fact.get("item_id"), {}).get(fact.get("field"))
        if alias:
            fact["_db_field"] = alias
        if utterance is not None:
            fact["evidence"] = fix_evidence(fact.get("evidence"), utterance)
        kept.append(fact)

    asked = {i.get("item_id") for i in intents}
    final = []
    for fact in kept:
        intent = _intent_from_text(fact)
        if intent and fact["item_id"] not in asked:
            intents.append({"item_id": fact["item_id"], "intent": intent,
                            "evidence": fact.get("evidence"), "source": "normalizer"})
            asked.add(fact["item_id"])
            continue
        final.append(fact)
    labels["facts"] = final
    return labels


def fix_evidence(evidence, utterance):
    """근거 문장을 발화에서 찾아 start·end를 다시 계산한다. 못 찾으면 None (지어내지 않음)."""
    if not isinstance(evidence, dict) or not evidence.get("text"):
        return evidence
    start = utterance.find(evidence["text"])
    if start < 0:
        return None
    fixed = dict(evidence)
    fixed["start"], fixed["end"] = start, start + len(evidence["text"])
    return fixed


def evidence_text(fact):
    ev = (fact or {}).get("evidence")
    if isinstance(ev, dict):
        return ev.get("text") or ""
    return ev if isinstance(ev, str) else ""


def _intent_from_text(fact):
    if fact.get("value") is not None or fact.get("semantic_status") not in _UNANSWERED:
        return None
    text = evidence_text(fact)
    if any(w in text for w in REFUSAL_WORDS):
        return "REFUSED"
    if any(w in text for w in DEFER_WORDS):
        return "REQUEST_SKIP"
    return None


def activity_item(text):
    """활동 이름 -> 문항 (Q8 고강도 / Q9 중강도 / Q10 근력). 목록에 없으면 None (8.9)."""
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    found = {item for name, item in ACTIVITY_ITEM.items() if name.replace(" ", "") in compact}
    return found.pop() if len(found) == 1 else None
