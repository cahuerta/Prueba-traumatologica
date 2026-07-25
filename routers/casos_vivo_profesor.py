"""
routers/casos_vivo_profesor.py
Endpoints PROTEGIDOS (requieren login de interrogador) de la presentacion
dinamica en vivo. Cubre 2 momentos:

1) PREPARACION (antes de la clase): crear casos clinicos con su media,
   ESCRIBIR sus preguntas una a una y en orden (1-5) - cada pregunta es
   independiente del banco de examen, porque se redacta ya sabiendo el
   contexto de las preguntas anteriores del mismo caso. El interrogador
   escribe pregunta + respuesta correcta, Claude genera solo las 4
   alternativas falsas (mismo patron que preguntas.py), se revisan/editan
   y se guardan. Fundamento: Claude busca en los materiales docx de la
   region y redacta un BORRADOR, se revisa/edita y recien ahi se guarda.
   Armar presentaciones (varios casos, ordenados, reutilizables).
   GET /casos/{caso_id} y GET /presentaciones/{presentacion_id} sirven
   como lectura completa para el profesor (incluyen todas las preguntas).

2) CONTROL EN VIVO (durante la clase): iniciar sesion desde una
   presentacion, avanzar el ciclo abrir_votacion -> cerrar_votacion
   (discusion/fundamentos orales) -> revelar -> siguiente. 'revelar'
   NUNCA llama a Claude: solo expone lo que ya quedo guardado y
   revisado de antemano. Expone el detalle nombre->opcion para que el
   profesor elija a quien pedir que fundamente en voz alta. Tambien
   expone quien ha marcado asistencia (ingreso a la sesion), sin
   necesidad de haber votado ninguna pregunta.

   Los votos de la pregunta ACTIVA viven en un archivo local (Render
   Disk, ver services/votos_local.py) mientras la votacion esta en
   curso -no en Supabase-. Recien al ejecutar "cerrar_votacion" se
   vuelcan todos de una sola vez a Supabase (votos_vivo), para el
   guardado permanente.

   El estado de la sesion (estado/caso_actual_orden/pregunta_actual_orden)
   y la asistencia pasan por la cache en memoria (routers/cache_vivo.py):
   avanzar_sesion actualiza la cache directo en cada accion, y
   ver_asistencia_vivo se sirve de ahi salvo que alguien acabe de
   ingresar (invalidada en casos_vivo_alumno.py).

Complementa a casos_vivo_alumno.py (endpoints publicos del alumno).
"""

import random
import string
import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, UploadFile, File, HTTPException

from routers.auth import sb, get_current_interrogador
from routers.conjuntos_comun import obtener_conjunto_activo_id
from routers import cache_vivo
from services.claude_client import generar_alternativas
from services.fundamento_vivo import buscar_fundamento
from services import votos_local
from routers.casos_vivo_comun import (
    CasoIn,
    GenerarAlternativasCasoIn,
    PreguntaCasoIn,
    PreguntaCasoUpdateIn,
    FundamentoIn,
    PresentacionIn,
    PresentacionCasoIn,
    IniciarSesionIn,
    AccionIn,
    obtener_sesion,
    caso_actual,
    pregunta_actual,
    total_preguntas_caso,
    total_casos_presentacion,
    url_firmada_media,
)

router = APIRouter(prefix="/casos-vivo", tags=["casos-vivo-profesor"])


# ============================================================
# CASOS CLINICOS
# ============================================================

@router.post("/casos")
def crear_caso(c: CasoIn, interrogador: dict = Depends(get_current_interrogador)):
    row = c.model_dump()
    row["creado_por"] = interrogador["sub"]
    res = sb.table("casos_clinicos").insert(row).execute()
    return res.data[0]

@router.post("/casos/media")
async def subir_media_caso(
    tipo: str = Form(...),  # "foto" | "video"
    archivo: UploadFile = File(...),
    interrogador: dict = Depends(get_current_interrogador),
):
    if tipo not in ("foto", "video"):
        raise HTTPException(400, "Tipo invalido, debe ser 'foto' o 'video'")

    contenido = await archivo.read()
    storage_path = f"{int(time.time())}-{archivo.filename}"

    sb.storage.from_("casos").upload(
        storage_path, contenido, {"content-type": archivo.content_type}
    )

    return {"media_url": storage_path, "media_tipo": tipo}

@router.post("/casos/{caso_id}/media")
def asociar_media_caso(caso_id: str, media_url: str, media_tipo: str, interrogador: dict = Depends(get_current_interrogador)):
    if media_tipo not in ("foto", "video"):
        raise HTTPException(400, "Tipo invalido")
    res = sb.table("casos_clinicos").update({
        "media_url": media_url, "media_tipo": media_tipo
    }).eq("id", caso_id).execute()
    if not res.data:
        raise HTTPException(404, "Caso no encontrado")
    return res.data[0]

@router.get("/casos/{caso_id}/media")
def obtener_media_caso(caso_id: str, interrogador: dict = Depends(get_current_interrogador)):
    c = sb.table("casos_clinicos").select("media_url").eq("id", caso_id).single().execute().data
    if not c or not c["media_url"]:
        raise HTTPException(404, "Este caso no tiene foto/video")
    url = url_firmada_media("casos", c["media_url"])
    return {"url": url}

@router.get("/casos")
def listar_casos(region: Optional[str] = None, interrogador: dict = Depends(get_current_interrogador)):
    q = sb.table("casos_clinicos").select("*")
    if region:
        q = q.eq("region", region)
    return q.order("created_at", desc=True).execute().data

@router.get("/casos/{caso_id}")
def obtener_caso(caso_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Lectura completa del caso para el profesor: datos + todas sus preguntas."""
    caso = sb.table("casos_clinicos").select("*").eq("id", caso_id).single().execute().data
    if not caso:
        raise HTTPException(404, "Caso no encontrado")

    preguntas = sb.table("caso_preguntas").select(
        "id, orden, pregunta, opciones, correcta, media_url, media_tipo, explicacion_generada, fuentes_generadas"
    ).eq("caso_id", caso_id).order("orden").execute().data

    caso["preguntas"] = preguntas
    return caso

@router.delete("/casos/{caso_id}")
def borrar_caso(caso_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Borra el caso completo (sus preguntas se borran solas por cascada).
    Tambien lo saca de cualquier presentacion donde estuviera agregado."""
    sb.table("presentacion_casos").delete().eq("caso_id", caso_id).execute()
    sb.table("casos_clinicos").delete().eq("id", caso_id).execute()
    return {"ok": True}


# ---------------- PREGUNTAS DEL CASO: escritas secuencialmente, con IA solo para alternativas falsas ----------------

@router.post("/casos/{caso_id}/preguntas/generar-alternativas")
def generar_alternativas_pregunta_caso(caso_id: str, body: GenerarAlternativasCasoIn, interrogador: dict = Depends(get_current_interrogador)):
    """El interrogador escribe la pregunta + respuesta correcta. Claude propone
    solo las 4 alternativas falsas. No guarda nada todavia."""
    caso = sb.table("casos_clinicos").select("region").eq("id", caso_id).single().execute().data
    if not caso:
        raise HTTPException(404, "Caso no encontrado")

    incorrectas = generar_alternativas(
        pregunta=body.pregunta,
        respuesta_correcta=body.respuesta_correcta,
        region=caso["region"],
        complejidad="intermedia",
    )

    opciones = incorrectas + [body.respuesta_correcta]
    random.shuffle(opciones)
    correcta_idx = opciones.index(body.respuesta_correcta)

    return {"opciones": opciones, "correcta": correcta_idx}

@router.post("/casos/{caso_id}/preguntas/media")
async def subir_media_pregunta_caso(
    tipo: str = Form(...),  # "foto" | "video"
    archivo: UploadFile = File(...),
    interrogador: dict = Depends(get_current_interrogador),
):
    if tipo not in ("foto", "video"):
        raise HTTPException(400, "Tipo invalido, debe ser 'foto' o 'video'")

    contenido = await archivo.read()
    storage_path = f"{int(time.time())}-{archivo.filename}"

    # Reusa el bucket privado "preguntas" que ya existe (mismo patron que banco_preguntas).
    sb.storage.from_("preguntas").upload(
        storage_path, contenido, {"content-type": archivo.content_type}
    )

    return {"media_url": storage_path, "media_tipo": tipo}

@router.post("/casos/{caso_id}/preguntas")
def crear_pregunta_caso(caso_id: str, p: PreguntaCasoIn, interrogador: dict = Depends(get_current_interrogador)):
    """Guarda la pregunta YA REVISADA (alternativas confirmadas por el interrogador)."""
    if not (1 <= p.orden <= 5):
        raise HTTPException(400, "El orden debe estar entre 1 y 5")
    if len(p.opciones) != 5:
        raise HTTPException(400, "Deben ser exactamente 5 alternativas")
    if not (0 <= p.correcta <= 4):
        raise HTTPException(400, "El indice de la respuesta correcta debe estar entre 0 y 4")

    existe_orden = sb.table("caso_preguntas").select("id").eq("caso_id", caso_id).eq("orden", p.orden).execute().data
    if existe_orden:
        raise HTTPException(409, f"Ya existe una pregunta en el orden {p.orden} para este caso")

    res = sb.table("caso_preguntas").insert({
        "caso_id": caso_id,
        "orden": p.orden,
        "pregunta": p.pregunta,
        "opciones": p.opciones,
        "correcta": p.correcta,
        "media_url": p.media_url,
        "media_tipo": p.media_tipo,
        "creado_por": interrogador["sub"],
    }).execute()
    return res.data[0]

@router.put("/casos/{caso_id}/preguntas/{caso_pregunta_id}")
def actualizar_pregunta_caso(caso_id: str, caso_pregunta_id: str, p: PreguntaCasoUpdateIn, interrogador: dict = Depends(get_current_interrogador)):
    """Editar una pregunta del caso ya guardada (texto, alternativas, correcta o media)."""
    if len(p.opciones) != 5:
        raise HTTPException(400, "Deben ser exactamente 5 alternativas")
    if not (0 <= p.correcta <= 4):
        raise HTTPException(400, "El indice de la respuesta correcta debe estar entre 0 y 4")

    res = sb.table("caso_preguntas").update({
        "pregunta": p.pregunta,
        "opciones": p.opciones,
        "correcta": p.correcta,
        "media_url": p.media_url,
        "media_tipo": p.media_tipo,
    }).eq("id", caso_pregunta_id).eq("caso_id", caso_id).execute()

    if not res.data:
        raise HTTPException(404, "Pregunta del caso no encontrada")

    cache_vivo.invalidar_pregunta(caso_id)
    return res.data[0]

@router.delete("/casos/{caso_id}/preguntas/{caso_pregunta_id}")
def quitar_pregunta_caso(caso_id: str, caso_pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("caso_preguntas").delete().eq("id", caso_pregunta_id).eq("caso_id", caso_id).execute()
    cache_vivo.invalidar_pregunta(caso_id)
    return {"ok": True}


# ---------------- FUNDAMENTO: borrador (Claude) -> revision -> guardado ----------------

@router.post("/casos/{caso_id}/preguntas/{caso_pregunta_id}/generar-fundamento")
def generar_fundamento_borrador(caso_id: str, caso_pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Genera un BORRADOR con Claude buscando en los materiales docx de la region. No guarda nada."""
    caso = sb.table("casos_clinicos").select("region").eq("id", caso_id).single().execute().data
    if not caso:
        raise HTTPException(404, "Caso no encontrado")

    cp = sb.table("caso_preguntas").select(
        "pregunta, opciones, correcta"
    ).eq("id", caso_pregunta_id).eq("caso_id", caso_id).single().execute().data
    if not cp:
        raise HTTPException(404, "Pregunta del caso no encontrada")
    if not cp["pregunta"]:
        raise HTTPException(409, "Esta pregunta todavia no tiene texto guardado")

    borrador = buscar_fundamento(
        region=caso["region"],
        pregunta=cp["pregunta"],
        opciones=cp["opciones"],
        correcta=cp["correcta"],
    )
    return borrador  # {"explicacion": ..., "fuentes": [...]} - sin guardar

@router.put("/casos/{caso_id}/preguntas/{caso_pregunta_id}/fundamento")
def guardar_fundamento_revisado(caso_id: str, caso_pregunta_id: str, body: FundamentoIn, interrogador: dict = Depends(get_current_interrogador)):
    """Guarda el fundamento YA REVISADO/EDITADO por el interrogador. Recien aqui queda persistido."""
    res = sb.table("caso_preguntas").update({
        "explicacion_generada": body.explicacion,
        "fuentes_generadas": body.fuentes or [],
    }).eq("id", caso_pregunta_id).eq("caso_id", caso_id).execute()

    if not res.data:
        raise HTTPException(404, "Pregunta del caso no encontrada")

    cache_vivo.invalidar_pregunta(caso_id)
    return res.data[0]


# ============================================================
# PRESENTACIONES (reemplazo reutilizable del PPT)
# ============================================================

@router.post("/presentaciones")
def crear_presentacion(p: PresentacionIn, interrogador: dict = Depends(get_current_interrogador)):
    row = p.model_dump()
    row["creado_por"] = interrogador["sub"]
    res = sb.table("presentaciones").insert(row).execute()
    return res.data[0]

@router.get("/presentaciones")
def listar_presentaciones(interrogador: dict = Depends(get_current_interrogador)):
    return sb.table("presentaciones").select("*").order("created_at", desc=True).execute().data

@router.get("/presentaciones/{presentacion_id}")
def obtener_presentacion(presentacion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Lectura completa de la presentacion para el profesor: todos los casos,
    ya ordenados, cada uno con sus preguntas."""
    pres = sb.table("presentaciones").select("*").eq("id", presentacion_id).single().execute().data
    if not pres:
        raise HTTPException(404, "Presentacion no encontrada")

    casos = sb.table("presentacion_casos").select(
        "id, orden, casos_clinicos(id, titulo, region, vineta_clinica, media_url, media_tipo)"
    ).eq("presentacion_id", presentacion_id).order("orden").execute().data

    for c in casos:
        caso_id = c["casos_clinicos"]["id"]
        preguntas = sb.table("caso_preguntas").select(
            "orden, pregunta, opciones"
        ).eq("caso_id", caso_id).order("orden").execute().data
        c["preguntas"] = preguntas

    pres["casos"] = casos
    return pres

@router.delete("/presentaciones/{presentacion_id}")
def borrar_presentacion(presentacion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Borra la presentacion completa. Primero borra sus sesiones en vivo
    asociadas (los votos se van solos por cascada) para no violar la
    foreign key; los casos clinicos NO se borran, solo se desvinculan."""
    sb.table("sesiones_vivo").delete().eq("presentacion_id", presentacion_id).execute()
    sb.table("presentaciones").delete().eq("id", presentacion_id).execute()
    return {"ok": True}

@router.post("/presentaciones/{presentacion_id}/casos")
def agregar_caso_presentacion(presentacion_id: str, pc: PresentacionCasoIn, interrogador: dict = Depends(get_current_interrogador)):
    existe_orden = sb.table("presentacion_casos").select("id").eq("presentacion_id", presentacion_id).eq("orden", pc.orden).execute().data
    if existe_orden:
        raise HTTPException(409, f"Ya existe un caso en el orden {pc.orden} de esta presentacion")

    res = sb.table("presentacion_casos").insert({
        "presentacion_id": presentacion_id, "caso_id": pc.caso_id, "orden": pc.orden
    }).execute()
    cache_vivo.invalidar_posiciones_presentacion(presentacion_id)
    return res.data[0]

@router.delete("/presentaciones/{presentacion_id}/casos/{presentacion_caso_id}")
def quitar_caso_presentacion(presentacion_id: str, presentacion_caso_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("presentacion_casos").delete().eq("id", presentacion_caso_id).eq("presentacion_id", presentacion_id).execute()
    cache_vivo.invalidar_posiciones_presentacion(presentacion_id)
    return {"ok": True}


# ============================================================
# CONTROL DE LA SESION EN VIVO
# ============================================================

def _generar_codigo(largo: int = 6) -> str:
    alfabeto = string.ascii_uppercase + string.digits
    return "".join(random.choices(alfabeto, k=largo))


@router.post("/vivo/iniciar")
def iniciar_sesion(body: IniciarSesionIn, interrogador: dict = Depends(get_current_interrogador)):
    codigo = _generar_codigo()
    while sb.table("sesiones_vivo").select("id").eq("codigo_acceso", codigo).execute().data:
        codigo = _generar_codigo()

    res = sb.table("sesiones_vivo").insert({
        "presentacion_id": body.presentacion_id,
        "codigo_acceso": codigo,
        "estado": "esperando",
        "caso_actual_orden": 1,
        "pregunta_actual_orden": 1,
        "creado_por": interrogador["sub"],
    }).execute()
    return res.data[0]

@router.get("/vivo")
def listar_sesiones_activas(interrogador: dict = Depends(get_current_interrogador)):
    """Sesiones en vivo que no han terminado (estado != 'cerrada'), con el
    titulo de su presentacion, para poder retomarlas o borrarlas."""
    sesiones = sb.table("sesiones_vivo").select(
        "id, codigo_acceso, estado, created_at, presentaciones(titulo)"
    ).neq("estado", "cerrada").order("created_at", desc=True).execute().data
    return sesiones

@router.delete("/vivo/{sesion_id}")
def borrar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Borra una sesion en vivo (sus votos se van solos por cascada)."""
    sb.table("sesiones_vivo").delete().eq("id", sesion_id).execute()
    cache_vivo.invalidar_sesion(sesion_id)
    return {"ok": True}

@router.get("/vivo/{sesion_id}")
def obtener_sesion_profesor(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Devuelve la sesion completa (incluyendo codigo_acceso) para el panel del profesor."""
    return obtener_sesion(sesion_id)

@router.get("/vivo/{sesion_id}/asistencia")
def ver_asistencia_vivo(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Panel del profesor: quien ha marcado asistencia (ingreso) a esta
    sesion, haya votado o no. El total de habilitados es el tamaño del
    conjunto de alumnos activo. Se sirve desde cache salvo que alguien
    acabe de ingresar (invalidada en casos_vivo_alumno.py)."""
    cacheada = cache_vivo.obtener_asistencia_cache(sesion_id)
    if cacheada is not None:
        return cacheada

    conjunto_id = obtener_conjunto_activo_id()

    presentes = sb.table("asistencia_vivo").select(
        "alumno_id, marcado_at, alumnos(nombre, rut)"
    ).eq("sesion_id", sesion_id).order("marcado_at").execute().data

    total = sb.table("alumnos").select("id", count="exact").eq("conjunto_id", conjunto_id).execute()

    resultado = {"presentes": presentes, "total_habilitados": total.count, "total_presentes": len(presentes)}
    cache_vivo.guardar_asistencia_cache(sesion_id, resultado)
    return resultado

@router.get("/vivo/{sesion_id}/detalle")
def detalle_votos(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Panel del profesor: nombre -> opcion, para elegir a quien pedir fundamento oral.
    Se lee del archivo local (Render Disk) mientras la pregunta sigue
    activa -los votos todavia no se han volcado a Supabase-, y se
    completan los nombres con una consulta puntual a la tabla alumnos."""
    sesion = obtener_sesion(sesion_id)
    pregunta = pregunta_actual(sesion)
    if not pregunta:
        return []

    votos_locales = votos_local.obtener_alumnos_que_votaron(sesion_id, pregunta["id"])
    if not votos_locales:
        return []

    alumno_ids = [v["alumno_id"] for v in votos_locales]
    alumnos_info = sb.table("alumnos").select("id, nombre, rut").in_("id", alumno_ids).execute().data
    alumnos_por_id = {a["id"]: a for a in alumnos_info}

    return [
        {
            "opcion": v["opcion"],
            "created_at": v["votado_at"],
            "alumnos": alumnos_por_id.get(v["alumno_id"]),
        }
        for v in votos_locales
    ]

@router.post("/vivo/{sesion_id}/accion")
def avanzar_sesion(sesion_id: str, body: AccionIn, interrogador: dict = Depends(get_current_interrogador)):
    """El profesor controla el ciclo: abrir_votacion -> cerrar_votacion (discusion) -> revelar -> siguiente.
    'revelar' NUNCA llama a Claude: solo cambia el estado para exponer lo ya guardado de antemano.
    'cerrar_votacion' vuelca todos los votos acumulados en el archivo local a Supabase de una sola vez.
    Cada cambio de estado se actualiza tambien en la cache en memoria, para
    que el proximo GET no tenga que ir a buscarlo de nuevo a Supabase."""
    sesion = obtener_sesion(sesion_id)

    if body.accion == "abrir_votacion":
        cambios = {"estado": "votando"}
        sb.table("sesiones_vivo").update(cambios).eq("id", sesion_id).execute()
        cache_vivo.actualizar_sesion_cache(sesion_id, cambios)

    elif body.accion == "cerrar_votacion":
        lote_votos = votos_local.volcar_y_limpiar(sesion_id)
        if lote_votos:
            sb.table("votos_vivo").insert(lote_votos).execute()

        cambios = {"estado": "discusion"}
        sb.table("sesiones_vivo").update(cambios).eq("id", sesion_id).execute()
        cache_vivo.actualizar_sesion_cache(sesion_id, cambios)

    elif body.accion == "revelar":
        pregunta = pregunta_actual(sesion)
        if not pregunta:
            raise HTTPException(409, "No hay pregunta activa para revelar")

        cambios = {"estado": "cerrada"}
        sb.table("sesiones_vivo").update(cambios).eq("id", sesion_id).execute()
        cache_vivo.actualizar_sesion_cache(sesion_id, cambios)

    elif body.accion == "siguiente":
        caso = caso_actual(sesion)
        if not caso:
            raise HTTPException(409, "No hay caso activo")

        total_preguntas = total_preguntas_caso(caso["id"])
        siguiente_pregunta = sesion["pregunta_actual_orden"] + 1

        if siguiente_pregunta <= total_preguntas:
            cambios = {"pregunta_actual_orden": siguiente_pregunta, "estado": "esperando"}
            sb.table("sesiones_vivo").update(cambios).eq("id", sesion_id).execute()
            cache_vivo.actualizar_sesion_cache(sesion_id, cambios)
        else:
            total_casos = total_casos_presentacion(sesion["presentacion_id"])
            siguiente_caso = sesion["caso_actual_orden"] + 1
            if siguiente_caso <= total_casos:
                cambios = {"caso_actual_orden": siguiente_caso, "pregunta_actual_orden": 1, "estado": "esperando"}
                sb.table("sesiones_vivo").update(cambios).eq("id", sesion_id).execute()
                cache_vivo.actualizar_sesion_cache(sesion_id, cambios)
            else:
                cambios = {"estado": "cerrada"}
                sb.table("sesiones_vivo").update(cambios).eq("id", sesion_id).execute()
                cache_vivo.actualizar_sesion_cache(sesion_id, cambios)
                return {"ok": True, "finalizada": True}
    else:
        raise HTTPException(400, "Accion invalida")

    return obtener_sesion(sesion_id)
