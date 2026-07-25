"""
routers/auth.py
Auth propio del proyecto — independiente de ICA y de Supabase Auth.
Login por RUT (no email).

Variables de entorno esperadas (Render):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
  JWT_SECRET          (secreto propio de este proyecto, generado por ti)
"""

import os

import bcrypt
import httpx
import jwt
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from supabase import create_client, Client, ClientOptions

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGO = "HS256"

# Se fuerza HTTP/1.1 en vez de HTTP/2: bajo carga concurrente alta (muchas
# peticiones simultaneas, ej. varios alumnos a la vez), el cliente HTTP/2
# compartido de httpx puede corromper su stream multiplexado y devolver
# "Exception in ASGI application" en cascada. HTTP/1.1 abre conexiones
# separadas en vez de compartir un solo stream, evitando ese problema.
_http_client = httpx.Client(http2=False)
sb: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
    options=ClientOptions(httpx_client=_http_client),
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------- HELPERS DE PASSWORD Y JWT ----------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())

def crear_token(interrogador: dict) -> str:
    payload = {
        "sub": interrogador["id"],
        "rut": interrogador["rut"],
        "rol": interrogador["rol"],
        # sin expiración por ahora — se define más adelante
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


# ---------------- DEPENDENCIAS DE AUTH (las usan los demás módulos) ----------------
def get_current_interrogador(authorization: str = Header(...)) -> dict:
    token = authorization.replace("Bearer ", "")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido o expirado")
    return payload

def requiere_admin(interrogador: dict = Depends(get_current_interrogador)) -> dict:
    if interrogador["rol"] != "admin":
        raise HTTPException(403, "Solo el administrador puede hacer esto")
    return interrogador


# ---------------- MODELOS ----------------
class LoginIn(BaseModel):
    rut: str
    password: str

class CrearInterrogadorIn(BaseModel):
    rut: str
    password: str
    nombre: str
    rol: str = "interrogador"  # "admin" | "interrogador"


# ---------------- ENDPOINTS ----------------
@router.post("/login")
def login(body: LoginIn):
    res = sb.table("interrogadores").select("*").eq("rut", body.rut).eq("activo", True).execute()
    if not res.data:
        raise HTTPException(401, "RUT o contraseña incorrectos")

    interrogador = res.data[0]
    if not verify_password(body.password, interrogador["password_hash"]):
        raise HTTPException(401, "RUT o contraseña incorrectos")

    token = crear_token(interrogador)
    return {
        "token": token,
        "nombre": interrogador["nombre"],
        "rol": interrogador["rol"],
    }

@router.post("/interrogadores")
def crear_interrogador(body: CrearInterrogadorIn, admin: dict = Depends(requiere_admin)):
    """Solo el admin (Cristóbal) puede crear cuentas nuevas."""
    if body.rol not in ("admin", "interrogador"):
        raise HTTPException(400, "Rol inválido")

    existe = sb.table("interrogadores").select("id").eq("rut", body.rut).execute()
    if existe.data:
        raise HTTPException(409, "Ya existe una cuenta con ese RUT")

    res = sb.table("interrogadores").insert({
        "rut": body.rut,
        "password_hash": hash_password(body.password),
        "nombre": body.nombre,
        "rol": body.rol,
    }).execute()

    nuevo = res.data[0]
    return {"id": nuevo["id"], "rut": nuevo["rut"], "nombre": nuevo["nombre"], "rol": nuevo["rol"]}

@router.get("/me")
def me(interrogador: dict = Depends(get_current_interrogador)):
    return interrogador
  
