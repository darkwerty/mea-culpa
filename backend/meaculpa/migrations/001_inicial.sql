-- 001: usuarios, eventos de uso, mensajes de contacto, límites del formulario y configuración.
-- SQL común a Azure SQL (T-SQL) y SQLite: tipos que entienden los dos, claves generadas en la aplicación
-- (uuid en hex) y fechas como segundos Unix (FLOAT) más el día UTC en texto (YYYY-MM-DD) para agrupar.
-- Una sentencia por bloque terminado en ';' a final de línea (así lo parte meaculpa/db.py).

CREATE TABLE usuarios (
    owner NVARCHAR(64) NOT NULL PRIMARY KEY, -- sha256 del sub de Google (la misma clave que en Blob)
    email NVARCHAR(320) NULL,
    nombre NVARCHAR(60) NULL,
    primera_visita FLOAT NOT NULL,
    ultima_visita FLOAT NOT NULL,
    bloqueado BIT NOT NULL DEFAULT 0
);

CREATE TABLE eventos (
    id NVARCHAR(32) NOT NULL PRIMARY KEY,
    ts FLOAT NOT NULL,
    dia NVARCHAR(10) NOT NULL,
    owner NVARCHAR(64) NULL,
    demo NVARCHAR(20) NOT NULL, -- meaculpa | geo
    tipo NVARCHAR(30) NOT NULL, -- partida, jugada, jugada_ia, analisis, analisis_pgn, geo_partida, geo_ronda
    ref NVARCHAR(40) NULL, -- id de la partida
    modelo NVARCHAR(60) NULL,
    tokens_entrada INT NOT NULL DEFAULT 0,
    tokens_salida INT NOT NULL DEFAULT 0,
    coste_usd FLOAT NOT NULL DEFAULT 0
);

CREATE INDEX ix_eventos_dia ON eventos (dia);

CREATE INDEX ix_eventos_owner ON eventos (owner);

CREATE INDEX ix_eventos_ref ON eventos (ref);

CREATE TABLE mensajes (
    id NVARCHAR(32) NOT NULL PRIMARY KEY,
    ts FLOAT NOT NULL,
    nombre NVARCHAR(100) NOT NULL,
    email NVARCHAR(320) NOT NULL,
    mensaje NVARCHAR(4000) NOT NULL,
    leido BIT NOT NULL DEFAULT 0
);

CREATE INDEX ix_mensajes_ts ON mensajes (ts);

-- Contador del formulario por día: la clave es un hash del día y la IP; las filas de días pasados se borran.
CREATE TABLE limites_contacto (
    clave NVARCHAR(64) NOT NULL PRIMARY KEY,
    dia NVARCHAR(10) NOT NULL,
    cuenta INT NOT NULL DEFAULT 0
);

-- Controles del panel /admin: demo.<id>, modelo.<id> (valor "0" o "1") y cuota.<tipo> (entero).
CREATE TABLE config (
    clave NVARCHAR(80) NOT NULL PRIMARY KEY,
    valor NVARCHAR(400) NOT NULL,
    actualizado FLOAT NOT NULL
);
