# Frontend de Mea culpa (Nuxt 4)

Ver el README de la raíz. `npm install && npm run dev` → http://localhost:3000 (la API debe estar en el puerto 8000,
o define `NUXT_PUBLIC_API_BASE`).

- `NUXT_PUBLIC_GOOGLE_CLIENT_ID`: client ID del cliente OAuth web de Google. Vacío = modo local sin login (solo
  funciona contra `backend/dev_server.py`).
- `npm run generate` escribe además `.output/public/staticwebapp.config.json` (CSP y cabeceras para Static Web
  Apps). Con `REQUIRE_DEPLOY_CONFIG=1` falla si faltan la URL https de la API o el client ID.
