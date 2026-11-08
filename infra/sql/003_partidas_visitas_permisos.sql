-- Permisos de la identidad de la API sobre las tablas de la migración 003_partidas_visitas.sql.
-- Se aplica una vez, como administrador de Entra del servidor, después de `sql_admin.py migrar`:
--   python backend/scripts/sql_admin.py sql infra/sql/003_partidas_visitas_permisos.sql

GRANT SELECT, INSERT, UPDATE ON dbo.partidas TO [meaculpa-api-id];

GRANT SELECT, INSERT, DELETE ON dbo.visitas TO [meaculpa-api-id];
