-- ============================================================
-- EXAMEN MUSCULOESQUELÉTICO — schema.sql (fuente de verdad)
-- Supabase nuevo, proyecto aparte de ICA
-- Idempotente: seguro de correr en cada arranque (db_init.py)
-- ============================================================

create extension if not exists "pgcrypto";

-- ---------- TIPOS ENUM (creación idempotente vía DO block) ----------
DO $$ BEGIN
  CREATE TYPE rol_enum AS ENUM ('admin', 'interrogador');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE complejidad_enum AS ENUM ('basica', 'intermedia', 'compleja');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE material_tipo AS ENUM ('ppt', 'resumen');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE sesion_estado AS ENUM ('creada', 'asistencia', 'encuesta', 'en_curso', 'finalizada');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE paquete_enum AS ENUM ('agil', 'estandar', 'exigente');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE media_tipo_enum AS ENUM ('foto', 'video');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE sesion_vivo_estado AS ENUM ('esperando', 'votando', 'discusion', 'cerrada');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------- INTERROGADORES (auth propio, login por RUT, con rol) ----------
create table if not exists interrogadores (
  id uuid primary key default gen_random_uuid(),
  rut text unique not null,
  password_hash text not null,
  nombre text not null,
  rol rol_enum not null default 'interrogador',
  activo boolean default true,
  created_at timestamptz default now()
);

-- ---------- ALUMNOS ----------
create table if not exists alumnos (
  id uuid primary key default gen_random_uuid(),
  rut text unique not null,
  nombre text not null,
  created_at timestamptz default now()
);

-- ---------- BANCO DE PREGUNTAS ----------
create table if not exists banco_preguntas (
  id uuid primary key default gen_random_uuid(),
  region text not null,
  complejidad complejidad_enum not null,
  pregunta text not null,
  opciones jsonb not null,
  correcta int not null,
  explicacion text,
  activo boolean default true,
  creado_por uuid references interrogadores(id),
  created_at timestamptz default now()
);

alter table banco_preguntas add column if not exists media_url text;
alter table banco_preguntas add column if not exists media_tipo media_tipo_enum;

create index if not exists idx_preguntas_region_complejidad on banco_preguntas(region, complejidad) where activo;

-- ---------- MATERIALES (PPT / resumen, por región) ----------
create table if not exists materiales (
  id uuid primary key default gen_random_uuid(),
  region text not null,
  tipo material_tipo not null,
  titulo text not null,
  storage_path text not null,
  subido_por uuid references interrogadores(id),
  created_at timestamptz default now()
);

create index if not exists idx_materiales_region on materiales(region);

-- ---------- TRACKING DE MATERIALES (visitas + descargas por alumno) ----------
create table if not exists visitas_materiales (
  id uuid primary key default gen_random_uuid(),
  alumno_id uuid references alumnos(id) not null,
  visitado_at timestamptz default now()
);

create table if not exists descargas_materiales (
  id uuid primary key default gen_random_uuid(),
  alumno_id uuid references alumnos(id) not null,
  material_id uuid references materiales(id) not null,
  descargado_at timestamptz default now()
);

create index if not exists idx_visitas_alumno on visitas_materiales(alumno_id);
create index if not exists idx_descargas_material on descargas_materiales(material_id);
create index if not exists idx_descargas_alumno on descargas_materiales(alumno_id);

-- ---------- SESIONES DE EXAMEN ----------
create table if not exists sesiones_examen (
  id uuid primary key default gen_random_uuid(),
  nombre text not null,
  fecha date not null,
  estado sesion_estado not null default 'creada',
  paquete_elegido paquete_enum,
  creado_por uuid references interrogadores(id),
  created_at timestamptz default now()
);

create table if not exists sesion_alumnos (
  sesion_id uuid references sesiones_examen(id) on delete cascade,
  alumno_id uuid references alumnos(id) on delete cascade,
  primary key (sesion_id, alumno_id)
);

create table if not exists asistencia (
  sesion_id uuid references sesiones_examen(id) on delete cascade,
  alumno_id uuid references alumnos(id) on delete cascade,
  marcado_at timestamptz default now(),
  primary key (sesion_id, alumno_id)
);

create table if not exists encuesta_votos (
  sesion_id uuid references sesiones_examen(id) on delete cascade,
  alumno_id uuid references alumnos(id) on delete cascade,
  paquete paquete_enum not null,
  votado_at timestamptz default now(),
  primary key (sesion_id, alumno_id)
);

-- ---------- INSTANCIA DE EXAMEN ----------
create table if not exists examen_instancia (
  id uuid primary key default gen_random_uuid(),
  sesion_id uuid references sesiones_examen(id) not null,
  alumno_id uuid references alumnos(id) not null,
  paquete paquete_enum not null,
  pregunta_ids uuid[] not null,
  puntos_por_pregunta jsonb not null,
  iniciado_at timestamptz,
  finalizado_at timestamptz,
  puntaje_total numeric,
  porcentaje numeric,
  nota numeric,
  unique (sesion_id, alumno_id)
);

alter table examen_instancia add column if not exists orden_opciones jsonb;
alter table examen_instancia add column if not exists salidas_detectadas int default 0;

create table if not exists intentos_salida (
  id uuid primary key default gen_random_uuid(),
  examen_instancia_id uuid references examen_instancia(id) not null,
  detectado_at timestamptz default now()
);

create index if not exists idx_intentos_salida_instancia on intentos_salida(examen_instancia_id);

create table if not exists respuestas (
  id uuid primary key default gen_random_uuid(),
  examen_instancia_id uuid references examen_instancia(id) not null,
  pregunta_id uuid references banco_preguntas(id) not null,
  opcion_elegida int not null,
  correcta boolean not null,
  respondido_at timestamptz default now(),
  unique (examen_instancia_id, pregunta_id)
);

create index if not exists idx_respuestas_instancia on respuestas(examen_instancia_id);
create index if not exists idx_instancia_sesion on examen_instancia(sesion_id);

-- ============================================================
-- CASOS CLÍNICOS / PRESENTACIÓN DINÁMICA EN VIVO (nuevo)
-- ============================================================

-- ---------- CASOS CLÍNICOS ----------
create table if not exists casos_clinicos (
  id uuid primary key default gen_random_uuid(),
  region text not null,
  titulo text not null,
  vineta_clinica text not null,
  media_url text,
  media_tipo media_tipo_enum,
  creado_por uuid references interrogadores(id),
  created_at timestamptz default now()
);

create index if not exists idx_casos_region on casos_clinicos(region);

-- ---------- PUENTE: preguntas del banco, ordenadas dentro de un caso ----------
create table if not exists caso_preguntas (
  id uuid primary key default gen_random_uuid(),
  caso_id uuid references casos_clinicos(id) on delete cascade not null,
  pregunta_id uuid references banco_preguntas(id) not null,
  orden int not null,
  unique (caso_id, orden),
  unique (caso_id, pregunta_id)
);

create index if not exists idx_caso_preguntas_caso on caso_preguntas(caso_id);

-- ---------- PRESENTACIONES (reemplazo del PPT: set de casos, en orden, reutilizable) ----------
create table if not exists presentaciones (
  id uuid primary key default gen_random_uuid(),
  titulo text not null,
  region text,
  creado_por uuid references interrogadores(id),
  created_at timestamptz default now()
);

-- ---------- PUENTE: casos dentro de una presentación, ordenados ----------
create table if not exists presentacion_casos (
  id uuid primary key default gen_random_uuid(),
  presentacion_id uuid references presentaciones(id) on delete cascade not null,
  caso_id uuid references casos_clinicos(id) not null,
  orden int not null,
  unique (presentacion_id, orden),
  unique (presentacion_id, caso_id)
);

create index if not exists idx_presentacion_casos_presentacion on presentacion_casos(presentacion_id);

-- ---------- SESIÓN EN VIVO (una clase real, corriendo sobre una presentación) ----------
create table if not exists sesiones_vivo (
  id uuid primary key default gen_random_uuid(),
  presentacion_id uuid references presentaciones(id) not null,
  codigo_acceso text unique not null,
  estado sesion_vivo_estado not null default 'esperando',
  caso_actual_orden int not null default 1,
  pregunta_actual_orden int not null default 1,
  creado_por uuid references interrogadores(id),
  created_at timestamptz default now()
);

create index if not exists idx_sesiones_vivo_codigo on sesiones_vivo(codigo_acceso);

-- ---------- VOTOS EN VIVO (nombre visible para el profesor, un voto por alumno por pregunta) ----------
create table if not exists votos_vivo (
  id uuid primary key default gen_random_uuid(),
  sesion_id uuid references sesiones_vivo(id) on delete cascade not null,
  pregunta_id uuid references banco_preguntas(id) not null,
  alumno_id uuid references alumnos(id) not null,
  opcion int not null,
  created_at timestamptz default now(),
  unique (sesion_id, pregunta_id, alumno_id)
);

create index if not exists idx_votos_vivo_sesion_pregunta on votos_vivo(sesion_id, pregunta_id);

-- ---------- VISTAS DE ANÁLISIS DOCENTE ----------
create or replace view analisis_preguntas as
select
  ei.sesion_id, r.pregunta_id, bp.pregunta, bp.region, bp.complejidad,
  count(*) as veces_respondida,
  sum(case when r.correcta then 1 else 0 end) as veces_correcta,
  round(100.0 * sum(case when r.correcta then 1 else 0 end) / count(*), 1) as pct_acierto
from respuestas r
join examen_instancia ei on ei.id = r.examen_instancia_id
join banco_preguntas bp on bp.id = r.pregunta_id
group by ei.sesion_id, r.pregunta_id, bp.pregunta, bp.region, bp.complejidad;

create or replace view analisis_sesion as
select
  sesion_id, count(*) as n_alumnos,
  round(avg(nota), 2) as nota_promedio,
  round(100.0 * sum(case when nota >= 4.0 then 1 else 0 end) / count(*), 1) as pct_aprobacion,
  min(nota) as nota_minima, max(nota) as nota_maxima
from examen_instancia
where finalizado_at is not null
group by sesion_id;

create or replace view analisis_complejidad as
select
  ei.sesion_id, bp.complejidad,
  count(*) as veces_respondida,
  sum(case when r.correcta then 1 else 0 end) as veces_correcta,
  round(100.0 * sum(case when r.correcta then 1 else 0 end) / count(*), 1) as pct_acierto
from respuestas r
join examen_instancia ei on ei.id = r.examen_instancia_id
join banco_preguntas bp on bp.id = r.pregunta_id
group by ei.sesion_id, bp.complejidad;

-- ---------- VISTAS DE ANÁLISIS DE MATERIALES ----------
create or replace view analisis_descargas_material as
select
  material_id,
  count(*) as total_descargas,
  count(distinct alumno_id) as alumnos_distintos
from descargas_materiales
group by material_id;

create or replace view analisis_actividad_alumno as
select
  a.id as alumno_id,
  a.nombre,
  a.rut,
  (select count(*) from visitas_materiales v where v.alumno_id = a.id) as total_visitas,
  (select count(*) from descargas_materiales d where d.alumno_id = a.id) as total_descargas
from alumnos a;
