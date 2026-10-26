#!/usr/bin/env bash
# Structural + lint check for the v0.12 vexa chart (no cluster required).
# Skips lint gracefully if helm is absent.
set -euo pipefail

HELM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHART="$HELM_DIR/charts/vexa"

echo "=== Helm chart structure (vexa v0.12) ==="
for f in Chart.yaml values.yaml values-test.yaml templates; do
  [ -e "$CHART/$f" ] || { echo "FAIL: missing $f"; exit 1; }
  echo "  OK: $f"
done

if command -v helm >/dev/null 2>&1; then
  # The default values ship NO stack secrets (adminApiToken / internalApiSecret are required), so the
  # default-values lint supplies operator values; rendering without them must FAIL, as must a
  # published placeholder.
  echo "  Linting (default values + operator-supplied secrets)..."
  helm lint "$CHART" --set secrets.adminApiToken=lint-admin --set secrets.internalApiSecret=lint-internal
  echo "  Refusing default secrets..."
  if helm template vexa "$CHART" >/dev/null 2>&1; then
    echo "FAIL: chart rendered with no adminApiToken/internalApiSecret"; exit 1
  fi
  if helm template vexa "$CHART" --set secrets.adminApiToken=changeme \
       --set secrets.internalApiSecret=lint-internal >/dev/null 2>&1; then
    echo "FAIL: chart rendered with the placeholder adminApiToken"; exit 1
  fi
  if helm template vexa "$CHART" --set secrets.adminApiToken=lint-admin \
       --set secrets.internalApiSecret=vexa-internal-secret >/dev/null 2>&1; then
    echo "FAIL: chart rendered with the placeholder internalApiSecret"; exit 1
  fi
  echo "  OK: empty / placeholder stack secrets refused"
  echo "  Linting (values-test)..."
  helm lint "$CHART" -f "$CHART/values-test.yaml"
else
  echo "  SKIP: helm not installed"
fi

echo "Helm chart lint: PASS"
