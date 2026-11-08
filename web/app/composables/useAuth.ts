/**
 * Login con Google Identity Services (flujo de ID token, sin client secret).
 *
 * El ID token se manda a la API en `Authorization: Bearer`; la API verifica firma, audiencia, emisor,
 * caducidad y correo verificado. Aquí solo se decodifica para mostrar el nombre y saber cuándo caduca.
 * Se guarda en sessionStorage (se pierde al cerrar la pestaña) para sobrevivir a una recarga.
 *
 * Sin NUXT_PUBLIC_GOOGLE_CLIENT_ID (desarrollo local con backend/dev_server.py) no hay login: se manda
 * un token fijo que solo acepta el servidor de desarrollo.
 */
interface Session {
  token: string
  name: string | null
  picture: string | null
  exp: number
}

const STORAGE_KEY = 'meaculpa.session'
const GSI_SRC = 'https://accounts.google.com/gsi/client'

function decodePayload(token: string): Record<string, any> | null {
  try {
    const part = token.split('.')[1]!.replace(/-/g, '+').replace(/_/g, '/')
    const json = decodeURIComponent(
      atob(part)
        .split('')
        .map((c) => '%' + c.charCodeAt(0).toString(16).padStart(2, '0'))
        .join(''),
    )
    return JSON.parse(json)
  } catch {
    return null
  }
}

function readStored(): Session | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    const s = raw ? (JSON.parse(raw) as Session) : null
    return s && s.exp * 1000 > Date.now() + 30_000 ? s : null
  } catch {
    return null
  }
}

let gsiLoading: Promise<void> | null = null
function loadGsi(): Promise<void> {
  if ((window as any).google?.accounts?.id) return Promise.resolve()
  gsiLoading ??= new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = GSI_SRC
    s.async = true
    s.onload = () => resolve()
    s.onerror = () => {
      gsiLoading = null
      reject(new Error('No se pudo cargar el login de Google'))
    }
    document.head.appendChild(s)
  })
  return gsiLoading
}

export function useAuth() {
  const clientId = useRuntimeConfig().public.googleClientId as string
  const localMode = !clientId
  const session = useState<Session | null>('session', () => null)
  const authError = useState<string | null>('authError', () => null)

  function restore() {
    if (localMode) {
      session.value = { token: 'desarrollo-local', name: 'modo local', picture: null, exp: Infinity }
      return
    }
    session.value = readStored()
  }

  function onCredential(resp: { credential?: string }) {
    const payload = resp.credential ? decodePayload(resp.credential) : null
    if (!resp.credential || !payload?.exp) {
      authError.value = 'Google no devolvió una sesión válida'
      return
    }
    authError.value = null
    session.value = {
      token: resp.credential,
      name: payload.given_name ?? payload.name ?? null,
      picture: payload.picture ?? null,
      exp: payload.exp,
    }
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session.value))
    } catch {
      /* sin almacenamiento: la sesión dura lo que la página */
    }
  }

  async function renderButton(el: HTMLElement) {
    if (localMode) return
    try {
      await loadGsi()
      const gid = (window as any).google.accounts.id
      gid.initialize({
        client_id: clientId,
        callback: onCredential,
        auto_select: false,
        cancel_on_tap_outside: true,
        ux_mode: 'popup',
        context: 'signin',
        itp_support: true,
      })
      gid.renderButton(el, {
        type: 'standard',
        theme: 'filled_black',
        size: 'large',
        shape: 'pill',
        text: 'continue_with', // «Continuar con Google»
        locale: 'es',
        logo_alignment: 'left',
      })
    } catch (e: any) {
      authError.value = e.message
    }
  }

  function signOut(reason: string | null = null) {
    session.value = null
    authError.value = reason
    try {
      sessionStorage.removeItem(STORAGE_KEY)
    } catch {
      /* nada que borrar */
    }
    ;(window as any).google?.accounts?.id?.disableAutoSelect?.()
    if (localMode) restore()
  }

  return { session, authError, localMode, restore, renderButton, signOut }
}
