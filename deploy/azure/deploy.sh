#!/usr/bin/env bash
# Deploy KubeSage to Azure Container Apps (consumption plan, scale to zero).
#
# Images come from GitHub Container Registry (.github/workflows/azure-images.yml).
# Secrets are read from .env and stored as Container Apps secrets; they are never
# printed. Re-running the script updates an existing deployment.
#
#   ALLOWED_IP=203.0.113.7 deploy/azure/deploy.sh
#
# Required in .env: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`),
# NEO4J_URI_AURA, NEO4J_USERNAME_AURA, NEO4J_PASSWORD_AURA.
set -euo pipefail
cd "$(dirname "$0")/../.."

RESOURCE_GROUP="${RESOURCE_GROUP:-kubesage-rg}"
LOCATION="${LOCATION:-germanywestcentral}"
ENVIRONMENT="${ENVIRONMENT:-kubesage-env}"
REGISTRY="${REGISTRY:-ghcr.io/soumitrim}"
TAG="${TAG:-latest}"
API_APP="kubesage-api"   # must match API_UPSTREAM in the frontend image
WEB_APP="kubesage-web"
ALLOWED_IP="${ALLOWED_IP:?set ALLOWED_IP to the public IP allowed to open the app}"

env_value() {
  # Read one key from .env without echoing it; strips optional quotes.
  local value
  value="$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2-)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  [ -n "$value" ] || { echo "Missing $1 in .env" >&2; exit 1; }
  printf '%s' "$value"
}

CLAUDE_TOKEN="$(env_value CLAUDE_CODE_OAUTH_TOKEN)"
NEO4J_URI="$(env_value NEO4J_URI_AURA)"
NEO4J_USERNAME="$(env_value NEO4J_USERNAME_AURA)"
NEO4J_PASSWORD="$(env_value NEO4J_PASSWORD_AURA)"

app_exists() { az containerapp show -n "$1" -g "$RESOURCE_GROUP" --query name -o tsv >/dev/null 2>&1; }

echo "Registering resource providers"
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.OperationalInsights --wait

echo "Resource group $RESOURCE_GROUP in $LOCATION"
az group create -n "$RESOURCE_GROUP" -l "$LOCATION" --output none

MODE="$(az containerapp env show -n "$ENVIRONMENT" -g "$RESOURCE_GROUP" --query properties.environmentMode -o tsv 2>/dev/null || true)"
if [ -z "$MODE" ]; then
  # ConsumptionOnly, not the CLI's default Express mode: in Express environments
  # "internal" ingress is still reachable from the internet and plain HTTP
  # between apps is not supported.
  echo "Container Apps environment $ENVIRONMENT (ConsumptionOnly, no Log Analytics)"
  az containerapp env create -n "$ENVIRONMENT" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --environment-mode ConsumptionOnly --logs-destination none --output none
elif [ "$MODE" = "Express" ]; then
  echo "$ENVIRONMENT is an Express environment, where internal ingress is public." >&2
  echo "Delete it (az containerapp env delete -n $ENVIRONMENT -g $RESOURCE_GROUP) and re-run." >&2
  exit 1
fi

API_ENV=(
  CLAUDE_CODE_OAUTH_TOKEN=secretref:claude-token
  NEO4J_URI="$NEO4J_URI"
  NEO4J_USERNAME="$NEO4J_USERNAME"
  NEO4J_PASSWORD=secretref:neo4j-password
  LLM_BACKEND=claude_code
  LLM_EFFORT=low
  LLM_TIMEOUT_SECONDS=120
)

if app_exists "$API_APP"; then
  echo "Updating $API_APP"
  az containerapp secret set -n "$API_APP" -g "$RESOURCE_GROUP" --output none \
    --secrets claude-token="$CLAUDE_TOKEN" neo4j-password="$NEO4J_PASSWORD"
  az containerapp update -n "$API_APP" -g "$RESOURCE_GROUP" --output none \
    --image "$REGISTRY/kubesage-api:$TAG" --set-env-vars "${API_ENV[@]}"
else
  echo "Creating $API_APP (internal ingress, 1 vCPU / 2 GiB, scale 0-1)"
  az containerapp create -n "$API_APP" -g "$RESOURCE_GROUP" --environment "$ENVIRONMENT" \
    --image "$REGISTRY/kubesage-api:$TAG" \
    --ingress internal --target-port 8000 --transport http \
    --cpu 1.0 --memory 2.0Gi --min-replicas 0 --max-replicas 1 \
    --secrets claude-token="$CLAUDE_TOKEN" neo4j-password="$NEO4J_PASSWORD" \
    --env-vars "${API_ENV[@]}" --output none
fi
# The frontend calls the API over plain HTTP inside the environment.
az containerapp ingress update -n "$API_APP" -g "$RESOURCE_GROUP" --allow-insecure --output none

if app_exists "$WEB_APP"; then
  echo "Updating $WEB_APP"
  az containerapp update -n "$WEB_APP" -g "$RESOURCE_GROUP" --output none \
    --image "$REGISTRY/kubesage-frontend:$TAG"
else
  echo "Creating $WEB_APP (external ingress, 0.25 vCPU / 0.5 GiB, scale 0-1)"
  az containerapp create -n "$WEB_APP" -g "$RESOURCE_GROUP" --environment "$ENVIRONMENT" \
    --image "$REGISTRY/kubesage-frontend:$TAG" \
    --ingress external --target-port 8080 --transport http \
    --cpu 0.25 --memory 0.5Gi --min-replicas 0 --max-replicas 1 --output none
fi

echo "Allowing only $ALLOWED_IP to open the app"
az containerapp ingress access-restriction set -n "$WEB_APP" -g "$RESOURCE_GROUP" \
  --rule-name owner --ip-address "$ALLOWED_IP/32" --action Allow --output none

FQDN="$(az containerapp show -n "$WEB_APP" -g "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn -o tsv)"
echo "KubeSage: https://$FQDN"
