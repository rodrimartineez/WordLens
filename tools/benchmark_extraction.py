"""Benchmark de extracción de vocabulario: aciertos, omisiones y falsos positivos.

Usa el LLM real (Ollama) y el conjunto anotado tests/benchmark/extraction_cases.json.
No escribe en ningún documento: evalúa la salida de VocabularyAnalyzer.analyze()
(LLM + validación), que es lo que la aplicación escribiría en Word.

  .venv\\Scripts\\python.exe tools\\benchmark_extraction.py
  .venv\\Scripts\\python.exe tools\\benchmark_extraction.py --batch 2 --label nuevo-b2
  .venv\\Scripts\\python.exe tools\\benchmark_extraction.py --impl C:\\ruta\\version_anterior --label anterior
  .venv\\Scripts\\python.exe tools\\benchmark_extraction.py --compare a.json b.json

--impl apunta a una carpeta que contenga un paquete "wordlens" (otra versión del
código) para comparar versiones con exactamente las mismas frases y la misma
puntuación. La puntuación es independiente del código evaluado.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "benchmark" / "extraction_cases.json"

# ------------------------------------------------------------------ puntuación
_DROP = frozenset(
    "a an the to it him her them me us you someone somebody something sb sth "
    "one's oneself my your his our their be".split()
)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return "".join(c for c in text if not unicodedata.combining(c))


def _stem(word: str) -> str:
    """Raíz muy simple, sólo para comparar formas (underestimated ~ underestimate)."""
    from wordfreq import zipf_frequency

    w = word
    for suffix, repl in (("ies", "y"), ("ied", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            base = w[: -len(suffix)] + repl
            for cand in (base, base + "e", base[:-1] if len(base) > 3 and base[-1] == base[-2] else base):
                if zipf_frequency(cand, "en") >= 2.0:
                    return cand
    return w


def match_key(expression: str) -> str:
    tokens = re.findall(r"[a-z]+(?:'[a-z]+)?", _fold(expression).replace("-", " "))
    kept = [t for t in tokens if t not in _DROP] or tokens
    return " ".join(_stem(t) for t in kept)


def translation_ok(translation: str, accepted: list[str]) -> bool:
    folded = _fold(translation)
    return any(_fold(a) in folded for a in accepted)


def score(cases: list[dict], predictions: dict[str, list[dict]]) -> dict:
    per_case = []
    for case in cases:
        preds = predictions.get(case["id"], [])
        expected = case["expected"]
        optional = {match_key(o) for o in case.get("optional", [])}
        exp_keys = [{match_key(e) for e in item["expr"]} for item in expected]
        hit = [None] * len(expected)
        fps, neutral = [], []
        for p in preds:
            key = match_key(p["expression"])
            idx = next((i for i, ks in enumerate(exp_keys) if key in ks and hit[i] is None), None)
            if idx is None and any(key in ks for ks in exp_keys):
                neutral.append(p)  # duplicado de algo ya acertado
            elif idx is not None:
                hit[idx] = p
            elif key in optional:
                neutral.append(p)
            else:
                fps.append(p)
        tps = [(expected[i], h) for i, h in enumerate(hit) if h is not None]
        per_case.append(
            {
                "id": case["id"],
                "text": case["text"],
                "n_expected": len(expected),
                "tp": len(tps),
                "fn": len(expected) - len(tps),
                "fp": len(fps),
                "translation_ok": sum(translation_ok(h["translation"], e["es"]) for e, h in tps),
                "missed": [e["expr"][0] for e, h in zip(expected, hit) if h is None],
                "false_positives": [f"{p['expression']} = {p['translation']}" for p in fps],
                "found": [f"{h['expression']} = {h['translation']}" for _, h in tps],
                "neutral": [f"{p['expression']} = {p['translation']}" for p in neutral],
                "bad_translations": [
                    f"{h['expression']} = {h['translation']}" for e, h in tps if not translation_ok(h["translation"], e["es"])
                ],
            }
        )
    return {"cases": per_case, "summary": summarize(per_case)}


def _ratio(a: float, b: float) -> float:
    return round(a / b, 3) if b else 0.0


def summarize(per_case: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in per_case:
        n = c["n_expected"]
        groups["all"].append(c)
        groups[{0: "0 items", 1: "1 item", 2: "2 items"}.get(n, "3+ items")].append(c)
    out = {}
    for name, cs in groups.items():
        tp = sum(c["tp"] for c in cs)
        fn = sum(c["fn"] for c in cs)
        fp = sum(c["fp"] for c in cs)
        exp = tp + fn
        p = _ratio(tp, tp + fp)
        r = _ratio(tp, exp)
        out[name] = {
            "cases": len(cs),
            "expected": exp,
            "found": tp,
            "missed": fn,
            "false_positives": fp,
            "precision": p,
            "recall": r,
            "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
            "translations_ok": _ratio(sum(c["translation_ok"] for c in cs), tp),
            "cases_with_fp": sum(1 for c in cs if c["fp"]),
        }
    multi = [c for c in per_case if c["n_expected"] >= 2]
    out["multi"] = {
        "cases": len(multi),
        "cases_with_2plus_found": sum(1 for c in multi if c["tp"] >= 2),
        "cases_complete": sum(1 for c in multi if c["fn"] == 0),
        "cases_with_only_1_found": sum(1 for c in multi if c["tp"] == 1),
    }
    return out


# ------------------------------------------------------------------ ejecución
class _Counter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.json_errors = 0
        self.discarded: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "no es JSON válido" in msg:
            self.json_errors += 1
        elif msg.startswith("Descartado"):
            self.discarded.append(msg)


def run(args: argparse.Namespace, cases: list[dict]) -> dict:
    os.environ.setdefault("WORDLENS_DATA_DIR", tempfile.mkdtemp(prefix="wordlens_bench_"))
    impl = Path(args.impl).resolve() if args.impl else ROOT
    sys.path.insert(0, str(impl))
    from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer
    from wordlens.config.settings import Settings

    import wordlens

    loaded_from = Path(wordlens.__file__).resolve().parent
    assert loaded_from.parent == impl, f"Se cargó {loaded_from}, no {impl}"

    settings = Settings(setup_complete=True, ollama_url="http://127.0.0.1:11434")
    if args.model:
        settings.ollama_model = args.model
    if args.no_hints:
        settings.candidate_hints = False
    if args.no_review:
        settings.review_pass = False
    analyzer = VocabularyAnalyzer(settings)
    counter = _Counter()
    logging.getLogger("wordlens").addHandler(counter)
    logging.getLogger("wordlens").setLevel(logging.INFO)

    analyzer.analyze(["Warm-up: the model is loading."])  # carga el modelo fuera de la medida
    predictions: dict[str, list[dict]] = {c["id"]: [] for c in cases}
    latencies = []
    for i in range(0, len(cases), args.batch):
        group = cases[i : i + args.batch]
        t0 = time.perf_counter()
        items = analyzer.analyze([c["text"] for c in group])
        latencies.append(time.perf_counter() - t0)
        for item in items:
            owner = next((c for c in group if item.context.strip() == c["text"].strip()), None)
            if owner is None:  # contexto = lote entero: se asigna a la primera frase que contenga la palabra
                head = item.expression.split()[0]
                owner = next((c for c in group if head[:4] in c["text"].lower()), group[0])
            predictions[owner["id"]].append({"expression": item.expression, "translation": item.translation})
        print(".", end="", flush=True)
    print()
    result = score(cases, predictions)
    result["meta"] = {
        "label": args.label,
        "impl": str(impl),
        "model": settings.ollama_model,
        "batch": args.batch,
        "llm_calls": len(latencies),
        "mean_call_seconds": round(sum(latencies) / len(latencies), 2),
        "total_seconds": round(sum(latencies), 1),
        "invalid_json_responses": counter.json_errors,
        "validator_discards": len(counter.discarded),
    }
    return result


def print_report(result: dict) -> None:
    m = result["meta"]
    print(f"\n=== {m['label']}  (modelo {m['model']}, {m['batch']} línea(s)/llamada, {m['impl']})")
    print(
        f"Llamadas: {m['llm_calls']}  media {m['mean_call_seconds']}s  total {m['total_seconds']}s  "
        f"JSON inválido: {m['invalid_json_responses']}  descartes del validador: {m['validator_discards']}"
    )
    header = f"{'grupo':<10}{'casos':>6}{'esper.':>7}{'acier.':>7}{'perd.':>7}{'FP':>5}{'prec.':>7}{'recall':>8}{'F1':>7}{'trad.ok':>9}"
    print(header)
    for name in ("0 items", "1 item", "2 items", "3+ items", "all"):
        s = result["summary"].get(name)
        if not s:
            continue
        print(
            f"{name:<10}{s['cases']:>6}{s['expected']:>7}{s['found']:>7}{s['missed']:>7}{s['false_positives']:>5}"
            f"{s['precision']:>7.2f}{s['recall']:>8.2f}{s['f1']:>7.2f}{s['translations_ok']:>9.2f}"
        )
    mu = result["summary"]["multi"]
    print(
        f"Frases con 2+ elementos: {mu['cases']} | con 2+ detectados: {mu['cases_with_2plus_found']} | "
        f"completas: {mu['cases_complete']} | sólo 1 detectado: {mu['cases_with_only_1_found']}"
    )


def print_details(result: dict) -> None:
    for c in result["cases"]:
        flag = "OK " if not c["fn"] and not c["fp"] else "   "
        print(f"{flag}{c['id']} [{c['tp']}/{c['n_expected']}, FP {c['fp']}] {c['text']}")
        for f in c["found"]:
            print(f"      + {f}")
        for f in c["neutral"]:
            print(f"      ~ {f}")
        for f in c["missed"]:
            print(f"      - (perdido) {f}")
        for f in c["false_positives"]:
            print(f"      ! (falso positivo) {f}")
        for f in c["bad_translations"]:
            print(f"      ? (traducción no reconocida) {f}")


def compare(paths: list[str]) -> None:
    results = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    keys = ["found", "missed", "false_positives", "precision", "recall", "f1", "translations_ok"]
    print(f"{'métrica (all)':<22}" + "".join(f"{r['meta']['label']:>16}" for r in results))
    for k in keys:
        print(f"{k:<22}" + "".join(f"{r['summary']['all'][k]:>16}" for r in results))
    for k in ("cases_with_2plus_found", "cases_complete", "cases_with_only_1_found"):
        print(f"{k:<22}" + "".join(f"{r['summary']['multi'][k]:>16}" for r in results))
    print(f"{'FP en frases sin vocab.':<22}" + "".join(f"{r['summary']['0 items']['false_positives']:>16}" for r in results))
    print(f"{'seg/llamada':<22}" + "".join(f"{r['meta']['mean_call_seconds']:>16}" for r in results))
    print(f"{'JSON inválido':<22}" + "".join(f"{r['meta']['invalid_json_responses']:>16}" for r in results))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--impl", help="Carpeta que contiene otra versión del paquete wordlens")
    parser.add_argument("--model", help="Modelo de Ollama (por defecto, el de la configuración)")
    parser.add_argument("--batch", type=int, default=1, help="Frases por llamada al LLM (la app usa hasta 4)")
    parser.add_argument("--no-hints", action="store_true", help="Sin pistas de candidatos (sólo versión nueva)")
    parser.add_argument("--no-review", action="store_true", help="Sin segunda pasada (sólo versión nueva)")
    parser.add_argument("--label", default="actual")
    parser.add_argument("--out", help="Guardar el resultado en JSON")
    parser.add_argument("--only", help="Ids separados por comas")
    parser.add_argument("--details", action="store_true", help="Mostrar cada frase")
    parser.add_argument("-v", "--verbose", action="store_true", help="Mostrar la respuesta cruda del LLM")
    parser.add_argument("--compare", nargs="+", metavar="JSON", help="Comparar resultados guardados")
    args = parser.parse_args()
    if args.compare:
        compare(args.compare)
        return 0
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    result = run(args, cases)
    if args.details:
        print_details(result)
    print_report(result)
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
