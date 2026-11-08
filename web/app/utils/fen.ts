/** Utilidades mínimas de FEN para pintar el tablero (la validación de verdad la hace la API). */

export type PieceMap = Record<string, string> // 'e4' -> 'P' | 'n' | ...

const FILES = 'abcdefgh'

export function parseFen(fen: string): PieceMap {
  const map: PieceMap = {}
  const rows = (fen.split(' ')[0] || '').split('/')
  rows.forEach((row, i) => {
    const rank = 8 - i
    let file = 0
    for (const ch of row) {
      if (/\d/.test(ch)) file += Number(ch)
      else {
        map[`${FILES[file]}${rank}`] = ch
        file++
      }
    }
  })
  return map
}

/** Aplica una jugada UCI al mapa de piezas, solo para mostrarla al instante mientras responde el rival. */
export function applyUci(map: PieceMap, uci: string): PieceMap {
  const next = { ...map }
  const from = uci.slice(0, 2)
  const to = uci.slice(2, 4)
  const promo = uci[4]
  const piece = next[from]
  if (!piece) return next
  delete next[from]
  // Enroque: mover también la torre
  if (piece.toLowerCase() === 'k' && Math.abs(FILES.indexOf(from[0]!) - FILES.indexOf(to[0]!)) === 2) {
    const rank = from[1]
    const kingside = to[0] === 'g'
    const rookFrom = `${kingside ? 'h' : 'a'}${rank}`
    const rookTo = `${kingside ? 'f' : 'd'}${rank}`
    if (next[rookFrom]) {
      next[rookTo] = next[rookFrom]!
      delete next[rookFrom]
    }
  }
  // Captura al paso
  if (piece.toLowerCase() === 'p' && from[0] !== to[0] && !next[to]) {
    delete next[`${to[0]}${from[1]}`]
  }
  next[to] = promo ? (piece === 'P' ? promo.toUpperCase() : promo.toLowerCase()) : piece
  return next
}

export const GLYPHS: Record<string, string> = {
  k: '♚',
  q: '♛',
  r: '♜',
  b: '♝',
  n: '♞',
  p: '♟︎',
}
