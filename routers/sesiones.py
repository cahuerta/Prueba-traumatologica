"""
routers/sesiones.py
Ciclo de vida de una sesión de examen ("clase"): asistencia por
nombre+RUT validada contra el conjunto de alumnos activo, encuesta en
vivo, y la tabla de resultados con el detalle de cada pregunta
respondida.

Los alumnos ya no se cargan por sesión: el listado vive en el conjunto
activo (ver routers/alumnos.py y routers/conjuntos.py). Cualquier RUT
que este dentro del conjunto activo puede marcar asistencia en
cualquier sesión.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador, requiere_admin
from routers.conjuntos_comun import obtener_conjunto_activo_id

router = APIRouter(prefix="/sesiones", tags=["sesiones"])

PAQUETES = ("agil", "estandar", "exigente")


# ---------------- MODELOS ----------------
class SesionIn(BaseModel):
    nombre: str
    fecha: str  # YYYY-MM-DD

class VotoIn(BaseModel):
    alumno_id: str
    paquete: str

class AsistenciaIn(BaseModel):
    nombre: str
    rut: str


# ---------------- CREACIÓN Y LISTADO DE SESIONES (admin) ----------------
@router.post("")
def crear_sesion(s: SesionIn, admin: dict = Depends(requiere_admin)):
    """La sesión ya no recibe ni crea alumnos: cualquier alumno del
    conjunto activo puede marcar asistencia en ella."""
    res = sb.table("sesiones_examen").insert({
        "nombre": s.nombre, "fecha": s.fecha, "estado": "creada", "creado_por": admin["sub"]
    }).execute()
    return res.data[0]

@router.get("")
def listar_sesiones(interrogador: dict = Depends(get_current_interrogador)):
    return sb.table("sesiones_examen").select("*").order("fecha", desc=True).execute().data

@router.get("/{sesion_id}")
def ver_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    res = sb.table("sesiones_examen").select("*").eq("id", sesion_id).single().execute().data
    if not res:
        raise HTTPException(404, "Sesión no encontrada")
    return res


# ---------------- ASISTENCIA (ingreso por nombre + RUT, contra el conjunto activo) ----------------
@router.post("/{sesion_id}/abrir-asistencia")
def abrir_asistencia(sesion_id: str, admin: dict = Depends(requiere_admin)):
    sb.table("sesiones_examen").update({"estado": "asistencia"}).eq("id", sesion_id).execute()
    return {"ok": True}

@router.post("/{sesion_id}/asistencia")
def marcar_asistencia(sesion_id: str, body: AsistenciaIn):
    """El alumno escribe su nombre y RUT. Se corrobora contra el
    conjunto de alumnos actualmente activo."""
    conjunto_id = obtener_conjunto_activo_id()

    alumno = sb.table("alumnos").select("id").eq("rut", body.rut.strip()).eq("conjunto_id", conjunto_id).execute().data
    if not alumno:
        raise HTTPException(403, "RUT no reconocido en el conjunto activo")
    alumno_id = alumno[0]["id"]

    sb.table("alumnos").update({"nombre": body.nombre.strip()}).eq("id", alumno_id).execute()
    sb.table("asistencia").upsert({"sesion_id": sesion_id, "alumno_id": alumno_id}).execute()

    return {"ok": True, "alumno_id": alumno_id}

@router.get("/{sesion_id}/asistencia")
def ver_asistencia(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Consola docente: quién ha marcado asistencia hasta ahora, en vivo.
    El total de habilitados es el tamaño del conjunto activo."""
    conjunto_id = obtener_conjunto_activo_id()

    res = sb.table("asistencia").select("alumno_id, marcado_at, alumnos(nombre, rut)").eq("sesion_id", sesion_id).order("marcado_at").execute().data
    total = sb.table("alumnos").select("id", count="exact").eq("conjunto_id", conjunto_id).execute()
    return {"presentes": res, "total_habilitados": total.count, "total_presentes": len(res)}


# ---------------- ENCUESTA EN VIVO (Ágil / Estándar / Exigente) ----------------
@router.post("/{sesion_id}/abrir-encuesta")
def abrir_encuesta(sesion_id: str, admin: dict = Depends(requiere_admin)):
    sb.table("sesiones_examen").update({"estado": "encuesta"}).eq("id", sesion_id).execute()
    return {"ok": True}

@router.post("/{sesion_id}/votar")
def votar(sesion_id: str, body: VotoIn):
    if body.paquete not in PAQUETES:
        raise HTTPException(400, "Paquete inválido")
    sb.table("encuesta_votos").upsert({
        "sesion_id": sesion_id, "alumno_id": body.alumno_id, "paquete": body.paquete
    }).execute()
    return {"ok": True}

@router.get("/{sesion_id}/encuesta")
def ver_encuesta(sesion_id: str):
    res = sb.table("encuesta_votos").select("paquete").eq("sesion_id", sesion_id).execute()
    conteo = {"agil": 0, "estandar": 0, "exigente": 0}
    for r in res.data:
        conteo[r["paquete"]] += 1
    return conteo

@router.post("/{sesion_id}/cerrar-encuesta")
def cerrar_encuesta(sesion_id: str, admin: dict = Depends(requiere_admin)):
    conteo = ver_encuesta(sesion_id)
    ganador = max(conteo, key=conteo.get)
    sb.table("sesiones_examen").update({
        "estado": "en_curso", "paquete_elegido": ganador
    }).eq("id", sesion_id).execute()
    return {"paquete_elegido": ganador, "conteo": conteo}


# ---------------- TABLA DE RESULTADOS (detalle pregunta por pregunta) ----------------
@router.get("/{sesion_id}/resultados")
def resultados_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """
    Por cada alumno que rindió: su nota/puntaje, y el detalle de cada
    pregunta que le tocó (enunciado, su respuesta, si fue correcta,
    la alternativa correcta y la explicación).
    """
    instancias = sb.table("examen_instancia").select(
        "id, alumno_id, paquete, puntaje_total, porcentaje, nota, iniciado_at, finalizado_at, salidas_detectadas, alumnos(nombre, rut)"
    ).eq("sesion_id", sesion_id).execute().data

    resultados = []
    for inst in instancias:
        respuestas = sb.table("respuestas").select(
            "pregunta_id, opcion_elegida, correcta, banco_preguntas(pregunta, opciones, correcta, explicacion, region, complejidad)"
        ).eq("examen_instancia_id", inst["id"]).execute().data

        detalle = []
        for r in respuestas:
            bp = r["banco_preguntas"]
            detalle.append({
                "pregunta": bp["pregunta"],
                "region": bp["region"],
                "complejidad": bp["complejidad"],
                "opciones": bp["opciones"],
                "respuesta_alumno": bp["opciones"][r["opcion_elegida"]],
                "respuesta_correcta": bp["opciones"][bp["correcta"]],
                "correcta": r["correcta"],
                "explicacion": bp["explicacion"],
            })

        resultados.append({
            "alumno": inst["alumnos"],
            "paquete": inst["paquete"],
            "puntaje_total": inst["puntaje_total"],
            "porcentaje": inst["porcentaje"],
            "nota": inst["nota"],
            "finalizado": inst["finalizado_at"] is not None,
            "salidas_detectadas": inst["salidas_detectadas"],
            "n_preguntas_respondidas": len(detalle),
            "n_correctas": sum(1 for d in detalle if d["correcta"]),
            "n_incorrectas": sum(1 for d in detalle if not d["correcta"]),
            "detalle_preguntas": detalle,
        })

    return resultados
    
