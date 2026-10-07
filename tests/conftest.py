import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ollama_ready() -> bool:
    try:
        from vocablens.ai.ollama_client import OllamaClient
        from vocablens.config.settings import Settings

        return OllamaClient(timeout=3).has_model(Settings().ollama_model)
    except Exception:
        return False


def pytest_configure(config):
    config.addinivalue_line("markers", "llm: necesita Ollama con el modelo descargado (lento)")


def pytest_collection_modifyitems(config, items):
    if any("llm" in item.keywords for item in items) and not _ollama_ready():
        skip = pytest.mark.skip(reason="Ollama o el modelo no disponibles")
        for item in items:
            if "llm" in item.keywords:
                item.add_marker(skip)
