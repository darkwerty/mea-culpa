using './main.bicep'

// Región: la política de la suscripción solo permite francecentral (la Static Web App va fija en centralus).
param prefix = 'meaculpa'
param location = 'francecentral'

// Los dos salen de variables de entorno que define .github/workflows/deploy.yml:
//   API_IMAGE: ghcr.io/<owner>/mea-culpa-api:<sha> (sin ella, la imagen de ejemplo de Microsoft, para que un
//              despliegue manual no se rompa).
//   GOOGLE_CLIENT_ID: obligatoria; sin ella el .bicepparam no compila (y la API no arrancaría).
param apiImage = readEnvironmentVariable('API_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param googleClientId = readEnvironmentVariable('GOOGLE_CLIENT_ID')

param modelName = 'gpt-4.1-mini'
param modelVersion = '2025-04-14'
param modelSku = 'GlobalStandard'
param modelCapacity = 10

param corsOrigins = 'https://meaculpa.danielalonso.dev'
param deployFrontend = true
param webCustomDomain = 'meaculpa.danielalonso.dev'
// Segundo despliegue: BIND_CUSTOM_DOMAIN=true cuando el CNAME meaculpa -> <defaultHostname> ya exista.
param bindCustomDomain = bool(readEnvironmentVariable('BIND_CUSTOM_DOMAIN', 'false'))
param apiMaxReplicas = 1

param budgetAmount = 10
param budgetStartDate = '2026-11-01'
param budgetEmails = [
  'admin@example.com'
]
