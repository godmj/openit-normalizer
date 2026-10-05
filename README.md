# openit-normalizer

건강문진 AI 에이전트 토이 프로젝트의 **③ 규칙 처리** 모듈입니다.
LoRA-A가 추출한 JSON(`facts / intents / relations / unmapped_facts`)을 받아
설문 DB에 저장할 수 있는 값으로 **정규화·매핑·검증**하고, 실제 DB에 **저장**합니다.

담당: 신민지 (데이터: 왕우석 `labeled_questionnaire_100_handoff`)
규칙 기준: 「자연어 응답 정규화 규칙 명세」 v0.4 (2026-10-05). 코드 주석의 (n.n)은 명세 절 번호입니다.

## 실행 방법

```bash
source .venv/bin/activate
python -m pytest -q          # 전체 테스트 (397개)
python -m normalizer.run     # 100건 정규화 -> output/normalized.jsonl, output/summary.md
```

`dataset/` 폴더에 우석 님 데이터 패키지가 있어야 합니다 (Git에는 올리지 않음).
외부 패키지 없이 Python 표준 라이브러리만 씁니다 (테스트에만 pytest 필요).

## 사용법

```python
from normalizer import Catalog, normalize
from normalizer.alcohol import alcohol_summary, answers_from_result

result = normalize(labels, Catalog(), previous=저장된_확정답,
                   utterance="사용자 발화 원문", today=상담일)
result.confirmed()       # 저장할 확정 값
result.to_clarify()      # 재질문할 칸 (reason = 사유 코드, choices = 보여줄 선택지)
result.not_applicable    # 답에 따라 묻지 않아도 되는 칸 (해당 없음)

answers, drinks = answers_from_result(result)
alcohol_summary(answers, drinks)   # 2층: 순수 알코올 g·표준잔 (추정치)
```

## 구조

| 파일 | 역할 |
|---|---|
| `normalizer/catalog.py` | DB에서 필드 타입·선택지·제약조건을 읽음 (선택지를 하드코딩하지 않음) |
| `normalizer/mappings.py` | **정규화 규칙표** (주종·도수 칸, 단위·용량, 선택지 표현, 어림 표현, 추론 단어, 재질문 문장 예) |
| `normalizer/parsing.py` | 표현 해석: 숫자·단위, 범위(중앙값), 어림·하한, 기간("주 3번") |
| `normalizer/repair.py` | LoRA-A 출력 보정: 중복 제거, 필드 별칭, evidence 위치 재계산, 거부·보류 감지 |
| `normalizer/normalize.py` | 정규화 본체: 정규화 → 항목별 추론(8.5) → 교차 검증 R1~R7(6장) |
| `normalizer/alcohol.py` | 2층: 원래 술 이름·도수로 순수 알코올·표준잔 계산 |
| `normalizer/store.py` | `answers / answer_revisions` 저장 (트랜잭션, revision 이력) |
| `normalizer/run.py` | 100건 일괄 실행과 요약 |
| `tests/` | 100건 엔진 비교, 명세 규칙별 테스트, 실제 DB 저장 테스트 |

## 핵심 원칙

정보가 남아 있으면 정규화로 채우고(`approximate`로 표시, 원문은 evidence에 보존),
재질문은 **정보가 없어서 추정하면 값이 2배 이상 갈릴 때만** 합니다.
모름·거부·보류는 기록하고 다시 묻지 않습니다. 저장 후 확인 질문은 없습니다.

## 주요 규칙 (명세 v0.4)

| 영역 | 규칙 |
|---|---|
| 주종 (4장) | 5주종·동의어는 그 칸. 혼합주·기타 술은 **도수 기준 칸**: 맥주 <5% · 막걸리 5~9% · 와인 9~15% · 소주 15~28% · 양주 ≥28%. 하이볼(7%)·소맥(8.25%, 3:7) → 막걸리, 매실주 → 와인, 사케·청주·복분자주 → 소주, 고량주 → 양주. 원래 이름·도수는 `drinks`에 보존 |
| 음주 단위 | 잔·병·캔·cc, 리터 ×1000, "n개" → 병, 막걸리 사발 → 잔, 피처 → 2,200cc(3000cc 피처 → 2,700cc), 짝·박스 → 재질문 |
| 같은 칸 여러 술 | 같은 단위 합산, 소주 병+잔 → 잔(1병=7잔), 그 밖은 cc 합산. 용량 기준 없는 단위가 섞이면 재질문 |
| 범위 (8.2) | 중앙값. 정수 칸 .5는 흡연·전자담배 올림 / 신체활동 내림. Q6_1은 선택지 3칸 이상 걸치면 재질문 |
| 어림 (8.3) | 쯤·정도 → approximate, 이상·넘게 → 하한, 미만 → 값−1, 거의 매일 → 6일·월 10-29일, 격일 3.5, 가끔·여러 번 → 재질문 |
| 기간 (8.4) | "주 n번" → 월 칸은 ×30/7, "한 달에 n번" → 주 칸은 ×7/30, Q7은 말한 단위 그대로 |
| Q6_1 매일 | 30·31일, 또는 사용 일수 ≥ 최근 한 달(지난달 같은 날~상담일, 28~31일). 31일 초과는 범위 밖 |
| 추론 (8.5) | 약 복용 → 진단 예, 진단만 말함 → 복약만 재질문, "병 없어" → 모든 행 아니요, 전단계 → 진단 아니요, "끊었어요" → Q4 예 + 과거 흡연, 1년 미만 흡연 → 1년, 운동 전혀 안 함 → 세 문항 0일, 셋이서 3병 → 1병 |
| 추가 (8.9) | 활동 이름 → 문항(문진표 예시만), 가열담배 스틱 = 개비·한 갑 = 20개비, 재질문 답과 앞선 일부 답 합치기 |

## 교차 검증 (6.1)

| 규칙 | 조건 | 처리 |
|---|---|---|
| R1 | 술 안 마심 + 횟수 | 음주자로 정리. 횟수 없이 음주량만 있으면 재질문 |
| R2 `COUNT_EXCEEDS_DAYS` | 주 7회·월 31회·연 366회 초과 | "술 마신 날 기준으로 며칠?" 재질문 |
| R3 | 최대 음주량 < 평소 (mL로 비교) | 재질문 |
| R4 | 경험 없음 + 상세 | 상세가 경험을 함축하면 '예', 아니면 상세는 해당 없음 |
| R5 | 현재 흡연 + 금연 기간 | 금연 기간은 해당 없음 |
| R6 | 활동 0일 + 시간 | 시간은 해당 없음, 다른 문항 후보로 `unmapped_facts`에 |
| R7 | 진단 없음 + 복약 중 | 진단 '예'로 정리 |

저장된 답을 바꿔야 하면 `change_reason = CORRECTION`인 새 revision으로 저장합니다.

## 사유 코드 (6.2)

| reason | 예시 |
|---|---|
| `MISSING` | "평균 네 번" (단위 없음), 진단은 말했는데 복약이 빠짐 |
| `VAGUE_QUANTITY` | "가끔", "여러 번" |
| `MISSING_NUMBER` | 일수 칸에 "네" |
| `OUT_OF_RANGE` | 주당 8일, 한 달 35일, 하루 1,500분 |
| `RANGE_SPANS_OPTIONS` | Q6_1 "1~15일" (`choices`에 걸친 칸) |
| `INVALID_OPTION` | 선택지에 없는 말 (`choices`에 선택지) |
| `BEVERAGE_MISSING` / `OTHER_BEVERAGE` / `UNKNOWN_UNIT` | 주종 없음(대화에 술이 하나면 그 술로 채움) / 사전에 없는 술 / 짝·박스 |
| `UNKNOWN_FIELD` | 별칭 매핑 후에도 DB에 없는 필드 (추출 오류) |
| `REFUSED` / `REQUEST_SKIP` | 거부 / 나중에 (다시 묻지 않음) |

재질문 문장 예시는 `mappings.CLARIFY_QUESTIONS`에 있습니다 (LoRA-B 참고용).

## 저장 규칙 (`store.py`)

- 한 턴 저장은 하나의 트랜잭션, 실패 시 전체 롤백
- 수정은 UPDATE가 아니라 새 revision 추가 (이력 보존)
- CONFIRMED 답을 미확정 결과로 덮어쓰지 않음 (`KEEP_CONFIRMED`)
- CONFIRMED 답과 다른 값은 CORRECTION일 때만 저장, 아니면 `CONFLICT_NEEDS_CONFIRM`

## 검증 결과

- 테스트 397개 통과 (Python 3.12)
- 100건: 진행 엔진(`state_after`)과 확정/미확정 판단 일치. 단, 규칙상 다르게 처리하는 2건은 별도 테스트
  - 082 "맥주 세 개" → 3병 (엔진은 단위 재질문)
  - 086 "끊은 상태" → Q4 예 + 과거 흡연 (엔진은 재질문)
- 100건: 실제 DB 복사본에 저장해 제약조건·트리거 통과
- 100건 결과: 148행 중 확정 132, 재질문 사례 **11 → 9**, 저장 후 확인 질문 **1 → 0**
- finetuned LoRA-A 예측 13건을 넣었을 때 정답과 일치: **8 → 12건** (남은 1건은 모델이 정정 값을 잘못 출력한 097)

## 팀 공유 사항

1. **데이터 정답과 다르게 처리하는 기준**: 082·086, 그리고 R4·R7(명시한 "아니요"를 함축으로 정리) → 데이터 추가 시 참고
2. **입력 추가**: `utterance`(발화 원문)를 넘기면 evidence 위치를 다시 계산하고, `today`(상담일)로 Q6_1 기준 기간을 계산
3. **MISSING → PARTIAL**: DB `resolution_status`에 MISSING이 없어 PARTIAL로 대응
4. **2026 문진표 원본**: "최근 한 달"이 30일 고정인지 문구 대조 필요
