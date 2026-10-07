"""Detección del estado de Ollama (sin depender de que esté instalado)."""

from wordlens.ai import ollama_setup
from wordlens.ai.ollama_client import OllamaClient, OllamaUnavailableError
from wordlens.ai.ollama_setup import OllamaState


class FakeClient:
    def __init__(self, *, running=True, models=("maternion/ling-3.0-tiny:8b",)):
        self.running, self.models = running, list(models)

    def version(self):
        if not self.running:
            raise OllamaUnavailableError("down")
        return "0.40.0"

    def has_model(self, model):
        return model in self.models


def test_ready():
    status = ollama_setup.check(FakeClient(), "maternion/ling-3.0-tiny:8b")
    assert status.ok and status.state is OllamaState.READY


def test_model_missing():
    status = ollama_setup.check(FakeClient(models=[]), "maternion/ling-3.0-tiny:8b")
    assert status.state is OllamaState.MODEL_MISSING
    assert "not downloaded" in status.message


def test_not_installed(monkeypatch):
    monkeypatch.setattr(ollama_setup, "find_ollama", lambda: None)
    status = ollama_setup.check(FakeClient(running=False), "m")
    assert status.state is OllamaState.NOT_INSTALLED


def test_installed_but_off_without_autostart(monkeypatch, tmp_path):
    monkeypatch.setattr(ollama_setup, "find_ollama", lambda: tmp_path / "ollama.exe")
    launched = []
    monkeypatch.setattr(ollama_setup, "launch_ollama", lambda: launched.append(1) or True)
    status = ollama_setup.check(FakeClient(running=False), "m", autostart=False)
    assert status.state is OllamaState.NOT_RUNNING
    assert launched == []


def test_autostart_recovers(monkeypatch, tmp_path):
    client = FakeClient(running=False)
    monkeypatch.setattr(ollama_setup, "find_ollama", lambda: tmp_path / "ollama.exe")

    def launch():
        client.running = True
        return True

    monkeypatch.setattr(ollama_setup, "launch_ollama", launch)
    assert ollama_setup.check(client, "maternion/ling-3.0-tiny:8b").ok


def test_localhost_uses_ipv4():
    # En Windows "localhost" intenta antes IPv6 y cada petición tardaba ~2 s.
    assert OllamaClient("http://localhost:11434").host == "127.0.0.1"
    assert OllamaClient("http://192.168.1.5:11434").host == "192.168.1.5"
