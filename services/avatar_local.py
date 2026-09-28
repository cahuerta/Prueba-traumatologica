"""
services/avatar_local.py
Estado del avatar de Clases Formales, guardado SOLO en el Render Disk
(/var/data/avatar_local) -no en Supabase-, mismo patron que
preguntas_local.py. No toca los archivos de preguntas.

Formato del archivo /var/data/avatar_local/{sesion_id}.json:
{
  "respuestas": {
    "preg_id_1": {
      "estado": "preparando" | "lista" | "error",
      "pregunta": "...",
      "respuesta": "...",            # texto que dira el avatar
      "fuente": "curso" | "literatura" | "ninguna",
      "region": "Cadera" | null,
      "actualizada_at": "2026-09-28T..."
    }
  },
  "activo": {                        # lo que la proyeccion debe decir ahora (o null)
    "id": "uuid-unico-por-proyeccion",
    "pregunta_id": "...",
    "pregunta": "...",
    "respuesta": "...",
    "fuente": "...",
    "proyectada_at": "2026-09-28T..."
  }
}
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DIR_AVATAR_LOCAL = Path(os.getenv("AVATAR_LOCAL_DIR", "/var/data/avatar_local"))

# Las respuestas se escriben desde tareas en segundo plano: un lock por proceso
# evita que dos escrituras simultaneas se pisen.
_lock = threading.Lock()


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ruta(sesion_id: str) -> Path:
    return DIR_AVATAR_LOCAL / f"{sesion_id}.json"


def _cargar(sesion_id: str) -> dict:
    ruta = _ruta(sesion_id)
    if not ruta.exists():
        return {"respuestas": {}, "activo": None}
    try:
        data = json.loads(ruta.read_text(encoding="utf-8"))
        data.setdefault("respuestas", {})
        data.setdefault("activo", None)
        return data
    except (json.JSONDecodeError, OSError):
        return {"respuestas": {}, "activo": None}


def _guardar(sesion_id: str, data: dict) -> None:
    DIR_AVATAR_LOCAL.mkdir(parents=True, exist_ok=True)
    ruta = _ruta(sesion_id)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporal.replace(ruta)  # escritura atomica: nunca queda un archivo a medias


def marcar_preparando(sesion_id: str, pregunta_id: str, pregunta: str) -> bool:
    """Devuelve False si ya esta preparando o lista (no se repite el gasto)."""
    with _lock:
        data = _cargar(sesion_id)
        actual = data["respuestas"].get(pregunta_id)
        if actual and actual.get("estado") in ("preparando", "lista"):
            return False
        data["respuestas"][pregunta_id] = {
            "estado": "preparando",
            "pregunta": pregunta,
            "respuesta": "",
            "fuente": "ninguna",
            "region": None,
            "actualizada_at": _ahora(),
        }
        _guardar(sesion_id, data)
        return True


def guardar_resultado(sesion_id: str, pregunta_id: str, estado: str, respuesta: str,
                      fuente: str, region: Optional[str]) -> None:
    with _lock:
        data = _cargar(sesion_id)
        registro = data["respuestas"].get(pregunta_id, {})
        registro.update({
            "estado": estado,
            "respuesta": respuesta,
            "fuente": fuente,
            "region": region,
            "actualizada_at": _ahora(),
        })
        data["respuestas"][pregunta_id] = registro
        _guardar(sesion_id, data)


def estados(sesion_id: str) -> dict:
    """Para el mando: solo el estado de cada pregunta (sin el texto de la respuesta)."""
    data = _cargar(sesion_id)
    return {pid: r.get("estado") for pid, r in data["respuestas"].items()}


def proyectar(sesion_id: str, pregunta_id: str) -> Optional[dict]:
    """Deja la respuesta como activa para la proyeccion. None si no esta lista."""
    with _lock:
        data = _cargar(sesion_id)
        registro = data["respuestas"].get(pregunta_id)
        if not registro or registro.get("estado") != "lista":
            return None
        data["activo"] = {
            "id": uuid.uuid4().hex,
            "pregunta_id": pregunta_id,
            "pregunta": registro.get("pregunta", ""),
            "respuesta": registro.get("respuesta", ""),
            "fuente": registro.get("fuente", "ninguna"),
            "proyectada_at": _ahora(),
        }
        _guardar(sesion_id, data)
        return data["activo"]


def activo(sesion_id: str) -> Optional[dict]:
    return _cargar(sesion_id).get("activo")
