// Aviso por correo de los mensajes del formulario de contacto (danielalonso.dev/contacto).
//
// Azure Communication Services Email con el dominio que gestiona Azure (DoNotReply@<id>.azurecomm.net): no hace
// falta tocar el DNS de danielalonso.dev. La API envía con su identidad administrada (rol "Communication and
// Email Service Owner" sobre el recurso), sin claves. Los datos se quedan en Europa. Cuesta unos 0,00025 USD por
// correo.

@description('Prefijo de los nombres (el mismo que main.bicep)')
param prefix string

@description('Sufijo único del grupo (el mismo que main.bicep)')
param suffix string

@description('principalId de la identidad administrada de la API')
param apiPrincipalId string

@description('Dónde se guardan los datos del servicio')
param dataLocation string = 'Europe'

var emailOwnerRole = '09976791-48a7-449e-bb21-39d1a415f350' // Communication and Email Service Owner

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: '${prefix}-email-${suffix}'
  location: 'global'
  properties: {
    dataLocation: dataLocation
  }
}

resource managedDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled' // sin píxeles de seguimiento ni enlaces reescritos
  }
}

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: '${prefix}-acs-${suffix}'
  location: 'global'
  properties: {
    dataLocation: dataLocation
    linkedDomains: [managedDomain.id]
  }
}

resource apiCanSendEmail 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acs.id, apiPrincipalId, emailOwnerRole)
  scope: acs
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', emailOwnerRole)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output endpoint string = 'https://${acs.properties.hostName}'
output sender string = 'DoNotReply@${managedDomain.properties.fromSenderDomain}'
