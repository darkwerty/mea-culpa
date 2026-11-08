-- Usuario de base de datos para la identidad administrada de la API (meaculpa-api-id), con permisos mínimos:
-- solo las tablas y operaciones que usa meaculpa/datos.py. Ni DDL ni acceso a otras tablas.
-- Se aplica una vez, como administrador de Entra del servidor, DESPUÉS de las migraciones:
--   python backend/scripts/sql_admin.py sql infra/sql/identidad_api.sql

CREATE USER [meaculpa-api-id] FROM EXTERNAL PROVIDER;

GRANT SELECT, INSERT, UPDATE ON dbo.usuarios TO [meaculpa-api-id];

GRANT SELECT, INSERT ON dbo.eventos TO [meaculpa-api-id];

GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.mensajes TO [meaculpa-api-id];

GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.limites_contacto TO [meaculpa-api-id];

GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.config TO [meaculpa-api-id];

GRANT SELECT ON dbo.schema_migraciones TO [meaculpa-api-id];

-- Rankings (migración 002; en una base de datos ya creada, infra/sql/002_ranking_permisos.sql).
GRANT SELECT, INSERT, UPDATE ON dbo.ranking_jugadores TO [meaculpa-api-id];

GRANT SELECT, INSERT ON dbo.ranking_partidas TO [meaculpa-api-id];

-- 003: partidas completas y visitas de la web.
GRANT SELECT, INSERT, UPDATE ON dbo.partidas TO [meaculpa-api-id];

GRANT SELECT, INSERT, DELETE ON dbo.visitas TO [meaculpa-api-id];
