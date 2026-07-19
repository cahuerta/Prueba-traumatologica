"""
routers/documentos.py
Genera documentos de revisión clínica llamando al backend de EvidenciaMed
(POST /generate/document), a partir de los papers que el interrogador
seleccionó y ya tiene analizados en EvidenciaMed.

La comunicación con EvidenciaMed es server-to-server: el frontend nunca ve
la DOCUMENT_KEY, solo necesita el token de sesión normal del interrogador.

Variables de entorno esperadas (Render):
  EVIDENCIAMED_URL    (ej: https://evidenciamed-api.onrender.com)
  DOCUMENT_KEY        (debe coincidir con la DOCUMENT_KEY configurada en EvidenciaMed)
"""

import os

import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador

EVIDENCIAMED_URL = os.environ["EVIDENCIAMED_URL"]
DOCUMENT_KEY = os.environ["DOCUMENT_KEY"]

router = APIRouter(prefix="/documentos", tags=["documentos"])


class GenerarDocumentoIn(BaseModel):
    papers: list[dict]
    tema: str


def _obtener_nombre_interrogador(interrogador_id: str) -> str:
    """Consulta el nombre real del interrogador en Supabase usando el 'sub' del JWT."""
    res = sb.table("interrogadores").select("nombre").eq("id", interrogador_id).execute()
    if not res.data:
        return "No especificado"
    return res.data[0]["nombre"]


@router.post("/generar")
async def generar_documento(
    body: GenerarDocumentoIn,
    interrogador: dict = Depends(get_current_interrogador),
):
    """Cualquier interrogador autenticado puede generar un documento
    (no está restringido a requiere_admin)."""
    if not body.papers:
        raise HTTPException(422, "Se requiere al menos un paper seleccionado.")
    if len(body.tema.strip()) < 3:
        raise HTTPException(422, "Tema demasiado corto.")

    autor = _obtener_nombre_interrogador(interrogador["sub"])

    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(
                f"{EVIDENCIAMED_URL}/generate/document",
                headers={"X-Document-Key": DOCUMENT_KEY},
                json={"papers": body.papers, "tema": body.tema.strip(), "autor": autor},
            )
    except httpx.RequestError as e:
        raise HTTPException(502, f"No se pudo contactar a EvidenciaMed: {e}")

    if r.status_code != 200:
        raise HTTPException(r.status_code, f"EvidenciaMed respondió con error: {r.text}")

    return r.json()
  
