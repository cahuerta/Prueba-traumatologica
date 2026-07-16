"""
Backend — Examen Musculoesquelético
main.py — solo organiza la app y monta los routers de cada módulo.
La lógica de cada grupo de endpoints vive en su propio archivo.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import auth, preguntas, materiales, sesiones, examen, analisis

app = FastAPI(title="Examen Musculoesquelético API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app|https://traumatologiaultal2026\.icarticular\.cl|http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(preguntas.router)
app.include_router(materiales.router)
app.include_router(sesiones.router)
app.include_router(examen.router)
app.include_router(analisis.router)
