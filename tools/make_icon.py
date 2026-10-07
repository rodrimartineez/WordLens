"""Regenera assets/wordlens.ico y assets/wordlens.png a partir de wordlens/ui/icon.py.

  .venv\\Scripts\\python.exe tools\\make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wordlens.ui.icon import save_icon_files  # noqa: E402


def main() -> int:
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    save_icon_files(assets / "wordlens.ico", assets / "wordlens.png")
    print(f"Icono generado en {assets}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
