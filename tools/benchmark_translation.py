"""Benchmark de traducción contextual de candidatos (inglés -> español).

Compara motores sobre tests/benchmark/translation_cases.jsonl: cada caso es un
subtítulo y las unidades que hay que traducir, con traducciones de referencia
aceptables (revisadas a mano). Una salida cuenta como correcta si alguna de
sus opciones coincide con una referencia (sin tildes, mayúsculas, signos ni
artículo inicial) o la contiene como frase completa.

La coincidencia automática es estricta con las paráfrasis válidas que no están
en la lista: por eso el informe guarda todas las salidas para revisarlas a mano
(--details) y la cifra automática se presenta como lo que es.

Motores:
  ollama:<modelo>   traductor de WordLens (subtítulo completo + lista de ítems)
  argos             Argos Translate (local, ítem aislado; necesita --argos-python)
  mymemory          API pública de MyMemory (ítem aislado, sin clave, 5000 car./día)

Uso:
  python tools/benchmark_translation.py --engine ollama:gemma3:4b --out bench/gemma.json
  python tools/benchmark_translation.py --compare bench/a.json bench/b.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = ROOT / "tests" / "benchmark" / "translation_cases.jsonl"
ARTICLES = {"el", "la", "los", "las", "un", "una", "unos", "unas", "lo"}


def load_cases(only: set[str] | None = None) -> list[dict]:
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [c for c in cases if not only or c["id"] in only]


def norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^\w\s]", " ", text)
    words = text.split()
    while words and words[0] in ARTICLES:
        words = words[1:]
    return " ".join(words)


def options(translation: str | None) -> list[str]:
    if not translation:
        return []
    return [norm(p) for p in re.split(r"[/,;]", translation) if norm(p)]


def is_correct(translation: str | None, refs: list[str]) -> bool:
    refs_n = [norm(r) for r in refs]
    for opt in options(translation):
        for ref in refs_n:
            if opt == ref or f" {ref} " in f" {opt} ":
                return True
    return False


# --------------------------------------------------------------- motores
def run_ollama(model: str, cases: list[dict], url: str) -> list[dict]:
    from wordlens.ai.ollama_client import OllamaClient
    from wordlens.translation.translator import OllamaTranslator, TranslationItem

    client = OllamaClient(url, 300)
    tr = OllamaTranslator(client, model, keep_alive="10m")
    client.warm_up(model, "10m")
    out = []
    for case in cases:
        items = [TranslationItem(i["item"], i["type"]) for i in case["items"]]
        t0 = time.perf_counter()
        try:
            results = tr.translate(case["sentence"], items)
            error = None
        except Exception as exc:  # noqa: BLE001
            results, error = [None] * len(items), str(exc)
        out.append({"id": case["id"], "seconds": time.perf_counter() - t0, "results": results, "error": error})
        print(f"{case['id']} {out[-1]['seconds']:.1f}s {results}", flush=True)
    return out


ARGOS_SCRIPT = r"""
import json, sys
import argostranslate.package, argostranslate.translate
items = json.load(sys.stdin)
res = [argostranslate.translate.translate(t, "en", "es") for t in items]
print(json.dumps(res, ensure_ascii=False))
"""


def run_argos(python: str, cases: list[dict]) -> list[dict]:
    texts = [i["item"] for c in cases for i in c["items"]]
    t0 = time.perf_counter()
    proc = subprocess.run(
        [python, "-I", "-c", ARGOS_SCRIPT], input=json.dumps(texts), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    flat = json.loads(proc.stdout.strip().splitlines()[-1])
    per_item = (time.perf_counter() - t0) / max(1, len(texts))
    out, k = [], 0
    for c in cases:
        n = len(c["items"])
        out.append({"id": c["id"], "seconds": per_item * n, "results": flat[k : k + n], "error": None})
        k += n
    return out


def run_mymemory(cases: list[dict]) -> list[dict]:
    out = []
    for c in cases:
        results, error = [], None
        t0 = time.perf_counter()
        for it in c["items"]:
            q = urllib.parse.urlencode({"q": it["item"], "langpair": "en|es"})
            try:
                with urllib.request.urlopen(f"https://api.mymemory.translated.net/get?{q}", timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8"))
                results.append(data.get("responseData", {}).get("translatedText"))
            except Exception as exc:  # noqa: BLE001
                results.append(None)
                error = str(exc)
            time.sleep(0.3)
        out.append({"id": c["id"], "seconds": time.perf_counter() - t0, "results": results, "error": error})
        print(c["id"], results, flush=True)
    return out


# ---------------------------------------------------------------- informe
def score(cases: list[dict], runs: list[dict]) -> dict:
    by_id = {r["id"]: r for r in runs}
    total = ok = missing = 0
    by_tag: dict[str, list[int]] = {}
    for c in cases:
        r = by_id[c["id"]]
        for item, tr in zip(c["items"], r["results"], strict=True):
            good = is_correct(tr, item["refs"])
            total += 1
            ok += good
            missing += tr is None
            for tag in c["tags"]:
                by_tag.setdefault(tag, [0, 0])
                by_tag[tag][0] += good
                by_tag[tag][1] += 1
    secs = sum(r["seconds"] for r in runs)
    return {
        "items": total,
        "correct_auto": ok,
        "accuracy_auto": round(ok / total, 3) if total else 0,
        "missing": missing,
        "seconds_total": round(secs, 1),
        "seconds_per_line": round(secs / max(1, len(runs)), 2),
        "by_tag": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_tag.items())},
    }


def details(cases: list[dict], runs: list[dict]) -> list[str]:
    by_id = {r["id"]: r for r in runs}
    lines = []
    for c in cases:
        for item, tr in zip(c["items"], by_id[c["id"]]["results"], strict=True):
            mark = "OK " if is_correct(tr, item["refs"]) else "-- "
            lines.append(f"{mark}{c['id']} {item['item']!r:34} -> {tr!r}")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine")
    ap.add_argument("--out")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--argos-python")
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--rescore", nargs="*", help="recalcula la puntuación de resultados guardados")
    args = ap.parse_args()

    cases = load_cases(set(args.only) if args.only else None)
    if args.rescore:
        for path in args.rescore:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            ids = {r["id"] for r in data["runs"]}
            sub = [c for c in cases if c["id"] in ids]
            print(path, json.dumps(score(sub, data["runs"]), ensure_ascii=False))
            if args.details:
                print("\n".join(details(sub, data["runs"])))
        return

    eng = args.engine
    if eng.startswith("ollama:"):
        runs = run_ollama(eng.split(":", 1)[1], cases, args.url)
    elif eng == "argos":
        runs = run_argos(args.argos_python, cases)
    elif eng == "mymemory":
        runs = run_mymemory(cases)
    else:
        raise SystemExit(f"Motor desconocido: {eng}")
    summary = score(cases, runs)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.details:
        print("\n".join(details(cases, runs)))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps({"engine": eng, "summary": summary, "runs": runs}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
