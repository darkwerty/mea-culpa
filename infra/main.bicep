// Mea culpa — infraestructura en Azure (ESCRITA, NO DESPLEGADA).
//
// Despliega en un grupo de recursos:
//   - Log Analytics + Application Insights (telemetría de la API)
//   - Recurso de Microsoft Foundry (Cognitive Services kind AIServices) con un deployment de modelo de chat
//   - Identidad administrada asignada por el usuario con el rol "Cognitive Services OpenAI User"
//     sobre el recurso de Foundry (sin claves: disableLocalAuth = true)
//   - Container Apps (plan Consumption) para la API, con escalado a cero
//   - Static Web App (Free) para el frontend (opcional)
//   - Presupuesto mensual con alertas por correo
//
// Uso (cuando decidas pagar):
//   az group create -n rg-mea-culpa -l swedencentral
//   az deployment group create -g rg-mea-culpa -f infra/main.bicep -p infra/main.bicepparam

targetScope = 'resourceGroup'

@description('Prefijo corto para nombrar recursos')
@minLength(3)
@maxLength(12)
param prefix string = 'meaculpa'

@description('Región de los recursos (la del modelo debe tener cuota para el deployment elegido)')
param location string = resourceGroup().location

@description('Imagen de la API (súbela antes a un registro). La de por defecto es solo un marcador.')
param apiImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Puerto en el que escucha el contenedor')
param apiPort int = 8000

@description('Modelo de chat a desplegar en Foundry (comprueba disponibilidad y versión en tu región)')
param modelName string = 'gpt-4.1-mini'

@description('Versión del modelo')
param modelVersion string = '2025-04-14'

@description('Tipo de deployment: GlobalStandard es pago por token')
@allowed([
  'GlobalStandard'
  'Standard'
  'DataZoneStandard'
])
param modelSku string = 'GlobalStandard'

@description('Capacidad en miles de tokens por minuto (TPM). 10 = 10K TPM, de sobra para una demo')
param modelCapacity int = 10

@description('Orígenes permitidos por CORS (el dominio del frontend)')
param corsOrigins string = ''

@description('Desplegar también el frontend en Static Web Apps (Free)')
param deployFrontend bool = true

@description('Importe mensual del presupuesto (en la moneda de la suscripción)')
param budgetAmount int = 10

@description('Correos que reciben las alertas del presupuesto')
param budgetEmails array

@description('Primer día del mes en que empieza el presupuesto (yyyy-MM-01)')
param budgetStartDate string = utcNow('yyyy-MM-01')

var suffix = uniqueString(resourceGroup().id)
var foundryName = '${prefix}-ai-${suffix}'
// Rol integrado "Cognitive Services OpenAI User": inferencia sin poder gestionar el recurso.
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

// ---------------------------------------------------------------- observabilidad
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 } // tope para no llevarse sustos
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-appi-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ---------------------------------------------------------------- identidad
resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-api-id'
  location: location
}

// ---------------------------------------------------------------- Microsoft Foundry
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryName
    disableLocalAuth: true // solo Entra ID: nada de claves en variables de entorno
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true
  }
}

resource chatModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: modelName
  sku: {
    name: modelSku
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

resource apiCanCallModel 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, apiIdentity.id, openAiUserRoleId)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: apiIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- Container Apps
resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env-${suffix}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-api'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${apiIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: apiPort
        transport: 'auto'
        allowInsecure: false
      }
      secrets: [
        { name: 'appinsights-conn', value: appInsights.properties.ConnectionString }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'LLM_PROVIDER', value: 'azure' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: 'https://${foundryName}.openai.azure.com/' }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: chatModel.name }
            // DefaultAzureCredential usa esta identidad administrada:
            { name: 'AZURE_CLIENT_ID', value: apiIdentity.properties.clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-conn' }
            { name: 'CORS_ORIGINS', value: corsOrigins }
            // Disco efímero: las partidas se pierden al escalar a cero (ver README, "Qué falta").
            { name: 'MEA_CULPA_DATA_DIR', value: '/tmp/mea-culpa' }
          ]
        }
      ]
      scale: {
        minReplicas: 0 // escala a cero: sin tráfico no hay coste de cómputo
        maxReplicas: 2
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
  dependsOn: [
    apiCanCallModel
  ]
}

// ---------------------------------------------------------------- frontend
resource web 'Microsoft.Web/staticSites@2023-12-01' = if (deployFrontend) {
  name: '${prefix}-web-${suffix}'
  // Static Web Apps solo existe en algunas regiones; centralus funciona en el plan Free.
  location: 'centralus'
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {}
}

// ---------------------------------------------------------------- presupuesto
resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: '${prefix}-budget'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      real50: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: budgetEmails
      }
      real90: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 90
        thresholdType: 'Actual'
        contactEmails: budgetEmails
      }
      previsto100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: budgetEmails
      }
    }
  }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output foundryEndpoint string = 'https://${foundryName}.openai.azure.com/'
output modelDeployment string = chatModel.name
output webUrl string = deployFrontend ? 'https://${web!.properties.defaultHostname}' : ''
output apiIdentityClientId string = apiIdentity.properties.clientId
