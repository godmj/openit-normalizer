# openit-normalizer
건강문진 AI 에이전트 토이 프로젝트의 ③ 규칙 처리 모듈입니다.
LoRA-A가 추출한 JSON(`facts / intents / relations / unmapped_facts`)을 받아
설문 DB에 저장할 수 있는 값으로 정규화·매핑·검증하고, 실제 DB에 저장 합니다.
담당: 신민지 (데이터: 왕우석 `labeled_questionnaire_100_handoff`)

## 실행 방법
```bash
source .venv/bin/activate
python -m pytest -q          # 전체 테스트 (235개)
python -m normalizer.run     # 100건 정규화 -> output/normalized.jsonl, output/summary.md
```

`dataset/` 폴더에 우석 님 데이터 패키지가 있어야 합니다 (Git에는 올리지 않음).
외부 패키지 없이 Python 표준 라이브러리만 씁니다 (테스트에만 pytest 필요).

## 구조
| 파일 | 역할 |
| `normalizer/catalog.py` | DB에서 필드 타입·선택지·제약조건을 읽음 (코드에 선택지를 하드코딩하지 않음) |
| `normalizer/mappings.py` | 라벨 이름 -> DB 이름 대응표 (필드명, 주종, 단위, Q6_1 구간) |
| `normalizer/normalize.py` | 정규화 본체: `normalize(labels, catalog) -> NormalizationResult` |
| `normalizer/store.py` | `answers / answer_revisions` 저장 (트랜잭션, revision 이력) |
| `normalizer/run.py` | 100건 일괄 실행과 요약 |
| `tests/` | 100건 엔진 결과 비교 + 경계 사례 + 실제 DB 저장 테스트 |

## 변환 규칙
| 변환 | 라벨 (A 추출) | DB 저장 |
| 필드 이름 | `Q10 / answer = 3` | `Q10.days`, `value_number = 3` |
| 시간 합산 | `Q8_2 / hours = 1, minutes = 20` | `Q8_2.duration_minutes = 80` |
| 구간 매핑 | `Q6_1 / usage_days = 2` | `Q6_1.use_pattern = days_1_2` |
| 주종 분리 | `amounts[0] = 맥주, 2, 캔` | `Q7_1.amount (beer) = 2`, `Q7_1.unit (beer) = can` |
| 타입 칸 | `diagnosed = true` | `value_boolean = 1` |

## 확정하지 않는 경우 (사유 코드)
값을 추정해서 채우지 않고, `resolution_status`와 `reason`만 남겨 LangGraph가 재질문하도록 넘깁니다.
| reason | 예시 | status |
| `MISSING` | "평균 네 번" (단위 없음) | PARTIAL |
| `AMBIGUOUS` / `UNCERTAIN` | "여러 번 했어요", "기억 안 나요" | AMBIGUOUS / UNCERTAIN |
| `RANGE_SPANS_OPTIONS` | Q6_1 "8~12일" (두 선택지에 걸침) | AMBIGUOUS |
| `NO_MATCHING_OPTION` | Q6_1 "30일" (매일로 추정 금지) | AMBIGUOUS |
| `RANGE_NOT_STORABLE` | Q10 "3~4일" (숫자 칸에 범위 불가) | AMBIGUOUS |
| `OUT_OF_RANGE` | Q10 "8일" (최대 7) | AMBIGUOUS |
| `OTHER_BEVERAGE` | "하이볼 2잔" (분류 기준 미정) | AMBIGUOUS, 저장 안 함 |
| `MIXED_UNIT` | "소주 1병이랑 2잔" | AMBIGUOUS |
| `UNKNOWN_UNIT` | "맥주 세 피처" | AMBIGUOUS |
| `CONFLICT_WITH_PREVIOUS` | relations에 CONFLICT | CONFLICT |
| `REFUSED` / `REQUEST_SKIP` | "답하지 않겠습니다" / "나중에" | REFUSED / DEFERRED |

`needs_clarify`가 True(PARTIAL·AMBIGUOUS·CONFLICT)면 재질문, REFUSED·DEFERRED·UNCERTAIN은 보류입니다.

## 저장 규칙 (`store.py`)
- 한 턴 저장은 하나의 트랜잭션, 실패 시 전체 롤백
- 수정은 UPDATE가 아니라 새 revision 추가 (이력 보존)
- CONFIRMED 답을 미확정 결과로 덮어쓰지 않음 (`KEEP_CONFIRMED`)
- CONFIRMED 답과 다른 값은 CORRECTION일 때만 저장, 아니면 `CONFLICT_NEEDS_CONFIRM`

## 검증 결과
- 100건 전체: 진행 엔진(`state_after`)과 확정/미확정 판단 일치 (의도된 차이 1건, 아래 참고)
- 100건 전체: 실제 DB 복사본에 저장해 제약조건·트리거 통과
- 데이터에 없는 경계 사례 20여 개 별도 테스트

## 팀 확인이 필요한 사항
1. MISSING -> PARTIAL: DB `resolution_status`에 MISSING이 없어 PARTIAL로 대응함
2. 범위 값 형식: A 계약에 `precision=range`의 value 모양이 정의돼 있지 않아 `{"min","max"}`, `[a, b]`를 받도록 함
3. 기타 술 분류 (`other_beverage_mapping`, PENDING): 하이볼·사케·소맥 등 기준 필요
4. Q6_1 근사값: "2일쯤"은 경계 판단이 불가해 현재 재질문 처리
5. 같은 주종·같은 단위 중복 ("소주 1잔이랑 또 2잔"): 현재 확인 질문, 합산 허용 여부 결정 필요
6. labeled100_060 (30일): 엔진은 원시 `usage_days=30`을 확정 저장하지만 DB 필드는 `use_pattern`뿐이라 정규화는 미확정 처리. 원시 일수를 어디에 보관할지 결정 필요
7. 데이터 오류: labeled100_082 `plan.assistant_text`가 "맥주"를 "소주"로 표기 (B 정답 `response`는 정상)