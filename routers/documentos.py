"""
routers/documentos.py
Tres funciones:

1. POST /documentos/buscar   → proxy server-to-server hacia /search de EvidenciaMed
2. POST /documentos/generar  → proxy server-to-server hacia /generate/document de EvidenciaMed
3. POST /documentos/pdf      → renderiza el texto YA EDITADO por el interrogador
                                a PDF. No llama a EvidenciaMed ni a Claude —
                                es solo formateo, con lo que el interrogador
                                ya corrigió/agregó en pantalla.

Variables de entorno esperadas (Render, backend Músculo):
  EVIDENCIAMED_URL       (ej: https://evidenciamed-api.onrender.com)
  EVIDENCIAMED_API_KEY   (la misma que usa el propio frontend de EvidenciaMed,
                          protege /search y /analyze/*)
  DOCUMENT_KEY           (clave exclusiva de /generate/document, distinta de
                          la anterior)
"""

import os
from io import BytesIO
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Depends, Response
from pydantic import BaseModel
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable

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


class EpidemiologiaIn(BaseModel):
    internacional: str = ""
    nacional: str = ""


class DocumentoEditadoIn(BaseModel):
    titulo: str = "Documento sin título"
    autor: str = "No especificado"
    introduccion: str = ""
    epidemiologia: Optional[EpidemiologiaIn] = EpidemiologiaIn()
    clinica: str = ""
    diagnostico: str = ""
    diagnostico_diferencial: str = ""
    examenes_complementarios: str = ""
    tratamientos: str = ""
    resultados: str = ""
    conclusiones: str = ""
    referencias: list[str] = []


def _obtener_nombre_interrogador(interrogador_id: str) -> str:
    """Consulta el nombre real del interrogador en Supabase usando el 'sub' del JWT."""
    res = sb.table("interrogadores").select("nombre").eq("id", interrogador_id).execute()
    if not res.data:
        return "No especificado"
    return res.data[0]["nombre"]


SECCIONES_PDF = [
    ("introduccion", "Introducción"),
    ("clinica", "Clínica"),
    ("diagnostico", "Diagnóstico"),
    ("diagnostico_diferencial", "Diagnóstico diferencial"),
    ("examenes_complementarios", "Exámenes complementarios"),
    ("tratamientos", "Tratamientos"),
    ("resultados", "Resultados"),
    ("conclusiones", "Conclusiones"),
]


def _construir_pdf(doc: DocumentoEditadoIn) -> bytes:
    buffer = BytesIO()
    pdf = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        title=doc.titulo, author=doc.autor,
    )

    styles = getSampleStyleSheet()
    navy = colors.HexColor("#1F4E78")
    gray = colors.HexColor("#595959")

    title_style = ParagraphStyle("TituloDoc", parent=styles["Title"], fontSize=20, textColor=navy, alignment=TA_CENTER, spaceAfter=6)
    autor_style = ParagraphStyle("AutorDoc", parent=styles["Normal"], fontSize=11, textColor=gray, alignment=TA_CENTER, spaceAfter=20, fontName="Helvetica-Oblique")
    h2 = ParagraphStyle("H2Doc", parent=styles["Heading2"], fontSize=13, textColor=navy, spaceBefore=16, spaceAfter=8)
    body = ParagraphStyle("BodyDoc", parent=styles["Normal"], fontSize=10.5, leading=15, alignment=TA_JUSTIFY, spaceAfter=8)
    ref_style = ParagraphStyle("RefDoc", parent=styles["Normal"], fontSize=9, textColor=gray, spaceAfter=4)

    story = [
        Paragraph(doc.titulo, title_style),
        Paragraph(f"Autor: {doc.autor}", autor_style),
        HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#BFBFBF")),
    ]

    def agregar_seccion(titulo, texto):
        story.append(Paragraph(titulo, h2))
        for parrafo in (texto or "—").split("\n"):
            if parrafo.strip():
                story.append(Paragraph(parrafo, body))

    agregar_seccion("Introducción", doc.introduccion)

    epi = doc.epidemiologia or EpidemiologiaIn()
    story.append(Paragraph("Epidemiología internacional y nacional", h2))
    story.append(Paragraph(f"<b>Internacional:</b> {epi.internacional or '—'}", body))
    story.append(Paragraph(f"<b>Nacional:</b> {epi.nacional or '—'}", body))

    for key, label in SECCIONES_PDF[1:]:
        agregar_seccion(label, getattr(doc, key))

    if doc.referencias:
        story.append(Paragraph("Referencias", h2))
        for ref in doc.referencias:
            story.append(Paragraph(ref, ref_style))

    story.append(Spacer(1, 0.3 * inch))

    pdf.build(story)
    buffer.seek(0)
    return buffer.read()


@router.get("/ping")
async def ping_evidenciamed():
    """Dispara una petición liviana a EvidenciaMed para despertar el servicio
    (Render free tier se duerme por inactividad). Se llama al entrar a la
    página de generar documentos, antes de que el interrogador termine de
    escribir el tema — así el cold start (~50s) ya pasó cuando busque."""
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(f"{EVIDENCIAMED_URL}/health")
        return {"awake": r.status_code == 200}
    except httpx.RequestError:
        return {"awake": False}


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
        async with httpx.AsyncClient(timeout=60) as c:
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


@router.post("/pdf")
async def documento_a_pdf(
    body: DocumentoEditadoIn,
    interrogador: dict = Depends(get_current_interrogador),
):
    """Recibe el texto YA EDITADO por el interrogador (título, secciones,
    referencias) y devuelve el PDF final. No llama a EvidenciaMed ni a
    Claude — es solo formateo del contenido que el interrogador ya corrigió
    en pantalla."""
    pdf_bytes = _construir_pdf(body)
    nombre = "".join(c for c in body.titulo if c.isalnum() or c in " -_")[:60].strip() or "documento"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre}.pdf"'},
    )
  
