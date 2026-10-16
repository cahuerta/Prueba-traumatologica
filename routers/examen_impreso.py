"""
routers/examen_impreso.py
Examen en papel: genera una copia imprimible (PDF) del banco de preguntas con
la MISMA lógica del examen online -preguntas en orden revuelto y alternativas
revueltas por copia-, identificada con un FOLIO único (F-0001, F-0002...).

Cada vez que se genera una copia se crea un folio nuevo y se guarda, en
examenes_impresos, la copia completa tal como se imprimió (orden de las
preguntas, orden de las alternativas e índice de la correcta en ese orden).
Con el folio se puede:
  - volver a bajar exactamente la misma prueba (GET /{folio}/prueba.pdf)
  - bajar la pauta de corrección de ESA copia (GET /{folio}/pauta.pdf)

Como se guarda la copia completa, la pauta de un folio siempre coincide con
el papel impreso, aunque después se edite o desactive una pregunta del banco.
"""

import io
import random
import textwrap
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image as PILImage
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from routers.auth import sb, get_current_interrogador

router = APIRouter(prefix="/examen-impreso", tags=["examen-impreso"])

LETRAS = "ABCDE"
TITULO_POR_DEFECTO = "Prueba de Especialidades - Traumatología"


# ---------------- MODELOS ----------------
class GenerarIn(BaseModel):
    titulo: str = TITULO_POR_DEFECTO
    carga_id: Optional[str] = None          # solo preguntas de esa carga
    region: Optional[str] = None            # solo preguntas de esa región
    excluir_por_revisar: bool = False       # deja fuera las marcadas "por revisar"


# ---------------- HELPERS ----------------
def _folio_texto(folio: int) -> str:
    return f"F-{folio:04d}"


def _t(texto) -> str:
    """Texto seguro para reportlab: escapa XML, respeta saltos de línea y
    reemplaza lo que la fuente estándar (Latin-1/cp1252) no puede dibujar."""
    texto = str(texto or "")
    texto = texto.encode("cp1252", "replace").decode("cp1252")
    return escape(texto).replace("\n", "<br/>")


def _cortar(texto, largo):
    return textwrap.shorten(" ".join(str(texto or "").split()), width=largo, placeholder="...")


def _copia_por_folio(folio: int) -> dict:
    filas = sb.table("examenes_impresos").select("*").eq("folio", folio).execute().data
    if not filas:
        raise HTTPException(404, f"No existe el folio {_folio_texto(folio)}")
    return filas[0]


def _respuesta_pdf(contenido: bytes, nombre: str) -> Response:
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{nombre}"'},
    )


# ---------------- ESTILOS ----------------
EST_TITULO = ParagraphStyle("titulo", fontName="Helvetica-Bold", fontSize=15, leading=19, spaceAfter=2)
EST_FOLIO = ParagraphStyle("folio", fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=colors.HexColor("#0E1526"))
EST_SUB = ParagraphStyle("sub", fontName="Helvetica", fontSize=9.5, leading=12, textColor=colors.HexColor("#444444"))
EST_PREG = ParagraphStyle("preg", fontName="Helvetica", fontSize=10.5, leading=13.5, spaceAfter=3)
EST_OPC = ParagraphStyle("opc", fontName="Helvetica", fontSize=10, leading=12.5, leftIndent=16, firstLineIndent=-16, spaceAfter=1.5)
EST_CELDA = ParagraphStyle("celda", fontName="Helvetica", fontSize=8.5, leading=10.5)
EST_CELDA_B = ParagraphStyle("celdab", parent=EST_CELDA, fontName="Helvetica-Bold")


# ---------------- IMÁGENES ----------------
def _imagen_flowable(media_url, cache: dict):
    """Descarga la foto de la pregunta del bucket privado, la aliviana
    (JPEG, máx. 1400 px) y la devuelve ajustada a un recuadro de 11 x 8 cm."""
    try:
        if media_url not in cache:
            crudo = sb.storage.from_("preguntas").download(media_url)
            im = PILImage.open(io.BytesIO(crudo)).convert("RGB")
            im.thumbnail((1400, 1400))
            salida = io.BytesIO()
            im.save(salida, "JPEG", quality=82)
            cache[media_url] = (salida.getvalue(), im.size)
        datos, (w, h) = cache[media_url]
        escala = min(11 * cm / w, 8 * cm / h)
        return Image(io.BytesIO(datos), width=w * escala, height=h * escala)
    except Exception:
        return Paragraph("<i>[Imagen no disponible]</i>", EST_SUB)


# ---------------- PDF: LA PRUEBA ----------------
def _pie(folio_txt, etiqueta):
    def dibujar(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(2 * cm, 1.1 * cm, f"{etiqueta} - Folio {folio_txt}")
        canvas.drawRightString(A4[0] - 2 * cm, 1.1 * cm, f"Página {doc.page}")
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawRightString(A4[0] - 2 * cm, A4[1] - 1.2 * cm, folio_txt)
        canvas.restoreState()
    return dibujar


def construir_pdf_prueba(copia: dict) -> bytes:
    folio_txt = _folio_texto(copia["folio"])
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=1.8 * cm,
        title=f"{copia['titulo']} - {folio_txt}", author="Cátedra de Traumatología",
    )

    story = [
        Paragraph(_t(copia["titulo"]), EST_TITULO),
        Paragraph(f"Folio {folio_txt}", EST_FOLIO),
        Spacer(1, 6),
    ]
    campos = Table(
        [["Nombre:", "", "RUT / matrícula:", ""], ["Fecha:", "", "Nota:", ""]],
        colWidths=[2 * cm, 7 * cm, 3.4 * cm, 4.6 * cm], rowHeights=[0.8 * cm, 0.8 * cm],
    )
    campos.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW", (1, 0), (1, -1), 0.6, colors.black), ("LINEBELOW", (3, 0), (3, -1), 0.6, colors.black),
    ]))
    story += [campos, Spacer(1, 14)]

    cache = {}
    for n, p in enumerate(copia["preguntas"], start=1):
        bloque = [Paragraph(f"<b>{n}.</b> {_t(p['pregunta'])}", EST_PREG)]
        if p.get("media_url"):
            if p.get("media_tipo") == "video":
                bloque.append(Paragraph("<i>[Esta pregunta tiene un video; no se puede imprimir]</i>", EST_SUB))
            else:
                bloque += [Spacer(1, 2), _imagen_flowable(p["media_url"], cache), Spacer(1, 4)]
        for i, op in enumerate(p["opciones"]):
            bloque.append(Paragraph(f"<b>{LETRAS[i]})</b>&nbsp;&nbsp;{_t(op)}", EST_OPC))
        bloque.append(Spacer(1, 11))
        story.append(KeepTogether(bloque))

    pie = _pie(folio_txt, "Prueba")
    doc.build(story, onFirstPage=pie, onLaterPages=pie)
    return buf.getvalue()


# ---------------- PDF: LA PAUTA ----------------
def construir_pdf_pauta(copia: dict) -> bytes:
    folio_txt = _folio_texto(copia["folio"])
    preguntas = copia["preguntas"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=1.8 * cm,
        title=f"Pauta - {folio_txt}", author="Cátedra de Traumatología",
    )

    story = [
        Paragraph("PAUTA DE CORRECCIÓN", EST_TITULO),
        Paragraph(f"Folio {folio_txt}", EST_FOLIO),
        Paragraph(f"{_t(copia['titulo'])} - {copia['total_preguntas']} preguntas", EST_SUB),
        Spacer(1, 10),
    ]

    # Pauta rápida: filas de 10 preguntas (números arriba, letras abajo).
    por_fila = 10
    filas = []
    for ini in range(0, len(preguntas), por_fila):
        tramo = range(ini, min(ini + por_fila, len(preguntas)))
        filas.append([str(i + 1) for i in tramo] + [""] * (por_fila - len(tramo)))
        filas.append([LETRAS[preguntas[i]["correcta"]] for i in tramo] + [""] * (por_fila - len(tramo)))
    rapida = Table(filas, colWidths=[1.7 * cm] * por_fila)
    estilo = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
    ]
    for r in range(0, len(filas), 2):
        estilo += [
            ("BACKGROUND", (0, r), (-1, r), colors.HexColor("#E8ECF3")),
            ("FONTNAME", (0, r + 1), (-1, r + 1), "Helvetica-Bold"), ("FONTSIZE", (0, r + 1), (-1, r + 1), 12),
        ]
    rapida.setStyle(TableStyle(estilo))
    story += [rapida, Spacer(1, 16)]

    # Detalle: para revisar cada pregunta contra su alternativa correcta.
    detalle = [[Paragraph("N°", EST_CELDA_B), Paragraph("Resp.", EST_CELDA_B),
                Paragraph("Pregunta", EST_CELDA_B), Paragraph("Alternativa correcta", EST_CELDA_B)]]
    for n, p in enumerate(preguntas, start=1):
        detalle.append([
            Paragraph(str(n), EST_CELDA),
            Paragraph(f"<b>{LETRAS[p['correcta']]}</b>", EST_CELDA),
            Paragraph(_t(_cortar(p["pregunta"], 110)), EST_CELDA),
            Paragraph(_t(_cortar(p["opciones"][p["correcta"]], 90)), EST_CELDA),
        ])
    tabla = Table(detalle, colWidths=[1.1 * cm, 1.3 * cm, 8.3 * cm, 6.3 * cm], repeatRows=1)
    tabla.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BBBBBB")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8ECF3")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(tabla)

    pie = _pie(folio_txt, "Pauta")
    doc.build(story, onFirstPage=pie, onLaterPages=pie)
    return buf.getvalue()


# ---------------- ENDPOINTS ----------------
@router.post("/generar")
def generar_copia(body: GenerarIn, interrogador: dict = Depends(get_current_interrogador)):
    """Crea una copia nueva (folio nuevo) con las preguntas y alternativas
    en orden revuelto. No devuelve el PDF: se baja con /{folio}/prueba.pdf."""
    q = sb.table("banco_preguntas").select(
        "id, numero_origen, pregunta, opciones, correcta, media_url, media_tipo"
    ).eq("activo", True)
    if body.carga_id:
        q = q.eq("carga_id", body.carga_id)
    if body.region:
        q = q.eq("region", body.region)
    if body.excluir_por_revisar:
        q = q.eq("revisar", False)
    filas = q.execute().data
    if not filas:
        raise HTTPException(409, "No hay preguntas activas con esos filtros")

    random.shuffle(filas)  # orden de las preguntas, distinto en cada copia

    preguntas = []
    for f in filas:
        # Mismo mecanismo que el examen online: por pregunta, un orden
        # distinto de alternativas. orden[pos_nueva] = índice_original.
        orden = list(range(len(f["opciones"])))
        random.shuffle(orden)
        preguntas.append({
            "pregunta_id": f["id"],
            "numero_origen": f.get("numero_origen"),
            "pregunta": f["pregunta"],
            "opciones": [f["opciones"][i] for i in orden],
            "correcta": orden.index(f["correcta"]),
            "media_url": f.get("media_url"),
            "media_tipo": f.get("media_tipo"),
        })

    fila = sb.table("examenes_impresos").insert({
        "titulo": body.titulo.strip() or TITULO_POR_DEFECTO,
        "filtros": {
            "carga_id": body.carga_id, "region": body.region,
            "excluir_por_revisar": body.excluir_por_revisar,
        },
        "total_preguntas": len(preguntas),
        "preguntas": preguntas,
        "creado_por": interrogador["sub"],
    }).execute().data[0]

    return {
        "id": fila["id"], "folio": fila["folio"], "folio_texto": _folio_texto(fila["folio"]),
        "total_preguntas": fila["total_preguntas"],
    }


@router.get("")
def listar_copias(interrogador: dict = Depends(get_current_interrogador)):
    filas = sb.table("examenes_impresos").select(
        "id, folio, titulo, filtros, total_preguntas, created_at, interrogadores(nombre)"
    ).order("folio", desc=True).limit(200).execute().data
    for f in filas:
        f["folio_texto"] = _folio_texto(f["folio"])
        f["creado_por_nombre"] = (f.pop("interrogadores", None) or {}).get("nombre")
    return filas


@router.get("/{folio}/prueba.pdf")
def descargar_prueba(folio: int, interrogador: dict = Depends(get_current_interrogador)):
    copia = _copia_por_folio(folio)
    return _respuesta_pdf(construir_pdf_prueba(copia), f"Prueba-{_folio_texto(folio)}.pdf")


@router.get("/{folio}/pauta.pdf")
def descargar_pauta(folio: int, interrogador: dict = Depends(get_current_interrogador)):
    copia = _copia_por_folio(folio)
    return _respuesta_pdf(construir_pdf_pauta(copia), f"Pauta-{_folio_texto(folio)}.pdf")
