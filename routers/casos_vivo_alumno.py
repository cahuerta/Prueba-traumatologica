"""
routers/casos_vivo_alumno.py
Endpoints PUBLICOS de la sesion en vivo - los usa el alumno desde su
celular (link/QR) y la pantalla proyectada. Ninguno requiere login de
interrogador. Complementa a casos_vivo_profesor.py (control de la sesion).
"""

from fastapi import APIRouter, HTTPException

from routers.auth import sb
from routers.conjuntos_comun import obtener_conjunto_activo_id
from routers import cache_vivo
from services import votos_local
from routers.casos_vivo_comun import (
    IngresoAlumnoIn,
    VotarIn,
    obtener_sesion,
    obtener_sesion_por_codigo,
    pregunta_actual,
    total_preguntas_caso,
    total_casos_presentacion,
    url_firmada_media,
)

router = APIRouter(prefix="/casos-vivo", tags=["casos-vivo-alumno"])


@router.post("/vivo/{codigo}/ingreso")
def ingreso_alumno_vivo(codigo: str, body: IngresoAlumnoIn):
    """El alumno entra desde el link/QR de la sesion, con nombre + RUT,
    validado contra el conjunto de alumnos actualmente activo."""
    sesion = sb.table("sesiones_vivo").select("id").eq("codigo_acceso", codigo).execute().data
    if not sesion:
        raise HTTPException(404, "Codigo de sesion invalido")

    conjunto_id = obtener_conjunto_activo_id()

    alumno = sb.table("alumnos").select("id").eq("rut", body.rut.strip()).eq("conjunto_id", conjunto_id).execute().data
    if not alumno:
        raise HTTPException(403, "RUT no reconocido en el conjunto activo")
    alumno_id = alumno[0]["id"]

    sb.table("alumnos").update({"nombre": body.nombre.strip()}).eq("id", alumno_id).execute()

    sesion_id = sesion[0]["id"]
    sb.table("asistencia_vivo").upsert({"sesion_id": sesion_id, "alumno_id": alumno_id}).execute()
    cache_vivo.invalidar_asistencia(sesion_id)

    return {"sesion_id": sesion_id, "alumno_id": alumno_id}


@router.get("/vivo/{codigo}/actual")
def estado_actual_alumno(codigo: str):
    """Pantalla del alumno/proyector. Muestra el caso clinico completo
    (titulo + vineta + su media) y la pregunta activa -con su propia
    media independiente, ej. una radiografia distinta por pregunta-.
    No expone la respuesta correcta ni la explicacion salvo que el
    estado sea 'cerrada'.

    'finalizada' avisa cuando esta pregunta era la ultima del ultimo
    caso Y ya esta revelada -sin este campo, Proyeccion (que solo hace
    polling de este endpoint, sin recibir nunca la respuesta directa
    del click "siguiente" que ve Admin) no tiene forma de distinguir
    "se acaba de revelar esta pregunta" de "esto fue lo ultimo de
    toda la presentacion": ambos casos devuelven exactamente los
    mismos campos, porque caso_actual_orden/pregunta_actual_orden no
    cambian despues del ultimo 'siguiente'."""
    sesion = obtener_sesion_por_codigo(codigo)

    pregunta = pregunta_actual(sesion)
    if not pregunta:
        return {"estado": sesion["estado"], "caso": None, "pregunta": None, "finalizada": False}

    caso = pregunta["caso"]

    total_preguntas = total_preguntas_caso(caso["id"])
    total_casos = total_casos_presentacion(sesion["presentacion_id"])
    es_ultima_pregunta = sesion["pregunta_actual_orden"] >= total_preguntas
    es_ultimo_caso = sesion["caso_actual_orden"] >= total_casos
    finalizada = sesion["estado"] == "cerrada" and es_ultima_pregunta and es_ultimo_caso

    salida = {
        "estado": sesion["estado"],
        "sesion_id": sesion["id"],
        "finalizada": finalizada,
        "caso": {
            "titulo": caso["titulo"],
            "vineta_clinica": caso["vineta_clinica"],
            "media_url": url_firmada_media("casos", caso.get("media_url")),
            "media_tipo": caso.get("media_tipo"),
        },
        "pregunta_id": pregunta["id"],
        "pregunta": pregunta["pregunta"],
        "opciones": pregunta["opciones"],
        "media_url": url_firmada_media("preguntas", pregunta.get("media_url")),
        "media_tipo": pregunta.get("media_tipo"),
        "caso_actual_orden": sesion["caso_actual_orden"],
        "pregunta_actual_orden": sesion["pregunta_actual_orden"],
    }

    if sesion["estado"] == "cerrada":
        salida["correcta"] = pregunta["correcta"]
        # Se lee lo ya preparado y revisado de antemano - nunca se genera aqui.
        salida["explicacion"] = pregunta.get("explicacion_generada") or ""
        salida["fuentes"] = pregunta.get("fuentes_generadas") or []

    return salida


@router.post("/vivo/votar")
def votar(body: VotarIn):
    """Un voto por alumno por pregunta. Se guarda en el archivo local del
    Render Disk (no en Supabase) mientras la votacion sigue en curso -esto
    evita que muchos alumnos votando casi al mismo tiempo disparen
    escrituras concurrentes a Supabase-. Se vuelca todo a Supabase de una
    sola vez cuando el admin cierra la votacion (ver avanzar_sesion)."""
    sesion = obtener_sesion(body.sesion_id)
    if sesion["estado"] != "votando":
        raise HTTPException(409, "La votacion no esta abierta en este momento")

    registrado = votos_local.registrar_voto(
        sesion_id=body.sesion_id,
        pregunta_id=body.pregunta_id,
        alumno_id=body.alumno_id,
        opcion=body.opcion,
    )
    if not registrado:
        raise HTTPException(409, "Este alumno ya voto esta pregunta")

    return {"ok": True}


@router.get("/vivo/{sesion_id}/resultados")
def resultados_agregados(sesion_id: str):
    """Solo el agregado por opcion (sin nombres), para la pantalla proyectada.
    Se lee del archivo local mientras la pregunta sigue activa -no toca
    Supabase-."""
    sesion = obtener_sesion(sesion_id)
    pregunta = pregunta_actual(sesion)
    if not pregunta:
        return {"total": 0, "conteo": {}}

    return votos_local.obtener_resultados(sesion_id, pregunta["id"])
    
