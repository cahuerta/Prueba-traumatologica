"""
services/fundamento_vivo.py
Al revelar una pregunta en la sesion en vivo: busca TODOS los materiales
(docx) subidos para la misma region de la pregunta, extrae su texto tal
cual esta (sin segmentar ni pre-procesar nada), y le pide a Claude un
resumen CORTO y fundamentado -no el texto completo- citando de que
material sale el argumento.

No se agregan tablas ni se modifica materiales.py: se lee el archivo
al vuelo desde Storage cada vez.
"""

import io
from typing import List

import docx
import anthropic

from routers.auth import sb

client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY del entorno, mismo patron que claude_client.py


def _extraer_texto_docx(contenido_bytes: bytes) -> str:
    documento = docx.Document(io.BytesIO(contenido_bytes))
    parrafos = [p.text for p in documento.paragraphs if p.text.strip()]
    return "\n".join(parrafos)


def _materiales_de_la_region(region: str) -> List[dict]:
    """Solo materiales tipo docx son legibles como texto; PPT se ignora aqui."""
    materiales = sb.table("materiales").select("id, titulo, storage_path, tipo").eq("region", region).execute().data
    return [m for m in materiales if m["storage_path"].lower().endswith(".docx")]


def buscar_fundamento(region: str, pregunta: str, opciones: List[str], correcta: int) -> dict:
    """
    Devuelve {"explicacion": str, "fuentes": [titulos]}.
    Si no hay materiales docx en la region, devuelve explicacion vacia y fuentes [].
    """
    materiales = _materiales_de_la_region(region)
    if not materiales:
        return {"explicacion": "", "fuentes": []}

    bloques_texto = []
    fuentes = []
    for m in materiales:
        try:
            contenido = sb.storage.from_("materiales").download(m["storage_path"])
            texto = _extraer_texto_docx(contenido)
            if texto.strip():
                bloques_texto.append("### " + m["titulo"] + "\n" + texto)
                fuentes.append(m["titulo"])
        except Exception:
            continue  # material ilegible o corrupto, se omite sin romper el flujo

    if not bloques_texto:
        return {"explicacion": "", "fuentes": []}

    contexto = "\n\n".join(bloques_texto)
    respuesta_correcta_texto = opciones[correcta]

    prompt = (
        "Eres un docente de traumatologia fundamentando la respuesta correcta de una "
        "pregunta de examen, en vivo, frente a alumnos de 4to año de medicina.\n\n"
        "PREGUNTA: " + pregunta + "\n"
        "OPCIONES: " + str(opciones) + "\n"
        "RESPUESTA CORRECTA: " + respuesta_correcta_texto + "\n\n"
        "MATERIAL DE REFERENCIA DISPONIBLE (documentos subidos por los docentes de esta region, "
        "puede haber mas de uno, cada uno bajo su titulo con formato '### Titulo'):\n"
        + contexto + "\n\n"
        "Antes de responder, identifica internamente cual de los materiales entregados arriba "
        "es el que realmente corresponde al cuadro clinico de la pregunta (guiate por los signos, "
        "sintomas y contexto clinico descritos en la pregunta y las opciones, comparandolos con el "
        "titulo y contenido de cada material). Si hay mas de un material, usa EXCLUSIVAMENTE el que "
        "corresponda a ese cuadro clinico especifico e ignora por completo el resto, aunque traten "
        "temas de la misma region anatomica.\n\n"
        "Redacta un resumen CORTO (maximo 4-5 lineas), fundamentado, que explique por que "
        "esa es la respuesta correcta, basandote unicamente en el material que identificaste como "
        "correspondiente. No copies el texto completo del material, sintetiza el argumento "
        "clinico clave."
    )

    mensaje = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    explicacion = "".join(bloque.text for bloque in mensaje.content if bloque.type == "text")

    return {"explicacion": explicacion.strip(), "fuentes": fuentes}
    
