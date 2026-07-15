"""
services/claude_client.py
Llamado a la API de Claude para proponer las 4 alternativas incorrectas
de una pregunta, dada la pregunta y la respuesta correcta.

Variable de entorno esperada (Render):
  ANTHROPIC_API_KEY
"""

import json
import os

import anthropic

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-sonnet-5"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def generar_alternativas(pregunta: str, respuesta_correcta: str, region: str, complejidad: str) -> list[str]:
    """Devuelve una lista de 4 strings: alternativas incorrectas, plausibles,
    del mismo nivel de complejidad y coherentes con la región anatómica."""

    prompt = f"""Eres un docente de traumatología creando un examen de opción múltiple para alumnos de 4to año de medicina.

Región: {region}
Complejidad: {complejidad}
Pregunta: {pregunta}
Respuesta correcta: {respuesta_correcta}

Genera exactamente 4 alternativas INCORRECTAS pero plausibles para esta pregunta, del mismo nivel de complejidad y longitud similar a la respuesta correcta. No repitas la respuesta correcta ni la parafrasees. No incluyas la letra ni numeración, solo el texto de cada alternativa.

Responde SOLO con un JSON válido, sin texto adicional, con este formato exacto:
{{"alternativas": ["opción 1", "opción 2", "opción 3", "opción 4"]}}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    texto = response.content[0].text.strip()
    texto = texto.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(texto)
        alternativas = data["alternativas"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raise ValueError(f"La IA no devolvió un JSON válido: {e}")

    if len(alternativas) != 4:
        raise ValueError(f"Se esperaban 4 alternativas, la IA devolvió {len(alternativas)}")

    return alternativas
  
