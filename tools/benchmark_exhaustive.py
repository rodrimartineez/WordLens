"""Benchmark de extracción EXHAUSTIVA: cobertura de unidades léxicas por subtítulo.

Usa el modelo real de Ollama y tests/benchmark/exhaustive_cases.jsonl, donde
cada subtítulo lleva (revisado a mano):
  expected    unidades que deberían proponerse (cada una con formas alternativas)
  acceptable  propuestas razonables que no cuentan ni a favor ni en contra
  forbidden   lo que nunca debería salir (palabras gramaticales, "have to",
              palabras sueltas de un modismo ya detectado, errores de OCR...)

Métricas: recall (expected encontradas), precisión (aciertos / aciertos +
falsos positivos; lo aceptable no cuenta), prohibidos, duplicados y tiempo.

  python tools/benchmark_exhaustive.py --label nuevo --out bench/nuevo.json
  python tools/benchmark_exhaustive.py --impl C:/ruta/v1.1 --label v1.1 --out bench/v11.json
  python tools/benchmark_exhaustive.py --compare bench/v11.json bench/nuevo.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "benchmark" / "exhaustive_cases.jsonl"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark_extraction import match_key  # noqa: E402


def load_cases(only: set[str] | None = None) -> list[dict]:
    rows = [json.loads(x) for x in CASES.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if not only or r["id"] in only]


def score_case(case: dict, preds: list[str]) -> dict:
    expected = [{match_key(f) for f in alts} for alts in case["expected"]]
    acceptable = {match_key(a) for a in case["acceptable"]}
    forbidden = {match_key(f) for f in case["forbidden"]}
    hit = [False] * len(expected)
    fps, forb, dups, seen = [], [], [], set()
    for p in preds:
        key = match_key(p)
        if key in seen:
            dups.append(p)
            continue
        seen.add(key)
        idx = next((i for i, ks in enumerate(expected) if key in ks and not hit[i]), None)
        if idx is not None:
            hit[idx] = True
        elif any(key in ks for ks in expected) or key in acceptable:
            continue
        else:
            fps.append(p)
            if key in forbidden:
                forb.append(p)
    return {
        "id": case["id"],
        "text": case["text"],
        "expected": len(expected),
        "tp": sum(hit),
        "missed": [alts[0] for alts, h in zip(case["expected"], hit, strict=True) if not h],
        "false_positives": fps,
        "forbidden": forb,
        "duplicates": dups,
        "predicted": preds,
    }


def summarize(rows: list[dict], seconds: float) -> dict:
    exp = sum(r["expected"] for r in rows)
    tp = sum(r["tp"] for r in rows)
    fp = sum(len(r["false_positives"]) for r in rows)
    return {
        "lines": len(rows),
        "expected": exp,
        "found": tp,
        "missed": exp - tp,
        "recall": round(tp / exp, 3) if exp else 0,
        "false_positives": fp,
        "precision": round(tp / (tp + fp), 3) if tp + fp else 0,
        "forbidden": sum(len(r["forbidden"]) for r in rows),
        "duplicates": sum(len(r["duplicates"]) for r in rows),
        "complete_lines": sum(r["tp"] == r["expected"] for r in rows),
        "seconds_per_line": round(seconds / max(1, len(rows)), 2),
    }


def run(args: argparse.Namespace, cases: list[dict]) -> dict:
    os.environ.setdefault("WORDLENS_DATA_DIR", tempfile.mkdtemp(prefix="wl_bench_"))
    impl = Path(args.impl).resolve() if args.impl else ROOT
    sys.path.insert(0, str(impl))
    import wordlens  # noqa: F401

    loaded = Path(sys.modules["wordlens"].__file__).resolve().parent.parent
    assert loaded == impl, f"Se cargó {loaded}, no {impl}"
    logging.basicConfig(level=logging.WARNING)
    from wordlens.ai.ollama_client import OllamaClient
    from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer
    from wordlens.config.settings import Settings

    settings = Settings(ollama_model=args.model)
    client = OllamaClient(settings.ollama_url, 300)
    client.warm_up(args.model, "30m")
    analyzer = VocabularyAnalyzer(settings, client)
    rows, total = [], 0.0
    for case in cases:
        if hasattr(analyzer, "known_names"):
            analyzer.known_names = set()  # cada caso es independiente
        t0 = time.perf_counter()
        items = analyzer.analyze([case["text"]])
        dt = time.perf_counter() - t0
        total += dt
        preds = [it.expression for it in items]
        row = score_case(case, preds)
        row["seconds"] = round(dt, 2)
        rows.append(row)
        print(f"{case['id']} {dt:4.1f}s {preds}", flush=True)
    return {"label": args.label, "impl": str(impl), "model": args.model, "summary": summarize(rows, total), "cases": rows}


def report(result: dict, details: bool) -> None:
    print(f"\n=== {result['label']} ({result['model']}, {result['impl']})")
    print(json.dumps(result["summary"], indent=1))
    if details:
        for r in result["cases"]:
            if r["missed"] or r["false_positives"] or r["duplicates"]:
                print(f"{r['id']} {r['text']}")
                if r["missed"]:
                    print("   omitidos :", r["missed"])
                if r["false_positives"]:
                    print("   sobrantes:", r["false_positives"], "PROHIBIDOS:" if r["forbidden"] else "", r["forbidden"] or "")
                if r["duplicates"]:
                    print("   duplicados:", r["duplicates"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl")
    ap.add_argument("--model", default="maternion/ling-3.0-tiny:8b")
    ap.add_argument("--label", default="actual")
    ap.add_argument("--out")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--compare", nargs="*")
    ap.add_argument("--rescore", help="recalcula un resultado guardado con el conjunto actual")
    args = ap.parse_args()
    if args.compare:
        for path in args.compare:
            res = json.loads(Path(path).read_text(encoding="utf-8"))
            print(f"{res['label']:>12}: {json.dumps(res['summary'])}")
        return
    cases = load_cases(set(args.only) if args.only else None)
    if args.rescore:
        res = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in cases}
        rows = []
        for old in res["cases"]:
            row = score_case(by_id[old["id"]], old["predicted"])
            row["seconds"] = old["seconds"]
            rows.append(row)
        res["cases"] = rows
        res["summary"] = summarize(rows, sum(r["seconds"] for r in rows))
        report(res, args.details)
        Path(args.rescore).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return
    result = run(args, cases)
    report(result, args.details)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
