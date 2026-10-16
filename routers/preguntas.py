"""
routers/preguntas.py
Banco de preguntas: opción múltiple simple, 5 alternativas (A-E), una sola correcta.
El interrogador ingresa la pregunta + la respuesta correcta; las otras 4
alternativas las propone la IA vía /preguntas/generar-alternativas, y el
interrogador las revisa/edita antes de guardar con POST /preguntas.
Opcionalmente puede llevar una foto o video (ej. radiografías), subido
al bucket "preguntas" (separado de "materiales").

Además: edición de preguntas ya guardadas (PUT), filtros "por revisar" y por
carga, y el registro de cargas desde archivo (ver scripts/importar_docx.py):
GET /preguntas/cargas lista los lotes y POST /preguntas/cargas/{id}/desactivar
da de baja un lote completo (solo admin).
"""

import random
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, File
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador, requiere_admin
from services.claude_client import generar_alternativas

router = APIRouter(prefix="/preguntas", tags=["preguntas"])

COMPLEJIDADES_VALIDAS = ("basica", "intermedia", "compleja")
REGIONES_VALIDAS = (
    "hombro", "codo", "mano_muneca", "columna", "cadera_pelvis", "rodilla",
    "tobillo_pie", "ortogeriatria", "imagenologia", "ciencias_basicas", "administrativo",
)


# ---------------- MODELOS ----------------
class GenerarAlternativasIn(BaseModel):
    region: str
    complejidad: str  # basica | intermedia | compleja
    pregunta: str
    respuesta_correcta: str

class PreguntaIn(BaseModel):
    region: str
    complejidad: str  # basica | intermedia | compleja
    pregunta: str
    opciones: List[str]  # exactamente 5 (A-E)
    correcta: int         # índice 0-4
    explicacion: Optional[str] = None
    media_url: Optional[str] = None
    media_tipo: Optional[str] = None  # "foto" | "video"

class PreguntaUpdateIn(BaseModel):
    """Edición parcial: solo se modifican los campos que vienen en el cuerpo.
    Para quitar la foto/video se envía media_url y media_tipo en null."""
    region: Optional[str] = None
    complejidad: Optional[str] = None
    pregunta: Optional[str] = None
    opciones: Optional[List[str]] = None
    correcta: Optional[int] = None
    explicacion: Optional[str] = None
    media_url: Optional[str] = None
    media_tipo: Optional[str] = None
    revisar: Optional[bool] = None
    nota_revision: Optional[str] = None


# ---------------- GENERAR ALTERNATIVAS CON IA (no guarda nada) ----------------
@router.post("/generar-alternativas")
def generar_alternativas_endpoint(body: GenerarAlternativasIn, interrogador: dict = Depends(get_current_interrogador)):
    incorrectas = generar_alternativas(
        pregunta=body.pregunta,
        respuesta_correcta=body.respuesta_correcta,
        region=body.region,
        complejidad=body.complejidad,
    )

    opciones = incorrectas + [body.respuesta_correcta]
    random.shuffle(opciones)
    correcta_idx = opciones.index(body.respuesta_correcta)

    return {"opciones": opciones, "correcta": correcta_idx}


# ---------------- SUBIR FOTO/VIDEO DE LA PREGUNTA (bucket "preguntas") ----------------
@router.post("/media")
async def subir_media(
    tipo: str = Form(...),  # "foto" | "video"
    archivo: UploadFile = File(...),
    interrogador: dict = Depends(get_current_interrogador),
):
    if tipo not in ("foto", "video"):
        raise HTTPException(400, "Tipo inválido, debe ser 'foto' o 'video'")

    contenido = await archivo.read()
    storage_path = f"{int(time.time())}-{archivo.filename}"

    sb.storage.from_("preguntas").upload(
        storage_path, contenido, {"content-type": archivo.content_type}
    )

    # Bucket privado: no hay URL pública. Se guarda la ruta interna;
    # la URL firmada (temporal) se genera recién al rendir el examen.
    return {"media_url": storage_path, "media_tipo": tipo}


# ---------------- CRUD DEL BANCO (cualquier interrogador logueado) ----------------
@router.post("")
def crear_pregunta(p: PreguntaIn, interrogador: dict = Depends(get_current_interrogador)):
    if len(p.opciones) != 5:
        raise HTTPException(400, "Deben ser exactamente 5 alternativas (A-E)")
    if not (0 <= p.correcta <= 4):
        raise HTTPException(400, "El índice de la respuesta correcta debe estar entre 0 y 4")

    row = p.model_dump()
    row["creado_por"] = interrogador["sub"]
    res = sb.table("banco_preguntas").insert(row).execute()
    return res.data[0]

@router.post("/bulk")
def crear_preguntas_bulk(preguntas: List[PreguntaIn], interrogador: dict = Depends(get_current_interrogador)):
    for p in preguntas:
        if len(p.opciones) != 5:
            raise HTTPException(400, f"Pregunta '{p.pregunta[:40]}...' no tiene 5 alternativas")
    rows = [{**p.model_dump(), "creado_por": interrogador["sub"]} for p in preguntas]
    res = sb.table("banco_preguntas").insert(rows).execute()
    return {"insertadas": len(res.data)}

@router.get("")
def listar_preguntas(
    region: Optional[str] = None,
    revisar: Optional[bool] = None,
    carga_id: Optional[str] = None,
    interrogador: dict = Depends(get_current_interrogador),
):
    q = sb.table("banco_preguntas").select("*, interrogadores(nombre)").eq("activo", True)
    if region:
        q = q.eq("region", region)
    if revisar is not None:
        q = q.eq("revisar", revisar)
    if carga_id:
        q = q.eq("carga_id", carga_id)
    rows = q.execute().data
    for r in rows:
        r["creado_por_nombre"] = (r.get("interrogadores") or {}).get("nombre")
    # Orden estable: primero las de una carga en su orden original, luego el resto por fecha.
    rows.sort(key=lambda r: (r.get("created_at") or "", r.get("numero_origen") or 0))
    return rows

# ---------------- REGISTRO DE CARGAS (archivos subidos al banco) ----------------
@router.get("/cargas")
def listar_cargas(interrogador: dict = Depends(get_current_interrogador)):
    cargas = sb.table("cargas_preguntas").select("*, interrogadores(nombre)").order("created_at", desc=True).execute().data
    for c in cargas:
        c["subido_por_nombre"] = (c.get("interrogadores") or {}).get("nombre")
        c.pop("interrogadores", None)
        vigentes = sb.table("banco_preguntas").select("id", count="exact").eq("carga_id", c["id"]).eq("activo", True).execute()
        c["preguntas_activas"] = vigentes.count or 0
    return cargas

@router.post("/cargas/{carga_id}/desactivar")
def desactivar_carga(carga_id: str, admin: dict = Depends(requiere_admin)):
    """Da de baja TODAS las preguntas de un lote (no borra nada: quedan inactivas)."""
    existe = sb.table("cargas_preguntas").select("id").eq("id", carga_id).execute().data
    if not existe:
        raise HTTPException(404, "Carga no encontrada")
    res = sb.table("banco_preguntas").update({"activo": False}).eq("carga_id", carga_id).eq("activo", True).execute()
    return {"ok": True, "desactivadas": len(res.data)}

# ---------------- EDITAR UNA PREGUNTA YA GUARDADA ----------------
@router.put("/{pregunta_id}")
def actualizar_pregunta(pregunta_id: str, cambios: PreguntaUpdateIn, interrogador: dict = Depends(get_current_interrogador)):
    datos = cambios.model_dump(exclude_unset=True)
    if not datos:
        raise HTTPException(400, "No hay cambios para guardar")

    if "region" in datos and datos["region"] not in REGIONES_VALIDAS:
        raise HTTPException(400, "Región inválida")
    if "complejidad" in datos and datos["complejidad"] not in COMPLEJIDADES_VALIDAS:
        raise HTTPException(400, "Complejidad inválida")
    if "pregunta" in datos and not (datos["pregunta"] or "").strip():
        raise HTTPException(400, "El enunciado no puede quedar vacío")
    if "opciones" in datos:
        if datos["opciones"] is None or len(datos["opciones"]) != 5 or any(not (o or "").strip() for o in datos["opciones"]):
            raise HTTPException(400, "Deben ser exactamente 5 alternativas (A-E), ninguna vacía")
    if "correcta" in datos and (datos["correcta"] is None or not (0 <= datos["correcta"] <= 4)):
        raise HTTPException(400, "El índice de la respuesta correcta debe estar entre 0 y 4")
    if "media_tipo" in datos and datos["media_tipo"] not in (None, "foto", "video"):
        raise HTTPException(400, "Tipo de media inválido, debe ser 'foto' o 'video'")

    datos["updated_at"] = datetime.now(timezone.utc).isoformat()
    res = sb.table("banco_preguntas").update(datos).eq("id", pregunta_id).execute()
    if not res.data:
        raise HTTPException(404, "Pregunta no encontrada")
    return res.data[0]

@router.get("/{pregunta_id}/media")
def obtener_media(pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """URL firmada temporal para previsualizar la foto/video de una pregunta (bucket privado)."""
    p = sb.table("banco_preguntas").select("media_url").eq("id", pregunta_id).single().execute().data
    if not p or not p["media_url"]:
        raise HTTPException(404, "Esta pregunta no tiene foto/video")

    firmada = sb.storage.from_("preguntas").create_signed_url(p["media_url"], 300)
    url = firmada.get("signedURL") or firmada.get("signed_url")
    return {"url": url}

@router.delete("/{pregunta_id}")
def borrar_pregunta(pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("banco_preguntas").update({"activo": False}).eq("id", pregunta_id).execute()
    return {"ok": True}
