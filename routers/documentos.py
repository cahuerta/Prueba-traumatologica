"""
routers/documentos.py
Cuatro funciones:

1. POST /documentos/buscar        → proxy server-to-server hacia /search de EvidenciaMed
2. POST /documentos/generar       → proxy server-to-server hacia /generate/document de EvidenciaMed
3. POST /documentos/pdf           → renderiza el texto YA EDITADO por el interrogador
                                     a PDF. No llama a EvidenciaMed ni a Claude —
                                     es solo formateo, con lo que el interrogador
                                     ya corrigió/agregó en pantalla.
4. POST /documentos/ppt           → mismo texto editado, resumido con Claude (regla 6x6)
                                     y exportado como .pptx descargable.
5. POST /documentos/clase-formal  → mismo texto editado, mismo resumen 6x6, pero en vez
                                     de un .pptx descargable, crea el contenido directo
                                     en Clases Formales (clases_formales + paginas_clase),
                                     listo para abrir en el constructor y agregarle
                                     imagenes/trivia/semaforo.

Variables de entorno esperadas (Render, backend Músculo):
  EVIDENCIAMED_URL       (ej: https://evidenciamed-api.onrender.com)
  EVIDENCIAMED_API_KEY   (la misma que usa el propio frontend de EvidenciaMed,
                          protege /search y /analyze/*)
  DOCUMENT_KEY           (clave exclusiva de /generate/document, distinta de
                          la anterior)
"""

import os
import json
import asyncio
from io import BytesIO
from typing import Optional

import httpx
import anthropic
from fastapi import APIRouter, HTTPException, Depends, Response
from pydantic import BaseModel
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

from routers.auth import sb, get_current_interrogador

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

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


NAVY_RGB = RGBColor(0x1F, 0x4E, 0x78)
GRAY_RGB = RGBColor(0x59, 0x59, 0x59)

SECCIONES_PPT = [
    ("introduccion", "Introducción"),
    ("clinica", "Clínica"),
    ("diagnostico", "Diagnóstico"),
    ("diagnostico_diferencial", "Diagnóstico diferencial"),
    ("examenes_complementarios", "Exámenes complementarios"),
    ("tratamientos", "Tratamientos"),
    ("resultados", "Resultados"),
    ("conclusiones", "Conclusiones"),
]


async def _resumir_seccion(titulo: str, texto: str) -> list[list[str]]:
    """Condensa el texto de una sección en bullets aplicando la regla 6x6
    (máx. 6 bullets por diapositiva, máx. 6 palabras por bullet). Si el
    contenido no cabe en 6 bullets, devuelve varias diapositivas."""
    texto = (texto or "").strip()
    if not texto or texto == "—":
        return [["Sin información disponible"]]

    prompt = f"""Resume el siguiente texto médico en bullets para una diapositiva de clase de docencia, para mantener la atención de los alumnos.

REGLAS ESTRICTAS (regla 6x6):
- Máximo 6 bullets por diapositiva.
- Cada bullet debe tener máximo 6 palabras — palabras clave, no oraciones completas.
- Si el contenido no cabe en 6 bullets, divide en varias diapositivas, cada una con máximo 6 bullets de máximo 6 palabras.
- No inventes información que no esté en el texto entregado.

Sección: {titulo}
Texto:
{texto}

Devuelve EXCLUSIVAMENTE un JSON válido, sin texto adicional ni markdown:
{{"slides": [{{"bullets": ["...", "..."]}}, {{"bullets": ["...", "..."]}}]}}"""

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean)
        slides = [s.get("bullets", [])[:6] for s in data.get("slides", [])]
        return slides or [["Sin información disponible"]]
    except Exception:
        # Fallback si Claude falla o el JSON viene mal: corta en bloques
        # crudos de 6 palabras para no dejar la slide vacía.
        palabras = texto.split()
        bloques = [" ".join(palabras[i:i + 6]) for i in range(0, min(len(palabras), 36), 6)]
        return [bloques] if bloques else [["Sin información disponible"]]


def _agregar_slide_titulo(prs: Presentation, doc: "DocumentoEditadoIn"):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # layout en blanco
    box = slide.shapes.add_textbox(Inches(0.7), Inches(2.7), Inches(8.6), Inches(1.8))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = doc.titulo
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = NAVY_RGB
    p.alignment = PP_ALIGN.CENTER

    p2 = tf.add_paragraph()
    p2.text = f"Autor: {doc.autor}"
    p2.font.size = Pt(16)
    p2.font.italic = True
    p2.font.color.rgb = GRAY_RGB
    p2.alignment = PP_ALIGN.CENTER


def _agregar_slide_bullets(prs: Presentation, titulo: str, bullets: list[str], continuacion: bool = False):
    """Una slide con hasta 6 bullets, fuente 24pt+, regla 6x6."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    titulo_mostrado = f"{titulo} (cont.)" if continuacion else titulo
    title_box = slide.shapes.add_textbox(Inches(0.5), Inches(0.35), Inches(9), Inches(0.7))
    tf_title = title_box.text_frame
    p_title = tf_title.paragraphs[0]
    p_title.text = titulo_mostrado
    p_title.font.size = Pt(28)
    p_title.font.bold = True
    p_title.font.color.rgb = NAVY_RGB

    body_box = slide.shapes.add_textbox(Inches(0.7), Inches(1.4), Inches(8.6), Inches(5.6))
    tf_body = body_box.text_frame
    tf_body.word_wrap = True

    bullets = bullets[:6] or ["Sin información disponible"]
    for i, bullet in enumerate(bullets):
        p = tf_body.paragraphs[0] if i == 0 else tf_body.add_paragraph()
        p.text = f"•  {bullet}"
        p.font.size = Pt(24)
        p.space_after = Pt(18)


async def _construir_ppt(doc: "DocumentoEditadoIn") -> bytes:
    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(7.5)

    _agregar_slide_titulo(prs, doc)

    epi = doc.epidemiologia or EpidemiologiaIn()
    epi_texto = f"Internacional: {epi.internacional or '—'}\nNacional: {epi.nacional or '—'}"

    secciones_a_resumir = [("Epidemiología internacional y nacional", epi_texto)]
    secciones_a_resumir += [(label, getattr(doc, key)) for key, label in SECCIONES_PPT]
    if doc.referencias:
        secciones_a_resumir.append(("Referencias", "\n".join(doc.referencias)))

    # Resume TODAS las secciones en paralelo (no secuencial)
    resultados = await asyncio.gather(*(_resumir_seccion(titulo, texto) for titulo, texto in secciones_a_resumir))

    for (titulo, _texto), slides_bullets in zip(secciones_a_resumir, resultados):
        for i, bullets in enumerate(slides_bullets):
            _agregar_slide_bullets(prs, titulo, bullets, continuacion=(i > 0))

    buffer = BytesIO()
    prs.save(buffer)
    buffer.seek(0)
    return buffer.read()


async def _construir_clase_formal(doc: "DocumentoEditadoIn") -> dict:
    """Mismo resumen 6x6 que _construir_ppt, pero en vez de dibujar slides
    en un .pptx, crea el contenido directo en Clases Formales:
    1 fila en clases_formales (nombre = titulo del documento) + 1 fila en
    paginas_clase por cada slide resuelta -titulo (tipo_herramienta
    'titulo') y una por cada bloque de bullets (tipo_herramienta
    'titulo_texto', bullets guardados tal cual en config, mismo array que
    ya usa _agregar_slide_bullets-. No se sube ninguna imagen aca -eso se
    agrega despues a mano en el constructor-."""
    contenido = sb.table("clases_formales").insert({"nombre": doc.titulo}).execute()
    clase_formal_id = contenido.data[0]["id"]

    orden = 1.0

    sb.table("paginas_clase").insert({
        "clase_formal_id": clase_formal_id,
        "orden": orden,
        "titulo": doc.titulo,
        "tipo_herramienta": "titulo",
        "config": {},
    }).execute()
    orden += 1.0

    epi = doc.epidemiologia or EpidemiologiaIn()
    epi_texto = f"Internacional: {epi.internacional or '—'}\nNacional: {epi.nacional or '—'}"

    secciones_a_resumir = [("Epidemiología internacional y nacional", epi_texto)]
    secciones_a_resumir += [(label, getattr(doc, key)) for key, label in SECCIONES_PPT]
    if doc.referencias:
        secciones_a_resumir.append(("Referencias", "\n".join(doc.referencias)))

    # Resume TODAS las secciones en paralelo (no secuencial), igual que el PPT
    resultados = await asyncio.gather(*(_resumir_seccion(titulo, texto) for titulo, texto in secciones_a_resumir))

    filas_paginas = []
    for (titulo, _texto), slides_bullets in zip(secciones_a_resumir, resultados):
        for i, bullets in enumerate(slides_bullets):
            titulo_mostrado = f"{titulo} (cont.)" if i > 0 else titulo
            filas_paginas.append({
                "clase_formal_id": clase_formal_id,
                "orden": orden,
                "titulo": titulo_mostrado,
                "tipo_herramienta": "titulo_texto",
                "config": {"bullets": (bullets[:6] or ["Sin información disponible"])},
            })
            orden += 1.0

    if filas_paginas:
        sb.table("paginas_clase").insert(filas_paginas).execute()

    return {"clase_formal_id": clase_formal_id}


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


@router.post("/ppt")
async def documento_a_ppt(
    body: DocumentoEditadoIn,
    interrogador: dict = Depends(get_current_interrogador),
):
    """Recibe el texto YA EDITADO por el interrogador y devuelve un
    PowerPoint EDITABLE pensado para docencia: cada sección se RESUME con
    Claude aplicando la regla 6x6 (máx. 6 bullets por slide, máx. 6 palabras
    por bullet), dividiendo en varias diapositivas si no cabe. No es una
    copia del texto — es un resumen del resumen, para mantener la atención
    de los alumnos. Independiente del PDF."""
    ppt_bytes = await _construir_ppt(body)
    nombre = "".join(c for c in body.titulo if c.isalnum() or c in " -_")[:60].strip() or "documento"
    return Response(
        content=ppt_bytes,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{nombre}.pptx"'},
    )


@router.post("/clase-formal")
async def documento_a_clase_formal(
    body: DocumentoEditadoIn,
    interrogador: dict = Depends(get_current_interrogador),
):
    """Recibe el texto YA EDITADO por el interrogador -mismo payload que
    /ppt- y, en vez de un archivo descargable, crea el contenido directo
    en Clases Formales (clases_formales + paginas_clase), listo para abrir
    en el constructor y agregarle imagenes, trivia o dejar el semaforo
    activo. Devuelve el id del contenido creado para redirigir al
    constructor."""
    return await _construir_clase_formal(body)
