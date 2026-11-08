// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2026-06-01',
  devtools: { enabled: false },
  ssr: false,
  css: ['~/assets/main.css'],
  runtimeConfig: {
    public: {
      // Sobrescribible con NUXT_PUBLIC_API_BASE (en el despliegue, la URL de Container Apps)
      apiBase: 'http://127.0.0.1:8000',
      // NUXT_PUBLIC_GOOGLE_CLIENT_ID. Vacío = modo local sin login (solo con backend/dev_server.py)
      googleClientId: '',
    },
  },
  app: {
    head: {
      htmlAttrs: { lang: 'es' },
      title: 'Mea culpa: por qué has perdido',
      meta: [
        { name: 'viewport', content: 'width=device-width, initial-scale=1' },
        { name: 'description', content: 'Juega al ajedrez y descubre, en castellano, por qué has perdido.' },
      ],
      link: [
        { rel: 'preconnect', href: 'https://fonts.googleapis.com' },
        { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' },
        {
          rel: 'stylesheet',
          href: 'https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=Figtree:wght@400;500;600;700&display=swap',
        },
        { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' },
      ],
    },
  },
})
