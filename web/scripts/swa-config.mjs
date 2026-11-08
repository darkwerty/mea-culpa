// Genera .output/public/staticwebapp.config.json después de `nuxt generate`:
// cabeceras de seguridad para Azure Static Web Apps con una CSP sin 'unsafe-inline' en scripts
// (los <script> en línea que mete Nuxt se permiten por su hash SHA-256) y connect-src limitado a la API.
//
// En el despliegue (REQUIRE_DEPLOY_CONFIG=1) falla si faltan la URL de la API o el client ID de Google,
// para no publicar un frontend que llame a 127.0.0.1 o sin login.
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const out = join(import.meta.dirname, '..', '.output', 'public')
const apiBase = process.env.NUXT_PUBLIC_API_BASE || 'http://127.0.0.1:8000'
const googleClientId = process.env.NUXT_PUBLIC_GOOGLE_CLIENT_ID || ''

if (process.env.REQUIRE_DEPLOY_CONFIG === '1') {
  if (!process.env.NUXT_PUBLIC_API_BASE?.startsWith('https://')) {
    throw new Error('NUXT_PUBLIC_API_BASE debe ser la URL https de la API para desplegar')
  }
  if (!googleClientId.endsWith('.apps.googleusercontent.com')) {
    throw new Error('NUXT_PUBLIC_GOOGLE_CLIENT_ID no parece un client ID de Google')
  }
}

const hashes = new Set()
for (const page of ['index.html', '200.html', '404.html']) {
  const file = join(out, page)
  if (!existsSync(file)) continue
  const html = readFileSync(file, 'utf8')
  for (const m of html.matchAll(/<script(?<attrs>[^>]*)>(?<body>[\s\S]*?)<\/script>/g)) {
    const { attrs, body } = m.groups
    if (/\bsrc=/.test(attrs) || /type="application\/json"/.test(attrs) || !body.trim()) continue
    hashes.add(`'sha256-${createHash('sha256').update(body, 'utf8').digest('base64')}'`)
  }
}

const apiOrigin = new URL(apiBase).origin
const gsi = 'https://accounts.google.com/gsi/'
const csp = [
  "default-src 'self'",
  `script-src 'self' ${[...hashes].join(' ')} ${gsi}client`,
  // Los estilos de la pantalla de Google y los atributos style de Vue/GIS necesitan 'unsafe-inline' en estilos.
  `style-src 'self' 'unsafe-inline' https://fonts.googleapis.com ${gsi}style`,
  "font-src 'self' https://fonts.gstatic.com",
  "img-src 'self' data: https://*.googleusercontent.com",
  `connect-src 'self' ${apiOrigin} ${gsi}`,
  `frame-src ${gsi}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join('; ')

const config = {
  navigationFallback: { rewrite: '/index.html', exclude: ['/_nuxt/*', '/favicon.svg'] },
  globalHeaders: {
    'Content-Security-Policy': csp,
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    // El popup de Google necesita poder comunicarse con esta ventana.
    'Cross-Origin-Opener-Policy': 'same-origin-allow-popups',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()',
    'Strict-Transport-Security': 'max-age=31536000',
  },
}

writeFileSync(join(out, 'staticwebapp.config.json'), JSON.stringify(config, null, 2) + '\n')
console.log(`staticwebapp.config.json: ${hashes.size} hash(es) de scripts en línea, connect-src ${apiOrigin}`)
