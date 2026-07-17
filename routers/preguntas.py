"""
routers/preguntas.py
Banco de preguntas: opción múltiple simple, 5 alternativas (A-E), una sola correcta.
El interrogador ingresa la pregunta + la respuesta correcta; las otras 4
alternativas las propone la IA vía /preguntas/generar-alternativas, y el
interrogador las revisa/edita antes de guardar con POST /preguntas.
Opcionalmente puede llevar una foto o video (ej. radiografías), subido
al bucket "preguntas" (separado de "materiales").
"""

import random
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, File
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador
from services.claude_client import generar_alternativas

router = APIRouter(prefix="/preguntas", tags=["preguntas"])


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
    url = sb.storage.from_("preguntas").get_public_url(storage_path)

    return {"media_url": url, "media_tipo": tipo}


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
def listar_preguntas(region: Optional[str] = None, interrogador: dict = Depends(get_current_interrogador)):
    q = sb.table("banco_preguntas").select("*").eq("activo", True)
    if region:
        q = q.eq("region", region)
    return q.execute().data

@router.delete("/{pregunta_id}")
def borrar_pregunta(pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("banco_preguntas").update({"activo": False}).eq("id", pregunta_id).execute()
    return {"ok": True}
    
