// Mea culpa — infraestructura en Azure.
//
// Despliega en un grupo de recursos:
//   - Log Analytics + Application Insights (telemetría de la API)
//   - Recurso de Microsoft Foundry (Cognitive Services kind AIServices) con un deployment de modelo de chat
//   - Identidad administrada asignada por el usuario con el rol "Cognitive Services OpenAI User"
//     sobre el recurso de Foundry (sin claves: disableLocalAuth = true)
//   - Storage account (Blob, sin claves compartidas) para partidas y cuotas, con el rol
//     "Storage Blob Data Contributor" para la misma identidad
//   - Container Apps (plan Consumption) para la API, con escalado a cero
//   - Static Web App (Free) para el frontend (opcional) y, en un segundo despliegue, su dominio propio
//   - Presupuesto mensual con alertas por correo
//
// Uso (lo normal es que lo lance .github/workflows/deploy.yml):
//   az group create -n rg-mea-culpa -l francecentral
//   az deployment group create -g rg-mea-culpa -f infra/main.bicep -p infra/main.bicepparam \
//     -p apiImage=ghcr.io/<owner>/mea-culpa-api:<sha> googleClientId=<id>.apps.googleusercontent.com

targetScope = 'resourceGroup'

@description('Prefijo corto para nombrar recursos')
@minLength(3)
@maxLength(12)
param prefix string = 'meaculpa'

@description('Región de los recursos (la del modelo debe tener cuota para el deployment elegido)')
param location string = resourceGroup().location

@description('Imagen de la API (pública en GHCR). La de por defecto es solo un marcador para que el despliegue no falle.')
param apiImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Client ID del cliente OAuth web de Google (Google Identity Services). La API no arranca sin él.')
@minLength(1)
param googleClientId string

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

@description('Orígenes permitidos por CORS, separados por comas (dominios exactos del frontend; nunca *)')
param corsOrigins string

@description('Dominio propio del frontend (CNAME a la Static Web App)')
param webCustomDomain string = 'meaculpa.danielalonso.dev'

@description('Asociar el dominio propio. Actívalo solo cuando el CNAME ya apunte al defaultHostname de la SWA.')
param bindCustomDomain bool = false

@description('Réplicas máximas de la API (tope de coste; el estado está en Blob, así que admite más de una)')
@minValue(1)
@maxValue(3)
param apiMaxReplicas int = 1

@description('Desplegar también el frontend en Static Web Apps (Free)')
param deployFrontend bool = true

@description('Importe mensual del presupuesto (en la moneda de la suscripción)')
param budgetAmount int = 10

@description('Correos que reciben las alertas del presupuesto')
param budgetEmails array

@description('Primer día del mes en que empieza el presupuesto (yyyy-MM-01). Fíjalo en el .bicepparam para que no cambie en cada despliegue.')
param budgetStartDate string = utcNow('yyyy-MM-01')

var suffix = uniqueString(resourceGroup().id)
var foundryName = '${prefix}-ai-${suffix}'
// Rol integrado "Cognitive Services OpenAI User": inferencia sin poder gestionar el recurso.
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
// Rol integrado "Storage Blob Data Contributor": leer y escribir blobs, sin gestionar la cuenta.
var blobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageName = take(toLower('${prefix}st${suffix}'), 24)
var storageContainerName = 'meaculpa'

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

// ---------------------------------------------------------------- almacenamiento (partidas y cuotas)
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    allowSharedKeyAccess: false // solo Entra ID (identidad administrada)
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource gamesContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: storageContainerName
  properties: { publicAccess: 'None' }
}

// Los contadores de cuota diarios (usage/AAAA-MM-DD/...) se borran solos a los 30 días.
resource storageLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'borrar-contadores-de-uso'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [ 'blockBlob' ]
              prefixMatch: [ '${storageContainerName}/usage/' ]
            }
            actions: {
              baseBlob: { delete: { daysAfterModificationGreaterThan: 30 } }
            }
          }
        }
      ]
    }
  }
}

resource apiCanUseStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, apiIdentity.id, blobContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributorRoleId)
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
            // Dominio propio + el *.azurestaticapps.net exacto de la SWA (para probar antes de tener el CNAME).
            { name: 'CORS_ORIGINS', value: deployFrontend ? '${corsOrigins},https://${web!.properties.defaultHostname}' : corsOrigins }
            { name: 'GOOGLE_CLIENT_ID', value: googleClientId }
            // Partidas y cuotas en Blob (compartido por las réplicas y persistente al escalar a cero).
            { name: 'AZURE_STORAGE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
            { name: 'AZURE_STORAGE_CONTAINER', value: storageContainerName }
            // Disco efímero solo para el registro de métricas del rival LLM (no hay datos de usuarios).
            { name: 'MEA_CULPA_DATA_DIR', value: '/tmp/mea-culpa' }
          ]
        }
      ]
      scale: {
        minReplicas: 0 // escala a cero: sin tráfico no hay coste de cómputo
        maxReplicas: apiMaxReplicas
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
    apiCanUseStorage
    gamesContainer
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

// Segundo paso: solo cuando el CNAME meaculpa -> <defaultHostname> ya existe (si no, la validación falla).
resource webDomain 'Microsoft.Web/staticSites/customDomains@2023-12-01' = if (deployFrontend && bindCustomDomain) {
  parent: web
  name: webCustomDomain
  properties: {
    validationMethod: 'cname-delegation'
  }
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
output webName string = deployFrontend ? web.name : ''
output webDefaultHostname string = deployFrontend ? web!.properties.defaultHostname : ''
output storageAccountName string = storage.name
output apiIdentityClientId string = apiIdentity.properties.clientId
