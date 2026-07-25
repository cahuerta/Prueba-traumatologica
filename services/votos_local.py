"""
services/votos_local.py
Los votos de la pregunta ACTIVA se guardan en un archivo JSON en el
Render Disk montado en /var/data -no en Supabase-, mientras la votacion
esta en curso. Esto evita que 90 alumnos votando casi al mismo tiempo
disparen 90 escrituras concurrentes a Supabase (que es donde vive el
bug de concurrencia HTTP/2 bajo carga). Recien al CERRAR la votacion
(accion "cerrar_votacion") se vuelcan todos los votos acumulados a
Supabase de una sola vez, para el guardado permanente.

El Render Disk sobrevive a reinicios/redeploys del servicio -a
diferencia de la memoria RAM del proceso-, asi que un voto ya
registrado localmente no se pierde aunque el backend se reinicie a
mitad de una votacion.

Formato del archivo /var/data/votos_local/{sesion_id}.json:
{
  "pregunta_id": "uuid-de-la-pregunta-activa",
  "votos": {
    "alumno_id_1": {"opcion": 2, "votado_at": "2026-07-25T..."},
    "alumno_id_2": {"opcion": 0, "votado_at": "2026-07-25T..."}
  }
}
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DIR_VOTOS_LOCALES = Path(os.getenv("VOTOS_LOCAL_DIR", "/var/data/votos_local"))


def _ruta_archivo(sesion_id: str) -> Path:
    return DIR_VOTOS_LOCALES / f"{sesion_id}.json"


def _asegurar_directorio():
    DIR_VOTOS_LOCALES.mkdir(parents=True, exist_ok=True)


def _cargar(sesion_id: str) -> dict:
    ruta = _ruta_archivo(sesion_id)
    if not ruta.exists():
        return {"pregunta_id": None, "votos": {}}
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Archivo corrupto o ilegible: se parte de cero en vez de romper el flujo.
        return {"pregunta_id": None, "votos": {}}


def _guardar(sesion_id: str, data: dict):
    _asegurar_directorio()
    ruta = _ruta_archivo(sesion_id)
    ruta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def registrar_voto(sesion_id: str, pregunta_id: str, alumno_id: str, opcion: int) -> bool:
    """Registra el voto en el archivo local. Devuelve False si el alumno
    ya habia votado esta misma pregunta (no lo sobreescribe)."""
    data = _cargar(sesion_id)

    # Si la pregunta activa cambio respecto a lo que habia guardado,
    # se parte una hoja nueva -los votos de la pregunta anterior ya
    # deberian haberse volcado a Supabase al cerrar esa votacion-.
    if data["pregunta_id"] != pregunta_id:
        data = {"pregunta_id": pregunta_id, "votos": {}}

    if alumno_id in data["votos"]:
        return False

    data["votos"][alumno_id] = {
        "opcion": opcion,
        "votado_at": datetime.now(timezone.utc).isoformat(),
    }
    _guardar(sesion_id, data)
    return True


def obtener_resultados(sesion_id: str, pregunta_id: str) -> dict:
    """{"total": int, "conteo": {opcion_str: cantidad}} - misma forma
    que ya devuelve el endpoint de resultados agregados."""
    data = _cargar(sesion_id)
    if data["pregunta_id"] != pregunta_id:
        return {"total": 0, "conteo": {}}

    conteo: dict = {}
    for voto in data["votos"].values():
        opcion = voto["opcion"]
        conteo[opcion] = conteo.get(opcion, 0) + 1

    return {"total": len(data["votos"]), "conteo": conteo}


def obtener_alumnos_que_votaron(sesion_id: str, pregunta_id: str) -> list[dict]:
    """[{"alumno_id": ..., "opcion": ..., "votado_at": ...}, ...] para el
    panel de detalle del admin, mientras la votacion sigue en curso
    (todavia sin volcar a Supabase)."""
    data = _cargar(sesion_id)
    if data["pregunta_id"] != pregunta_id:
        return []
    return [
        {"alumno_id": alumno_id, "opcion": v["opcion"], "votado_at": v["votado_at"]}
        for alumno_id, v in data["votos"].items()
    ]


def volcar_y_limpiar(sesion_id: str) -> list[dict]:
    """Devuelve la lista de votos acumulados (para que el caller los
    inserte en Supabase) y limpia el archivo local. Se llama al cerrar
    la votacion."""
    data = _cargar(sesion_id)
    pregunta_id = data["pregunta_id"]
    votos = data["votos"]

    _guardar(sesion_id, {"pregunta_id": None, "votos": {}})

    if not pregunta_id:
        return []

    return [
        {
            "sesion_id": sesion_id,
            "pregunta_id": pregunta_id,
            "alumno_id": alumno_id,
            "opcion": v["opcion"],
        }
        for alumno_id, v in votos.items()
                                ]
  
