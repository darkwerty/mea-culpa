-- Permiso para quitar del ranking una partida de ajedrez que se reabre deshaciendo jugadas (RankingStore.forget):
-- las partidas «con ayuda» no cuentan. Se aplica una vez, como administrador de Entra del servidor:
--   python backend/scripts/sql_admin.py sql infra/sql/004_ranking_borrar_permisos.sql

GRANT DELETE ON dbo.ranking_partidas TO [meaculpa-api-id];
