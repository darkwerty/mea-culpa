using './main.bicep'

// Región: la política de la suscripción solo permite francecentral (la Static Web App va fija en centralus).
param prefix = 'meaculpa'
param location = 'francecentral'

// Los dos salen de variables de entorno que define .github/workflows/deploy.yml:
//   API_IMAGE: <acr>.azurecr.io/mea-culpa-api:<sha> (sin ella, la imagen de ejemplo de Microsoft, para el primer
//              despliegue, con el ACR aún vacío).
//   GOOGLE_CLIENT_ID: obligatoria; sin ella el .bicepparam no compila (y la API no arrancaría).
param apiImage = readEnvironmentVariable('API_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param googleClientId = readEnvironmentVariable('GOOGLE_CLIENT_ID')

// Cuenta de Foundry existente (francecentral). gpt-6-luna no tiene cuota en la suscripción (0 TPM), así que la demo
// usa gpt-4.1-mini (GlobalStandard, 200K TPM de cuota disponibles), que además admite temperatura.
param foundryAccountName = 'meaculpa-fc-7952'
// Selector de modelo del rival IA: "todos" = los no ocultos del catálogo (backend/meaculpa/models.py), con su
// deployment creado por la CLI. gpt-6-luna, gpt-6.1-terra y claude-haiku-5-5 están ocultos (cuota 0): al conseguirla,
// crea su deployment y añádelos aquí, p. ej. 'todos,gpt-6-luna'.
param llmModels = 'todos'
param foundrySweAccountName = 'meaculpa-swe-7952'
param claudeResourceName = 'meaculpa-swe-7952'
param modelName = 'gpt-4.1-mini'
param modelVersion = '2025-04-14'
param modelSku = 'GlobalStandard'
param modelCapacity = 10

param corsOrigins = 'https://danielalonso.dev,https://meaculpa.danielalonso.dev'
param deployFrontend = true
param webCustomDomain = 'meaculpa.danielalonso.dev'
// Segundo despliegue: BIND_CUSTOM_DOMAIN=true cuando el CNAME meaculpa -> <defaultHostname> ya exista.
param bindCustomDomain = bool(readEnvironmentVariable('BIND_CUSTOM_DOMAIN', 'false'))
param apiMaxReplicas = 1

param budgetAmount = 10
param budgetStartDate = '2026-11-01'
// Correos fuera del repositorio: ADMIN_EMAILS (acceso a /admin) y BUDGET_EMAILS (avisos del presupuesto), separados
// por comas
param adminEmails = readEnvironmentVariable('ADMIN_EMAILS')
param budgetEmails = split(readEnvironmentVariable('BUDGET_EMAILS'), ',')
