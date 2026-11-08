-- 002: rankings públicos de las dos demos (¿Dónde está esta foto? y Mea culpa).
-- Una fila por partida terminada, escrita por el servidor con el resultado que calcula él mismo, y una fila por
-- jugador con el nombre que da Google y si quiere salir en el ranking (casilla del juego, marcada por defecto).

CREATE TABLE ranking_jugadores (
    owner NVARCHAR(64) NOT NULL PRIMARY KEY, -- sha256 del sub de Google (la misma clave que en usuarios y Blob)
    nombre NVARCHAR(60) NULL, -- nombre de pila de Google, sin correo ni foto
    publico BIT NOT NULL DEFAULT 1,
    actualizado FLOAT NOT NULL
);

CREATE TABLE ranking_partidas (
    id NVARCHAR(40) NOT NULL PRIMARY KEY, -- demo:id de la partida (una fila por partida)
    ts FLOAT NOT NULL,
    demo NVARCHAR(20) NOT NULL, -- geo | meaculpa
    owner NVARCHAR(64) NOT NULL,
    rival NVARCHAR(60) NOT NULL, -- id del modelo o del algoritmo (stockfish-5, minimax-3...)
    rival_nombre NVARCHAR(120) NOT NULL,
    resultado NVARCHAR(10) NOT NULL, -- del humano: victoria | tablas | derrota
    puntos INT NULL, -- geo: puntos del humano en la partida
    puntos_ia INT NULL -- geo: puntos de la IA
);

CREATE INDEX ix_ranking_partidas_demo ON ranking_partidas (demo);

CREATE INDEX ix_ranking_partidas_owner ON ranking_partidas (owner);
