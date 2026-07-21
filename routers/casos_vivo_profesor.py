"""
routers/casos_vivo_profesor.py
Endpoints PROTEGIDOS (requieren login de interrogador) de la presentacion
dinamica en vivo. Cubre 2 momentos:

1) PREPARACION (antes de la clase): crear casos clinicos con su media,
   agregar preguntas del banco en orden fijo (1-5), generar un BORRADOR
   de fundamento con Claude buscando en los materiales docx de la region
   (services/fundamento_vivo.py), revisar/editar ese borrador y recien
   ahi guardarlo. Armar presentaciones (varios casos, ordenados,
   reutilizables - reemplazo del PPT).

2) CONTROL EN VIVO (durante la clase): iniciar sesion desde una
   presentacion, y avanzar el ciclo abrir_votacion -> cerrar_votacion
   (discusion/fundamentos orales) -> revelar -> siguiente. 'revelar'
   NUNCA llama a Claude: solo expone lo que ya quedo guardado y
   revisado de antemano. Tambien expone el detalle nombre->opcion para
   que el profesor elija a quien pedir que fundamente en voz alta.

Complementa a casos_vivo_alumno.py (endpoints publicos del alumno).
"""

import random
import string
import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, UploadFile, File, HTTPException

from routers.auth import sb, get_current_interrogador
from services.fundamento_vivo import buscar_fundamento
from routers.casos_vivo_comun import (
    CasoIn,
    PreguntaCasoIn,
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
    caso = sb.table("casos_clinicos").select("*").eq("id", caso_id).single().execute().data
    if not caso:
        raise HTTPException(404, "Caso no encontrado")

    puente = sb.table("caso_preguntas").select(
        "id, orden, pregunta_id, explicacion_generada, fuentes_generadas, "
        "banco_preguntas(id, pregunta, opciones, correcta, explicacion, media_url, media_tipo, complejidad)"
    ).eq("caso_id", caso_id).order("orden").execute().data

    caso["preguntas"] = puente
    return caso

@router.post("/casos/{caso_id}/preguntas")
def agregar_pregunta_caso(caso_id: str, p: PreguntaCasoIn, interrogador: dict = Depends(get_current_interrogador)):
    if not (1 <= p.orden <= 5):
        raise HTTPException(400, "El orden debe estar entre 1 y 5")

    existe_orden = sb.table("caso_preguntas").select("id").eq("caso_id", caso_id).eq("orden", p.orden).execute().data
    if existe_orden:
        raise HTTPException(409, f"Ya existe una pregunta en el orden {p.orden} para este caso")

    res = sb.table("caso_preguntas").insert({
        "caso_id": caso_id, "pregunta_id": p.pregunta_id, "orden": p.orden
    }).execute()
    return res.data[0]

@router.delete("/casos/{caso_id}/preguntas/{caso_pregunta_id}")
def quitar_pregunta_caso(caso_id: str, caso_pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("caso_preguntas").delete().eq("id", caso_pregunta_id).eq("caso_id", caso_id).execute()
    return {"ok": True}


# ---------------- FUNDAMENTO: borrador (Claude) -> revision -> guardado ----------------

@router.post("/casos/{caso_id}/preguntas/{caso_pregunta_id}/generar-fundamento")
def generar_fundamento_borrador(caso_id: str, caso_pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Genera un BORRADOR con Claude buscando en los materiales docx de la region. No guarda nada."""
    caso = sb.table("casos_clinicos").select("region").eq("id", caso_id).single().execute().data
    if not caso:
        raise HTTPException(404, "Caso no encontrado")

    cp = sb.table("caso_preguntas").select(
        "id, banco_preguntas(pregunta, opciones, correcta)"
    ).eq("id", caso_pregunta_id).eq("caso_id", caso_id).single().execute().data
    if not cp:
        raise HTTPException(404, "Pregunta del caso no encontrada")

    bp = cp["banco_preguntas"]
    borrador = buscar_fundamento(
        region=caso["region"],
        pregunta=bp["pregunta"],
        opciones=bp["opciones"],
        correcta=bp["correcta"],
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
    pres = sb.table("presentaciones").select("*").eq("id", presentacion_id).single().execute().data
    if not pres:
        raise HTTPException(404, "Presentacion no encontrada")

    casos = sb.table("presentacion_casos").select(
        "id, orden, casos_clinicos(id, titulo, region, vineta_clinica, media_url, media_tipo)"
    ).eq("presentacion_id", presentacion_id).order("orden").execute().data

    for c in casos:
        caso_id = c["casos_clinicos"]["id"]
        preguntas = sb.table("caso_preguntas").select(
            "orden, pregunta_id, explicacion_generada, banco_preguntas(pregunta, opciones)"
        ).eq("caso_id", caso_id).order("orden").execute().data
        c["preguntas"] = preguntas

    pres["casos"] = casos
    return pres

@router.post("/presentaciones/{presentacion_id}/casos")
def agregar_caso_presentacion(presentacion_id: str, pc: PresentacionCasoIn, interrogador: dict = Depends(get_current_interrogador)):
    existe_orden = sb.table("presentacion_casos").select("id").eq("presentacion_id", presentacion_id).eq("orden", pc.orden).execute().data
    if existe_orden:
        raise HTTPException(409, f"Ya existe un caso en el orden {pc.orden} de esta presentacion")

    res = sb.table("presentacion_casos").insert({
        "presentacion_id": presentacion_id, "caso_id": pc.caso_id, "orden": pc.orden
    }).execute()
    return res.data[0]

@router.delete("/presentaciones/{presentacion_id}/casos/{presentacion_caso_id}")
def quitar_caso_presentacion(presentacion_id: str, presentacion_caso_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("presentacion_casos").delete().eq("id", presentacion_caso_id).eq("presentacion_id", presentacion_id).execute()
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

@router.get("/vivo/{sesion_id}/detalle")
def detalle_votos(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Panel del profesor: nombre -> opcion, para elegir a quien pedir fundamento oral."""
    sesion = obtener_sesion(sesion_id)
    pregunta = pregunta_actual(sesion)
    if not pregunta:
        return []

    pregunta_id = pregunta["banco_preguntas"]["id"]
    votos = sb.table("votos_vivo").select(
        "opcion, created_at, alumnos(id, nombre, rut)"
    ).eq("sesion_id", sesion_id).eq("pregunta_id", pregunta_id).order("created_at").execute().data

    return votos

@router.post("/vivo/{sesion_id}/accion")
def avanzar_sesion(sesion_id: str, body: AccionIn, interrogador: dict = Depends(get_current_interrogador)):
    """El profesor controla el ciclo: abrir_votacion -> cerrar_votacion (discusion) -> revelar -> siguiente.
    'revelar' NUNCA llama a Claude: solo cambia el estado para exponer lo ya guardado de antemano."""
    sesion = obtener_sesion(sesion_id)

    if body.accion == "abrir_votacion":
        sb.table("sesiones_vivo").update({"estado": "votando"}).eq("id", sesion_id).execute()

    elif body.accion == "cerrar_votacion":
        sb.table("sesiones_vivo").update({"estado": "discusion"}).eq("id", sesion_id).execute()

    elif body.accion == "revelar":
        pregunta = pregunta_actual(sesion)
        if not pregunta:
            raise HTTPException(409, "No hay pregunta activa para revelar")
        sb.table("sesiones_vivo").update({"estado": "cerrada"}).eq("id", sesion_id).execute()

    elif body.accion == "siguiente":
        caso = caso_actual(sesion)
        if not caso:
            raise HTTPException(409, "No hay caso activo")

        total_preguntas = total_preguntas_caso(caso["id"])
        siguiente_pregunta = sesion["pregunta_actual_orden"] + 1

        if siguiente_pregunta <= total_preguntas:
            sb.table("sesiones_vivo").update({
                "pregunta_actual_orden": siguiente_pregunta,
                "estado": "esperando",
            }).eq("id", sesion_id).execute()
        else:
            total_casos = total_casos_presentacion(sesion["presentacion_id"])
            siguiente_caso = sesion["caso_actual_orden"] + 1
            if siguiente_caso <= total_casos:
                sb.table("sesiones_vivo").update({
                    "caso_actual_orden": siguiente_caso,
                    "pregunta_actual_orden": 1,
                    "estado": "esperando",
                }).eq("id", sesion_id).execute()
            else:
                sb.table("sesiones_vivo").update({"estado": "cerrada"}).eq("id", sesion_id).execute()
                return {"ok": True, "finalizada": True}
    else:
        raise HTTPException(400, "Accion invalida")

    return obtener_sesion(sesion_id)
  
