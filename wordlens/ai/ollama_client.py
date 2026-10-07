"""Cliente mínimo de la API REST local de Ollama (sin dependencias externas).

Usa http.client para poder abortar una petición en curso desde otro hilo
(al pulsar el atajo de parada no hay que esperar a que el modelo termine).
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class OllamaUnavailableError(OllamaError):
    pass


class OllamaAborted(OllamaError):
    pass


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434", timeout: float = 180.0) -> None:
        parsed = urlparse(base_url)
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or 11434
        self.timeout = timeout
        self._lock = threading.Lock()
        self._active: http.client.HTTPConnection | None = None
        self._aborted = False

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> Any:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        with self._lock:
            self._active = conn
            self._aborted = False
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"} if payload else {}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                detail = data.decode("utf-8", "replace")[:300]
                raise OllamaError(f"Ollama respondió {resp.status}: {detail}")
            return json.loads(data.decode("utf-8"))
        except (ConnectionRefusedError, socket.gaierror) as exc:
            raise OllamaUnavailableError(
                f"No se puede conectar con Ollama en {self.host}:{self.port}. ¿Está en ejecución?"
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            with self._lock:
                aborted = self._aborted
            if aborted:
                raise OllamaAborted("Petición cancelada") from exc
            if isinstance(exc, TimeoutError | socket.timeout):
                raise OllamaError("Ollama tardó demasiado en responder") from exc
            raise OllamaUnavailableError(f"Error de comunicación con Ollama: {exc}") from exc
        finally:
            with self._lock:
                self._active = None
            conn.close()

    def abort(self) -> None:
        """Cancela la petición en curso (si la hay) desde cualquier hilo."""
        with self._lock:
            conn = self._active
            self._aborted = True
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ------------------------------------------------------------------ API
    def version(self) -> str:
        return self._request("GET", "/api/version", timeout=5).get("version", "?")

    def list_models(self) -> list[str]:
        data = self._request("GET", "/api/tags", timeout=10)
        return [m.get("name", "") for m in data.get("models", [])]

    def has_model(self, model: str) -> bool:
        names = self.list_models()
        wanted = model if ":" in model else f"{model}:latest"
        return wanted in names or model in names

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        temperature: float = 0.0,
        num_predict: int = 400,
        keep_alive: str = "30m",
    ) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,  # el modo razonamiento sólo añade latencia aquí
            "keep_alive": keep_alive,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        if schema is not None:
            body["format"] = schema
        return self._request("POST", "/api/chat", body)

    def warm_up(self, model: str, keep_alive: str = "30m") -> None:
        """Carga el modelo en memoria para que el primer subtítulo no espere."""
        self._request(
            "POST",
            "/api/generate",
            {"model": model, "prompt": "", "keep_alive": keep_alive, "stream": False},
        )
