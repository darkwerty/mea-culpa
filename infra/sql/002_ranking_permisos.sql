-- Permisos de la identidad de la API sobre las tablas del ranking (migración 002_ranking.sql).
-- Se aplica una vez, como administrador de Entra del servidor, después de `sql_admin.py migrar`:
--   python backend/scripts/sql_admin.py sql infra/sql/002_ranking_permisos.sql

GRANT SELECT, INSERT, UPDATE ON dbo.ranking_jugadores TO [meaculpa-api-id];

GRANT SELECT, INSERT ON dbo.ranking_partidas TO [meaculpa-api-id];
