"""
routers/documentos.py
Dos funciones, ambas server-to-server hacia EvidenciaMed (el navegador del
interrogador nunca ve ninguna clave de EvidenciaMed):

1. POST /documentos/buscar   → proxy hacia /search de EvidenciaMed
2. POST /documentos/generar  → proxy hacia /generate/document de EvidenciaMed

Variables de entorno esperadas (Render, backend Músculo):
  EVIDENCIAMED_URL       (ej: https://evidenciamed-api.onrender.com)
  EVIDENCIAMED_API_KEY   (la misma que usa el propio frontend de EvidenciaMed,
                          protege /search y /analyze/*)
  DOCUMENT_KEY           (clave exclusiva de /generate/document, distinta de
                          la anterior)
"""

import os

import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador

EVIDENCIAMED_URL = os.environ["EVIDENCIAMED_URL"]
EVIDENCIAMED_API_KEY = os.environ["EVIDENCIAMED_API_KEY"]
DOCUMENT_KEY = os.environ["DOCUMENT_KEY"]

router = APIRouter(prefix="/documentos", tags=["documentos"])


class BuscarIn(BaseModel):
    tema: str
    max_results: int = 20


class GenerarDocumentoIn(BaseModel):
    papers: list[dict]
    tema: str


def _obtener_nombre_interrogador(interrogador_id: str) -> str:
    """Consulta el nombre real del interrogador en Supabase usando el 'sub' del JWT."""
    res = sb.table("interrogadores").select("nombre").eq("id", interrogador_id).execute()
    if not res.data:
        return "No especificado"
    return res.data[0]["nombre"]


@router.post("/buscar")
async def buscar_papers(
    body: BuscarIn,
    interrogador: dict = Depends(get_current_interrogador),
):
    """Cualquier interrogador autenticado puede buscar. Proxy server-to-server
    hacia /search de EvidenciaMed — el navegador nunca ve EVIDENCIAMED_API_KEY."""
    if len(body.tema.strip()) < 3:
        raise HTTPException(422, "Tema demasiado corto.")

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{EVIDENCIAMED_URL}/search",
                headers={"X-API-Key": EVIDENCIAMED_API_KEY},
                json={"query": body.tema.strip(), "max_results": min(body.max_results, 20)},
            )
    except httpx.RequestError as e:
        raise HTTPException(502, f"No se pudo contactar a EvidenciaMed: {e}")

    if r.status_code != 200:
        raise HTTPException(r.status_code, f"EvidenciaMed respondió con error: {r.text}")

    return r.json()


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
        async with httpx.AsyncClient(timeout=300) as c:
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
  
