"""Measure the small synthetic labeled extraction/matching fixture."""
from __future__ import annotations

import json
from pathlib import Path

from imports import parse_schedule_file
from intelligence import analyze_report


ROOT = Path(__file__).resolve().parent


def evaluate() -> dict:
    examples = json.loads((ROOT / "sample-data" / "labeled_examples.json").read_text(encoding="utf-8"))
    rows = parse_schedule_file("project_schedule.csv", (ROOT / "sample-data" / "project_schedule.csv").read_bytes())
    activities = [{
        "id": row["activity_id"], "wbs": row.get("wbs", ""), "name": row["activity_name"],
        "discipline": row.get("discipline", ""), "location": row.get("location", ""),
        "aliases": row.get("aliases", []), "predecessors": row.get("predecessors", []),
    } for row in rows]
    extraction_total = extraction_correct = positive_total = positive_correct = 0
    top1 = top3 = 0
    cases = []
    for example in examples:
        result = analyze_report(example, activities)
        facts = result["facts"]
        expected = example["expected_fields"]
        for field, expected_value in expected.items():
            extraction_total += 1
            actual_value = facts.get(field)
            if isinstance(expected_value, (int, float)) and actual_value not in (None, ""):
                exact = float(actual_value) == float(expected_value)
            else:
                exact = str(actual_value or "").strip().casefold() == str(expected_value or "").strip().casefold()
            extraction_correct += int(exact)
            if expected_value not in (None, ""):
                positive_total += 1
                positive_correct += int(exact)
        ranked = [item["activity_id"] for item in result["candidates"]]
        target = example["target_activity_id"]
        top1 += int(bool(ranked) and ranked[0] == target)
        top3 += int(target in ranked[:3])
        cases.append({"id": example["id"], "expected_activity_id": target, "ranked_activity_ids": ranked, "top1_hit": bool(ranked) and ranked[0] == target, "top3_hit": target in ranked[:3]})
    count = len(examples)
    return {
        "fixture": "synthetic labeled examples; local deterministic extraction and heuristic ranking",
        "example_count": count,
        "field_checks": extraction_total,
        "field_exact_matches": extraction_correct,
        "field_exact_match_rate": round(extraction_correct / extraction_total, 4) if extraction_total else None,
        "nonempty_expected_facts": positive_total,
        "nonempty_expected_facts_exact": positive_correct,
        "nonempty_fact_recall": round(positive_correct / positive_total, 4) if positive_total else None,
        "top1_matches": top1,
        "top1_rate": round(top1 / count, 4) if count else None,
        "top3_matches": top3,
        "top3_rate": round(top3 / count, 4) if count else None,
        "cases": cases,
        "caveat": "Small synthetic fixture only. Scores are heuristic and this result is not a production accuracy or calibration claim.",
    }


if __name__ == "__main__":
    result = evaluate()
    output_path = ROOT / "sample-data" / "measured_results.json"
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
