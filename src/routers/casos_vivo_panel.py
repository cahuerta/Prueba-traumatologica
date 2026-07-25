"""
routers/casos_vivo_panel.py
Endpoint UNIFICADO para AdminVivo y ProyeccionVivo: junta en una sola
respuesta lo que antes eran 3 llamadas por separado a casos_vivo_alumno.py
y casos_vivo_profesor.py (estado actual, resultados agregados, asistencia).

No reemplaza ni modifica esos tres endpoints originales -siguen existiendo
tal cual, activos y vigentes, los sigue usando el alumno y cualquier otro
consumidor que los necesite por separado-. Este archivo solo REUSA esa
misma logica llamando a las funciones ya existentes, y devuelve todo
junto en una sola respuesta.

Motivo: el polling de AdminVivo/ProyeccionVivo hacia las 3 llamadas en
paralelo con Promise.all en el frontend, y si UNA fallaba (por cualquier
motivo puntual e intermitente), Promise.all descartaba las otras dos que
si habian respondido bien, mostrando error aunque el backend estuviera
funcionando. Unificar a una sola llamada reduce a un tercio la cantidad
de peticiones del polling en vivo, y elimina ese punto de falla.
"""

from fastapi import APIRouter, Depends

from routers.auth import get_current_interrogador
from routers.casos_vivo_alumno import estado_actual_alumno, resultados_agregados
from routers.casos_vivo_profesor import ver_asistencia_vivo
from routers.casos_vivo_comun import obtener_sesion

router = APIRouter(prefix="/casos-vivo", tags=["casos-vivo-panel"])


@router.get("/vivo/{sesion_id}/panel")
def panel_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Une estado_actual_alumno + resultados_agregados + ver_asistencia_vivo
    en una sola respuesta. Requiere login de interrogador (igual que
    ver_asistencia_vivo, que ya lo requeria)."""
    sesion = obtener_sesion(sesion_id)
    codigo_acceso = sesion["codigo_acceso"]

    estado = estado_actual_alumno(codigo_acceso)
    resultados = resultados_agregados(sesion_id)
    asistencia = ver_asistencia_vivo(sesion_id, interrogador)

    return {
        **estado,
        "resultados": resultados,
        "asistencia": asistencia,
    }
  
