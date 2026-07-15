-- ============================================================
-- EXAMEN MUSCULOESQUELÉTICO — Supabase nuevo, proyecto aparte de ICA
-- ============================================================

create extension if not exists "pgcrypto";

-- ---------- INTERROGADORES (auth propio, con rol) ----------
create type rol_enum as enum ('admin', 'interrogador');

create table if not exists interrogadores (
  id uuid primary key default gen_random_uuid(),
  email text unique not null,
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
create type complejidad_enum as enum ('basica', 'intermedia', 'compleja');

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

create index if not exists idx_preguntas_region_complejidad on banco_preguntas(region, complejidad) where activo;

-- ---------- MATERIALES (PPT / resumen, por región) ----------
create type material_tipo as enum ('ppt', 'resumen');

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

-- ---------- SESIONES DE EXAMEN ----------
create type sesion_estado as enum ('creada', 'asistencia', 'encuesta', 'en_curso', 'finalizada');
create type paquete_enum as enum ('agil', 'estandar', 'exigente');

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
