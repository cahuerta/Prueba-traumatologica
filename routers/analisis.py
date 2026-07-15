"""
routers/analisis.py
Dashboard docente: rendimiento del curso en una sesión de examen,
usando las vistas SQL analisis_sesion / analisis_preguntas / analisis_complejidad.
"""

from fastapi import APIRouter, Depends

from routers.auth import sb, get_current_interrogador

router = APIRouter(prefix="/sesiones", tags=["analisis"])


@router.get("/{sesion_id}/analisis")
def analisis_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    resumen = sb.table("analisis_sesion").select("*").eq("sesion_id", sesion_id).execute().data
    preguntas = sb.table("analisis_preguntas").select("*").eq("sesion_id", sesion_id).order("pct_acierto").execute().data
    complejidad = sb.table("analisis_complejidad").select("*").eq("sesion_id", sesion_id).execute().data

    return {
        "resumen": resumen[0] if resumen else None,
        "preguntas_mas_falladas": preguntas[:10],
        "por_complejidad": complejidad,
    }
  
