"""
Backend — Examen Musculoesquelético
main.py — organiza la app, monta los routers, y aplica schema.sql al arrancar.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import (
    auth,
    preguntas,
    materiales,
    sesiones,
    examen,
    analisis,
    documentos,
    casos_vivo_alumno,
    casos_vivo_profesor,
    casos_vivo_panel,
    conjuntos,
    alumnos,
    clases_formales_sesiones,
    clases_formales_paginas,
    clases_formales_contenido,
    clases_formales_preguntas,
    clases_formales_semaforo,
    clases_formales_trivia,
    clases_formales_actual,
    clases_formales_ingreso,
    sesion_resolver,
)
from db_init import inicializar_schema

app = FastAPI(title="Examen Musculoesquelético API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app|https://traumatologiautal2026\.icarticular\.cl|https://catedra\.hipokratia\.health|http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(preguntas.router)
app.include_router(materiales.router)
app.include_router(sesiones.router)
app.include_router(examen.router)
app.include_router(analisis.router)
app.include_router(documentos.router)
app.include_router(casos_vivo_alumno.router)
app.include_router(casos_vivo_profesor.router)
app.include_router(casos_vivo_panel.router)
app.include_router(conjuntos.router)
app.include_router(alumnos.router)

# ---------------- Clases Formales (rama nueva, separada de casos_vivo) ----------------
app.include_router(clases_formales_sesiones.router)
app.include_router(clases_formales_paginas.router)
app.include_router(clases_formales_contenido.router)
app.include_router(clases_formales_preguntas.router)
app.include_router(clases_formales_semaforo.router)
app.include_router(clases_formales_trivia.router)
app.include_router(clases_formales_actual.router)
app.include_router(clases_formales_ingreso.router)
app.include_router(sesion_resolver.router)


@app.on_event("startup")
def startup():
    inicializar_schema()
    
