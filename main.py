"""
Backend — Examen Musculoesquelético
main.py — organiza la app, monta los routers, y aplica schema.sql al arrancar.
"""

import traceback

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    clases_formales_avatar,
    examen_impreso,
)
from db_init import inicializar_schema

app = FastAPI(title="Examen Musculoesquelético API")


# Errores internos con su mensaje real. Se registra ANTES que CORS para que
# quede por dentro de él: así la respuesta de error sale con los permisos
# CORS y el navegador muestra el mensaje verdadero, en vez de
# "Failed to fetch". El detalle completo queda también en el log de Render.
@app.middleware("http")
async def errores_con_mensaje(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        print(f"[ERROR] {request.method} {request.url.path}\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={"detail": f"Error interno en {request.url.path}: {type(e).__name__}: {e}"},
        )


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
app.include_router(examen_impreso.router)
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
app.include_router(clases_formales_avatar.router)


@app.on_event("startup")
def startup():
    inicializar_schema()
    # Vuelve a programar el cierre único de las sesiones de examen en curso
    # (se pierde si el servicio se reinicia en medio de un examen).
    try:
        examen.reprogramar_cierres_pendientes()
    except Exception as e:
        print(f"[startup] ERROR reprogramando cierres de sesiones: {type(e).__name__}: {e}")
