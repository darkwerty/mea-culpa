-- 003: registro completo de las partidas de las dos demos y estadísticas de visitas de la web (sin cookies).
--
-- partidas: una fila por partida, empezada o terminada, que se actualiza en cada jugada o ronda. La clave es
-- demo:id, la misma que ranking_partidas (002), así que se pueden cruzar. ranking_partidas solo tiene las
-- terminadas, con lo que necesita el ranking público; esta tabla guarda el detalle (PGN, rondas, duración)
-- para el panel /admin. Las partidas siguen también en Blob, que es de donde las lee la demo.
-- NVARCHAR(MAX) se convierte en TEXT en SQLite (meaculpa/db.py).

CREATE TABLE partidas (
    id NVARCHAR(40) NOT NULL PRIMARY KEY, -- demo:id (igual que ranking_partidas)
    demo NVARCHAR(20) NOT NULL, -- meaculpa | geo
    owner NVARCHAR(64) NOT NULL, -- sha256 del sub de Google (usuarios.owner)
    rival NVARCHAR(60) NOT NULL, -- id del modelo o del algoritmo (stockfish-5, minimax-3...)
    rival_nombre NVARCHAR(120) NOT NULL,
    estado NVARCHAR(12) NOT NULL, -- en_curso | terminada
    resultado NVARCHAR(10) NULL, -- del humano: victoria | tablas | derrota
    motivo_fin NVARCHAR(40) NULL, -- ajedrez: jaque mate, tablas por repetición, abandono...
    color NVARCHAR(5) NULL, -- ajedrez: color del humano (white | black)
    jugadas INT NOT NULL DEFAULT 0, -- ajedrez: medias jugadas; fotos: rondas respondidas
    pgn NVARCHAR(MAX) NULL, -- ajedrez
    rondas NVARCHAR(MAX) NULL, -- fotos: JSON con los puntos y la distancia de cada ronda
    puntos INT NULL, -- fotos: total del humano
    puntos_ia INT NULL, -- fotos: total de la IA
    inicio FLOAT NOT NULL,
    actualizado FLOAT NOT NULL,
    fin FLOAT NULL,
    duracion_s FLOAT NULL, -- fin (o última jugada) menos inicio
    dia NVARCHAR(10) NOT NULL -- día UTC de inicio
);

CREATE INDEX ix_partidas_dia ON partidas (dia);

CREATE INDEX ix_partidas_owner ON partidas (owner);

-- visitas: una fila por página vista. Sin IP, sin cookies y sin identificadores persistentes: `visitante` es un
-- hash de IP y navegador con una sal aleatoria que solo vive en memoria y cambia cada día (no se guarda), así que
-- sirve para contar visitantes únicos de un día y no permite seguir a nadie de un día a otro.
CREATE TABLE visitas (
    id NVARCHAR(32) NOT NULL PRIMARY KEY,
    ts FLOAT NOT NULL,
    dia NVARCHAR(10) NOT NULL,
    ruta NVARCHAR(200) NOT NULL,
    referer NVARCHAR(120) NULL, -- solo el dominio de procedencia, y solo si es externo
    pais NVARCHAR(2) NULL, -- código ISO, por cabecera o por la zona horaria del navegador
    dispositivo NVARCHAR(12) NOT NULL, -- movil | tableta | ordenador
    visitante NVARCHAR(16) NULL
);

CREATE INDEX ix_visitas_dia ON visitas (dia);
