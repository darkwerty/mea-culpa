using './main.bicep'

// Valores de ejemplo. Revisa región, modelo y versión antes de desplegar.
param prefix = 'meaculpa'
param location = 'swedencentral'
param apiImage = 'ghcr.io/REEMPLAZAR/mea-culpa-api:0.1.0'
param modelName = 'gpt-4.1-mini'
param modelVersion = '2025-04-14'
param modelSku = 'GlobalStandard'
param modelCapacity = 10
param corsOrigins = 'https://REEMPLAZAR.azurestaticapps.net'
param deployFrontend = true
param budgetAmount = 10
param budgetEmails = [
  'admin@example.com'
]
