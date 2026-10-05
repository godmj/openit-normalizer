"""100건 데이터 전체를 정규화해 결과 파일과 요약을 만든다.

실행:  python -m normalizer.run
결과:  output/normalized.jsonl   사례별 정규화 결과
       output/summary.md         상태·사유별 집계
"""
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .catalog import Catalog
from .normalize import normalize

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "dataset" / "data" / "master.jsonl"
OUT = ROOT / "output"


def main():
    catalog = Catalog()
    OUT.mkdir(exist_ok=True)
    status_count, reason_count, rows = Counter(), Counter(), []

    with open(MASTER, encoding="utf-8") as f:
        cases = [json.loads(line) for line in f]

    for case in cases:
        result = normalize(case["labels"], catalog)
        for rec in result.records:
            status_count[rec.resolution_status] += 1
            if rec.reason:
                reason_count[rec.reason] += 1
        rows.append({
            "id": case["id"],
            "category": case["category"],
            "user_text": case["context"][-1]["content"],
            "records": [
                {k: v for k, v in asdict(r).items() if k not in ("evidence",)}
                for r in result.records
            ],
            "not_applicable": result.not_applicable,
            "needs_clarify": any(r.needs_clarify for r in result.records),
            "needs_confirm": any(r.needs_confirm for r in result.records),
        })

    with open(OUT / "normalized.jsonl", "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    lines = [
        "# 정규화 결과 요약",
        "",
        f"- 사례 수: {len(cases)}",
        f"- 생성된 답변 행: {sum(status_count.values())}",
        f"- 재질문 필요 사례: {sum(r['needs_clarify'] for r in rows)}",
        f"- 저장 후 확인 질문 사례: {sum(r['needs_confirm'] for r in rows)}",
        "",
        "## 상태별",
        "",
        "| resolution_status | 행 수 |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in status_count.most_common()],
        "",
        "## 미확정 사유별",
        "",
        "| reason | 행 수 |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in reason_count.most_common()],
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"저장 완료: {OUT / 'normalized.jsonl'}")


if __name__ == "__main__":
    main()
