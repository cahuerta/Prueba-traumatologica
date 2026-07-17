"""
Backend — Examen Musculoesquelético
main.py — organiza la app, monta los routers, y aplica schema.sql al arrancar.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import auth, preguntas, materiales, sesiones, examen, analisis
from db_init import inicializar_schema

app = FastAPI(title="Examen Musculoesquelético API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app|https://traumatologiautal2026\.icarticular\.cl|http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(preguntas.router)
app.include_router(materiales.router)
app.include_router(sesiones.router)
app.include_router(examen.router)
app.include_router(analisis.router)


@app.on_event("startup")
def startup():
    inicializar_schema()
    
