"""
routers/sesiones.py
Ciclo de vida de una sesión de examen ("clase"): asistencia por
nombre+RUT validada contra el conjunto de alumnos activo, encuesta en
vivo, y la tabla de resultados con el detalle de cada pregunta
respondida.

Los alumnos ya no se cargan por sesión: el listado vive en el conjunto
(ver routers/alumnos.py y routers/conjuntos.py). Cada sesión guarda el
conjunto que estaba activo al crearla, y solo los alumnos de ESE conjunto
pueden ingresar, aunque después se active otro.

Horario: al cerrar la encuesta el examen parte para todos y se fija su
hora de término (ver routers/examen.py: cierre único programado).

Pase del alumno: al ingresar con nombre + RUT/matrícula (POST
/{sesion_id}/asistencia) el alumno se valida UNA sola vez y recibe un
"pase" firmado, válido solo para esa sesión y ese alumno. Con ese pase la
pantalla de espera consulta GET /{sesion_id}/estado-alumno (solo devuelve
el estado de la sesión, nunca preguntas) sin volver a la base de datos a
revalidarlo. El pase se firma con una clave DISTINTA a la del login de
profesores, así que no sirve como token de profesor.
"""

import hashlib
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador, requiere_admin, JWT_SECRET, JWT_ALGO
from routers.conjuntos_comun import obtener_conjunto_activo_id
from routers.examen import minutos_del_paquete, programar_cierre_sesion

router = APIRouter(prefix="/sesiones", tags=["sesiones"])

PAQUETES = ("agil", "estandar", "exigente")

# Clave propia del pase de alumno, derivada del secreto del proyecto pero
# distinta a la del login de profesores (un pase NO valida como profesor).
_CLAVE_PASE_ALUMNO = hashlib.sha256(f"{JWT_SECRET}|pase-alumno".encode()).hexdigest()
_DURACION_PASE_SEG = 12 * 3600  # cubre la jornada completa del examen


def _crear_pase_alumno(sesion_id: str, alumno_id: str) -> str:
    ahora = int(time.time())
    payload = {"tipo": "pase_alumno", "sesion_id": sesion_id, "alumno_id": alumno_id,
               "iat": ahora, "exp": ahora + _DURACION_PASE_SEG}
    return jwt.encode(payload, _CLAVE_PASE_ALUMNO, algorithm=JWT_ALGO)


def _validar_pase_alumno(pase: str, sesion_id: str) -> dict:
    try:
        payload = jwt.decode(pase, _CLAVE_PASE_ALUMNO, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "Tu ingreso expiró o no es válido. Vuelve a ingresar con tu nombre y RUT o matrícula.")
    if payload.get("tipo") != "pase_alumno" or payload.get("sesion_id") != sesion_id:
        raise HTTPException(401, "Este ingreso no corresponde a esta sesión. Vuelve a ingresar.")
    return payload


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
    """La sesión ya no recibe ni crea alumnos. Queda amarrada al conjunto
    activo AL CREARLA: solo los alumnos de ese conjunto pueden ingresar, y
    no cambia aunque después se active otro conjunto."""
    res = sb.table("sesiones_examen").insert({
        "nombre": s.nombre, "fecha": s.fecha, "estado": "creada", "creado_por": admin["sub"],
        "conjunto_id": obtener_conjunto_activo_id(),
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


def _conjunto_de_la_sesion(sesion_id: str) -> str:
    """Conjunto guardado en la sesión al crearla (no el activo ahora)."""
    filas = sb.table("sesiones_examen").select("conjunto_id").eq("id", sesion_id).execute().data
    if not filas:
        raise HTTPException(404, "Sesión no encontrada")
    if not filas[0]["conjunto_id"]:
        raise HTTPException(409, "Esta sesión se creó antes de que la sesión guardara su conjunto. Crea una sesión nueva.")
    return filas[0]["conjunto_id"]


# ---------------- ASISTENCIA (ingreso por nombre + RUT, contra el conjunto de la sesión) ----------------
@router.post("/{sesion_id}/abrir-asistencia")
def abrir_asistencia(sesion_id: str, admin: dict = Depends(requiere_admin)):
    sb.table("sesiones_examen").update({"estado": "asistencia"}).eq("id", sesion_id).execute()
    return {"ok": True}

@router.post("/{sesion_id}/asistencia")
def marcar_asistencia(sesion_id: str, body: AsistenciaIn):
    """El alumno escribe su nombre y RUT/matrícula. Se corrobora contra el
    conjunto de alumnos de la sesión (el que estaba activo al crearla)."""
    conjunto_id = _conjunto_de_la_sesion(sesion_id)

    alumno = sb.table("alumnos").select("id").eq("rut", body.rut.strip()).eq("conjunto_id", conjunto_id).execute().data
    if not alumno:
        raise HTTPException(403, "RUT o matrícula no reconocido en el curso de esta sesión")
    alumno_id = alumno[0]["id"]

    sb.table("alumnos").update({"nombre": body.nombre.strip()}).eq("id", alumno_id).execute()
    sb.table("asistencia").upsert({"sesion_id": sesion_id, "alumno_id": alumno_id}).execute()

    return {"ok": True, "alumno_id": alumno_id, "pase": _crear_pase_alumno(sesion_id, alumno_id)}

@router.get("/{sesion_id}/estado-alumno")
def estado_para_alumno(sesion_id: str, pase: str):
    """Estado de la sesión para la pantalla de espera del alumno. Exige el
    pase entregado al ingresar (va en la URL, ?pase=..., para que el GET
    siga siendo "simple" y no genere preflight en cada consulta). Solo
    devuelve el estado: nunca preguntas ni datos del examen."""
    _validar_pase_alumno(pase, sesion_id)
    filas = sb.table("sesiones_examen").select("estado").eq("id", sesion_id).execute().data
    if not filas:
        raise HTTPException(404, "Sesión no encontrada")
    return {"estado": filas[0]["estado"]}

@router.get("/{sesion_id}/asistencia")
def ver_asistencia(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Consola docente: quién ha marcado asistencia hasta ahora, en vivo.
    El total de habilitados es el tamaño del conjunto de la sesión."""
    conjunto_id = _conjunto_de_la_sesion(sesion_id)

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
    """Cierra la encuesta y el examen PARTE PARA TODOS en este momento:
    termina a la misma hora para todos (ahora + minutos del paquete). Se
    programa un único cierre de la sesión para esa hora."""
    sesion = sb.table("sesiones_examen").select("estado, paquete_elegido, termina_at").eq("id", sesion_id).execute().data
    if not sesion:
        raise HTTPException(404, "Sesión no encontrada")
    if sesion[0]["estado"] in ("en_curso", "finalizada"):
        # Ya cerrada antes: no se reinicia el horario del examen.
        return {"paquete_elegido": sesion[0]["paquete_elegido"], "conteo": ver_encuesta(sesion_id)}
    _conjunto_de_la_sesion(sesion_id)

    conteo = ver_encuesta(sesion_id)
    ganador = max(conteo, key=conteo.get)
    ahora = datetime.now(timezone.utc)
    termina = ahora + timedelta(minutes=minutos_del_paquete(ganador))
    sb.table("sesiones_examen").update({
        "estado": "en_curso", "paquete_elegido": ganador,
        "iniciada_at": ahora.isoformat(), "termina_at": termina.isoformat(),
    }).eq("id", sesion_id).execute()
    programar_cierre_sesion(sesion_id, termina.isoformat())
    return {"paquete_elegido": ganador, "conteo": conteo, "termina_at": termina.isoformat()}


# ---------------- TABLA DE RESULTADOS (detalle pregunta por pregunta) ----------------
@router.get("/{sesion_id}/resultados")
def resultados_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """
    Por cada alumno que rindió: su nota/puntaje, y el detalle de cada
    pregunta que le tocó (enunciado, su respuesta, si fue correcta, la
    alternativa correcta y la explicación). Las preguntas de casos
    clinicos viven en una tabla de respuestas aparte (respuestas_caso,
    con su propia foreign key estricta a caso_preguntas) porque una
    sola FK no puede apuntar a dos tablas distintas segun el origen de
    la pregunta -se leen por separado y se juntan aqui en un solo
    detalle, ya ordenadas para el profesor.
    """
    instancias = sb.table("examen_instancia").select(
        "id, alumno_id, paquete, puntaje_total, porcentaje, nota, iniciado_at, finalizado_at, salidas_detectadas, alumnos(nombre, rut)"
    ).eq("sesion_id", sesion_id).execute().data

    resultados = []
    for inst in instancias:
        respuestas_banco = sb.table("respuestas").select(
            "pregunta_id, opcion_elegida, correcta, banco_preguntas(pregunta, opciones, correcta, explicacion, region, complejidad)"
        ).eq("examen_instancia_id", inst["id"]).execute().data

        respuestas_caso = sb.table("respuestas_caso").select(
            "pregunta_id, opcion_elegida, correcta, caso_preguntas(pregunta, opciones, correcta, explicacion_generada)"
        ).eq("examen_instancia_id", inst["id"]).execute().data

        detalle = []
        for r in respuestas_banco:
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
        for r in respuestas_caso:
            cp = r["caso_preguntas"]
            detalle.append({
                "pregunta": cp["pregunta"],
                "region": None,
                "complejidad": "caso_clinico",
                "opciones": cp["opciones"],
                "respuesta_alumno": cp["opciones"][r["opcion_elegida"]],
                "respuesta_correcta": cp["opciones"][cp["correcta"]],
                "correcta": r["correcta"],
                "explicacion": cp["explicacion_generada"],
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
