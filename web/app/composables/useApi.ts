export interface GameState {
  id: string
  fen: string
  turn: 'white' | 'black'
  user_color: 'white' | 'black'
  opponent: Record<string, any>
  moves_uci: string[]
  moves_san: string[]
  legal_moves: string[]
  in_check: boolean
  status: 'en_curso' | 'terminada'
  result: string
  resultado_usuario: 'victoria' | 'derrota' | 'tablas' | null
  motivo_fin: string | null
  last_move: string | null
  last_opponent_meta: Record<string, any> | null
  has_diagnosis: boolean
  pgn: string
}

export function useApi() {
  const base = useRuntimeConfig().public.apiBase as string
  const { session, signOut } = useAuth()

  const call = <T>(path: string, opts: Record<string, any> = {}) => {
    const headers: Record<string, string> = { ...(opts.headers ?? {}) }
    if (session.value) headers.Authorization = `Bearer ${session.value.token}`
    return $fetch<T>(path, { baseURL: base, ...opts, headers }).catch((err: any) => {
      if (err?.status === 401 || err?.statusCode === 401) {
        signOut('Tu sesión ha caducado: vuelve a entrar con Google.')
      }
      const detail = err?.data?.detail
      throw new Error(typeof detail === 'string' ? detail : err?.message || 'Error de red')
    })
  }

  return {
    health: () => call<any>('/api/meaculpa/health'),
    opponents: () => call<any>('/api/meaculpa/opponents'),
    newGame: (body: any) => call<GameState>('/api/meaculpa/games', { method: 'POST', body }),
    move: (id: string, uci: string) => call<GameState>(`/api/meaculpa/games/${id}/move`, { method: 'POST', body: { uci } }),
    resign: (id: string) => call<GameState>(`/api/meaculpa/games/${id}/resign`, { method: 'POST' }),
    game: (id: string) => call<GameState>(`/api/meaculpa/games/${id}`),
    getAnalysis: (id: string) => call<any>(`/api/meaculpa/games/${id}/analysis`),
    analyse: (id: string) => call<any>(`/api/meaculpa/games/${id}/analysis`, { method: 'POST' }),
    games: () => call<any[]>('/api/meaculpa/games'),
    metrics: () => call<any>('/api/meaculpa/metrics/llm'),
  }
}
