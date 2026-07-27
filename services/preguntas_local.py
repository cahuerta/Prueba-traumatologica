"""
services/preguntas_local.py
Las preguntas anonimas de una sesion de Clases Formales se guardan en un
archivo JSON en el Render Disk montado en /var/data -no directo en
Supabase-, a medida que van llegando. Esto evita insercion individual
por cada pregunta/upvote (mismo motivo que votos_local.py: evitar
escrituras concurrentes que disparen el bug de concurrencia HTTP/2 de
Supabase bajo carga).

A diferencia de los votos de casos clinicos, las preguntas NO tienen un
evento de "cierre" (estan abiertas toda la sesion, llegan en cualquier
momento). El volcado a Supabase no depende de una accion de cierre:
se aprovecha CADA poll del interrogador (GET /preguntas) para volcar en
batch lo que aun no se ha persistido, antes de responder con el estado
actual. Ver volcar_pendientes_a_supabase().

El Render Disk sobrevive a reinicios/redeploys del servicio -a
diferencia de la memoria RAM del proceso-, asi que ninguna pregunta o
upvote ya registrado localmente se pierde aunque el backend se reinicie
a mitad de una clase.

Formato del archivo /var/data/preguntas_local/{sesion_id}.json:
{
  "preguntas": {
    "preg_id_1": {
      "texto": "...",
      "autor_alumno_id": "uuid-del-alumno",
      "upvotes": ["alumno_id_1", "alumno_id_2"],
      "respondida": false,
      "creada_at": "2026-07-27T...",
      "volcada": false
    }
  }
}
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

DIR_PREGUNTAS_LOCALES = Path(os.getenv("PREGUNTAS_LOCAL_DIR", "/var/data/preguntas_local"))


def _ruta_archivo(sesion_id: str) -> Path:
    return DIR_PREGUNTAS_LOCALES / f"{sesion_id}.json"


def _asegurar_directorio():
    DIR_PREGUNTAS_LOCALES.mkdir(parents=True, exist_ok=True)


def _cargar(sesion_id: str) -> dict:
    ruta = _ruta_archivo(sesion_id)
    if not ruta.exists():
        return {"preguntas": {}}
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Archivo corrupto o ilegible: se parte de cero en vez de romper el flujo.
        return {"preguntas": {}}


def _guardar(sesion_id: str, data: dict):
    _asegurar_directorio()
    ruta = _ruta_archivo(sesion_id)
    ruta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def registrar_pregunta(sesion_id: str, autor_alumno_id: str, texto: str) -> str:
    """Registra una pregunta nueva en el archivo local. Devuelve el id
    generado para la pregunta."""
    data = _cargar(sesion_id)

    pregunta_id = str(uuid.uuid4())
    data["preguntas"][pregunta_id] = {
        "texto": texto,
        "autor_alumno_id": autor_alumno_id,
        "upvotes": [],
        "respondida": False,
        "creada_at": datetime.now(timezone.utc).isoformat(),
        "volcada": False,
    }
    _guardar(sesion_id, data)
    return pregunta_id


def registrar_upvote(sesion_id: str, pregunta_id: str, alumno_id: str) -> bool:
    """Agrega el upvote del alumno a la pregunta. Devuelve False si la
    pregunta no existe o si el alumno ya habia votado esa pregunta (no
    se duplica)."""
    data = _cargar(sesion_id)
    pregunta = data["preguntas"].get(pregunta_id)
    if not pregunta:
        return False

    if alumno_id in pregunta["upvotes"]:
        return False

    pregunta["upvotes"].append(alumno_id)
    _guardar(sesion_id, data)
    return True


def marcar_respondida(sesion_id: str, pregunta_id: str) -> bool:
    """El interrogador marca una pregunta como respondida. Devuelve
    False si la pregunta no existe."""
    data = _cargar(sesion_id)
    pregunta = data["preguntas"].get(pregunta_id)
    if not pregunta:
        return False

    pregunta["respondida"] = True
    _guardar(sesion_id, data)
    return True


def listar_preguntas(sesion_id: str) -> list[dict]:
    """Lista para el panel del interrogador, ordenada por cantidad de
    upvotes descendente. Sin ningun campo de identidad de alumno -ni
    autor_alumno_id ni la lista de upvotes se exponen tal cual, solo
    el conteo-."""
    data = _cargar(sesion_id)
    preguntas = [
        {
            "id": pregunta_id,
            "texto": p["texto"],
            "upvotes": len(p["upvotes"]),
            "respondida": p["respondida"],
            "creada_at": p["creada_at"],
        }
        for pregunta_id, p in data["preguntas"].items()
    ]
    preguntas.sort(key=lambda p: p["upvotes"], reverse=True)
    return preguntas


def volcar_pendientes_a_supabase(sesion_id: str) -> list[dict]:
    """Devuelve la lista de preguntas que aun no se han volcado a
    Supabase (para que el caller las inserte), y las marca como
    volcadas. Se llama en CADA poll del interrogador (GET /preguntas),
    no en un evento de cierre -las preguntas siguen abiertas toda la
    sesion-.

    Solo se vuelcan preguntas nuevas. Los upvotes y el estado
    'respondida' que cambien DESPUES del volcado inicial se resuelven
    con un UPDATE por parte del caller (no se rastrean como
    'pendientes' aca, para no complicar el archivo local con un segundo
    tipo de pendiente)."""
    data = _cargar(sesion_id)
    pendientes = []

    for pregunta_id, p in data["preguntas"].items():
        if p["volcada"]:
            continue
        pendientes.append({
            "id": pregunta_id,
            "sesion_id": sesion_id,
            "texto": p["texto"],
            "autor_alumno_id": p["autor_alumno_id"],
            "creada_at": p["creada_at"],
        })
        p["volcada"] = True

    if pendientes:
        _guardar(sesion_id, data)

    return pendientes
  
