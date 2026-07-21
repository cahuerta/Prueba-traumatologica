"""
routers/casos_vivo_alumno.py
Endpoints PUBLICOS de la sesion en vivo - los usa el alumno desde su
celular (link/QR) y la pantalla proyectada. Ninguno requiere login de
interrogador. Complementa a casos_vivo_profesor.py (control de la sesion).
"""

from fastapi import APIRouter, HTTPException

from routers.auth import sb
from routers.casos_vivo_comun import (
    IngresoAlumnoIn,
    VotarIn,
    obtener_sesion,
    pregunta_actual,
    url_firmada_media,
)

router = APIRouter(prefix="/casos-vivo", tags=["casos-vivo-alumno"])


@router.post("/vivo/{codigo}/ingreso")
def ingreso_alumno_vivo(codigo: str, body: IngresoAlumnoIn):
    """El alumno entra desde el link/QR de la sesion, con nombre + RUT
    (reusa la tabla 'alumnos' ya validada en materiales.py)."""
    sesion = sb.table("sesiones_vivo").select("id").eq("codigo_acceso", codigo).execute().data
    if not sesion:
        raise HTTPException(404, "Codigo de sesion invalido")

    alumno = sb.table("alumnos").select("id").eq("rut", body.rut.strip()).execute().data
    if not alumno:
        raise HTTPException(403, "RUT no reconocido en el sistema")
    alumno_id = alumno[0]["id"]

    sb.table("alumnos").update({"nombre": body.nombre.strip()}).eq("id", alumno_id).execute()

    return {"sesion_id": sesion[0]["id"], "alumno_id": alumno_id}


@router.get("/vivo/{codigo}/actual")
def estado_actual_alumno(codigo: str):
    """Pantalla del alumno/proyector. Muestra el caso clinico completo
    (titulo + vineta + su media) y la pregunta activa. No expone la
    respuesta correcta ni la explicacion salvo que el estado sea 'cerrada'."""
    sesion = sb.table("sesiones_vivo").select("*").eq("codigo_acceso", codigo).single().execute().data
    if not sesion:
        raise HTTPException(404, "Codigo de sesion invalido")

    pregunta = pregunta_actual(sesion)
    if not pregunta:
        return {"estado": sesion["estado"], "caso": None, "pregunta": None}

    caso = pregunta["caso"]
    bp = pregunta["banco_preguntas"]

    salida = {
        "estado": sesion["estado"],
        "sesion_id": sesion["id"],
        "caso": {
            "titulo": caso["titulo"],
            "vineta_clinica": caso["vineta_clinica"],
            "media_url": url_firmada_media("casos", caso.get("media_url")),
            "media_tipo": caso.get("media_tipo"),
        },
        "pregunta_id": bp["id"],
        "pregunta": bp["pregunta"],
        "opciones": bp["opciones"],
        "media_url": url_firmada_media("preguntas", bp.get("media_url")),
        "media_tipo": bp["media_tipo"],
        "caso_actual_orden": sesion["caso_actual_orden"],
        "pregunta_actual_orden": sesion["pregunta_actual_orden"],
    }

    if sesion["estado"] == "cerrada":
        salida["correcta"] = bp["correcta"]
        # Se lee lo ya preparado y revisado de antemano - nunca se genera aqui.
        salida["explicacion"] = pregunta.get("explicacion_generada") or bp["explicacion"] or ""
        salida["fuentes"] = pregunta.get("fuentes_generadas") or []

    return salida


@router.post("/vivo/votar")
def votar(body: VotarIn):
    """Un voto por alumno por pregunta (protegido tambien por unique constraint en la BD)."""
    ya_voto = sb.table("votos_vivo").select("id").eq(
        "sesion_id", body.sesion_id
    ).eq("pregunta_id", body.pregunta_id).eq("alumno_id", body.alumno_id).execute().data
    if ya_voto:
        raise HTTPException(409, "Este alumno ya voto esta pregunta")

    sesion = obtener_sesion(body.sesion_id)
    if sesion["estado"] != "votando":
        raise HTTPException(409, "La votacion no esta abierta en este momento")

    res = sb.table("votos_vivo").insert({
        "sesion_id": body.sesion_id,
        "pregunta_id": body.pregunta_id,
        "alumno_id": body.alumno_id,
        "opcion": body.opcion,
    }).execute()
    return res.data[0]


@router.get("/vivo/{sesion_id}/resultados")
def resultados_agregados(sesion_id: str):
    """Solo el agregado por opcion (sin nombres), para la pantalla proyectada."""
    sesion = obtener_sesion(sesion_id)
    pregunta = pregunta_actual(sesion)
    if not pregunta:
        return {"total": 0, "conteo": {}}

    pregunta_id = pregunta["banco_preguntas"]["id"]
    votos = sb.table("votos_vivo").select("opcion").eq(
        "sesion_id", sesion_id
    ).eq("pregunta_id", pregunta_id).execute().data

    conteo = {}
    for v in votos:
        conteo[v["opcion"]] = conteo.get(v["opcion"], 0) + 1

    return {"total": len(votos), "conteo": conteo}
  
