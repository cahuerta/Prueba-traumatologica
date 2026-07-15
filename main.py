"""
Backend — Examen Musculoesquelético
Módulo de autenticación propio (independiente de ICA y de Supabase Auth).

Variables de entorno esperadas (Render):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
  JWT_SECRET          (secreto propio de este proyecto, generado por ti)
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGO = "HS256"

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = FastAPI(title="Examen Musculoesquelético API — Auth")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app|http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- HELPERS DE PASSWORD Y JWT ----------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())

def crear_token(interrogador: dict) -> str:
    payload = {
        "sub": interrogador["id"],
        "email": interrogador["email"],
        "rol": interrogador["rol"],
        # sin expiración por ahora — se define más adelante
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


# ---------------- DEPENDENCIAS DE AUTH ----------------
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
    email: EmailStr
    password: str

class CrearInterrogadorIn(BaseModel):
    email: EmailStr
    password: str
    nombre: str
    rol: str = "interrogador"  # "admin" | "interrogador"


# ---------------- ENDPOINTS ----------------
@app.post("/auth/login")
def login(body: LoginIn):
    res = sb.table("interrogadores").select("*").eq("email", body.email).eq("activo", True).execute()
    if not res.data:
        raise HTTPException(401, "Email o contraseña incorrectos")

    interrogador = res.data[0]
    if not verify_password(body.password, interrogador["password_hash"]):
        raise HTTPException(401, "Email o contraseña incorrectos")

    token = crear_token(interrogador)
    return {
        "token": token,
        "nombre": interrogador["nombre"],
        "rol": interrogador["rol"],
    }

@app.post("/auth/interrogadores")
def crear_interrogador(body: CrearInterrogadorIn, admin: dict = Depends(requiere_admin)):
    """Solo el admin (Cristóbal) puede crear cuentas nuevas."""
    if body.rol not in ("admin", "interrogador"):
        raise HTTPException(400, "Rol inválido")

    existe = sb.table("interrogadores").select("id").eq("email", body.email).execute()
    if existe.data:
        raise HTTPException(409, "Ya existe una cuenta con ese email")

    res = sb.table("interrogadores").insert({
        "email": body.email,
        "password_hash": hash_password(body.password),
        "nombre": body.nombre,
        "rol": body.rol,
    }).execute()

    nuevo = res.data[0]
    return {"id": nuevo["id"], "email": nuevo["email"], "nombre": nuevo["nombre"], "rol": nuevo["rol"]}

@app.get("/auth/me")
def me(interrogador: dict = Depends(get_current_interrogador)):
    return interrogador
