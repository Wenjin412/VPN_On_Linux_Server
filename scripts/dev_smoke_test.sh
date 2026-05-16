#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${VPN_SUBSCRIPTION_URL:-}" ]]; then
  echo "Set VPN_SUBSCRIPTION_URL before running this smoke test." >&2
  exit 2
fi

RUN_AUTO=false
if [[ "${1:-}" == "--auto" ]]; then
  RUN_AUTO=true
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
cleanup() {
  local status=$?
  kill "${MIHOMO_PID:-}" >/dev/null 2>&1 || true
  if [[ "$status" -eq 0 ]]; then
    rm -rf "$TMP_DIR"
  else
    echo "Smoke test failed. Temporary files kept at: $TMP_DIR" >&2
    if [[ -f "$TMP_DIR/mihomo.log" ]]; then
      echo "----- mihomo.log -----" >&2
      tail -n 120 "$TMP_DIR/mihomo.log" >&2
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

export VPNCTL_ETC_DIR="$TMP_DIR/etc"
export VPNCTL_VAR_DIR="$TMP_DIR/var"
export VPNCTL_STATE_PATH="$VPNCTL_ETC_DIR/state.json"
export VPNCTL_CONFIG_PATH="$VPNCTL_ETC_DIR/config.yaml"
export VPNCTL_PROVIDER_PATH="$VPNCTL_ETC_DIR/providers/subscription.yaml"
export VPNCTL_SERVICE_NAME="vpn-on-linux-dev.service"

latest_mihomo_tag() {
  local effective
  effective="$(curl -fsSIL -o /dev/null -w '%{url_effective}' https://github.com/MetaCubeX/mihomo/releases/latest)"
  echo "${effective##*/}"
}

asset_pattern() {
  local os arch
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "$os:$arch" in
    darwin:arm64|darwin:aarch64) echo 'mihomo-darwin-arm64-.*\.gz' ;;
    darwin:x86_64) echo 'mihomo-darwin-amd64-v1([.-]|$).*\.gz|mihomo-darwin-amd64-.*\.gz' ;;
    linux:x86_64|linux:amd64) echo 'mihomo-linux-amd64-v1([.-]|$).*\.gz|mihomo-linux-amd64-.*\.gz' ;;
    linux:aarch64|linux:arm64) echo 'mihomo-linux-arm64-.*\.gz' ;;
    *) echo "Unsupported smoke-test platform: $os $arch" >&2; exit 1 ;;
  esac
}

download_mihomo() {
  local tag pattern html asset
  tag="$(latest_mihomo_tag)"
  pattern="$(asset_pattern)"
  html="$(curl -fsSL "https://github.com/MetaCubeX/mihomo/releases/expanded_assets/$tag")"
  asset="$(printf '%s\n' "$html" | grep -Eo 'mihomo-(darwin|linux)-[^"< ]+\.gz' | sort -u | grep -E "$pattern" | head -n 1)"
  if [[ -z "$asset" ]]; then
    echo "Could not find Mihomo asset for smoke-test platform." >&2
    exit 1
  fi
  curl -fL --retry 3 "https://github.com/MetaCubeX/mihomo/releases/download/$tag/$asset" -o "$TMP_DIR/mihomo.gz"
  gzip -dc "$TMP_DIR/mihomo.gz" > "$TMP_DIR/mihomo"
  chmod +x "$TMP_DIR/mihomo"
}

download_mihomo

python3 "$ROOT_DIR/vpn_on_linux/vpnctl.py" subscription set "$VPN_SUBSCRIPTION_URL" --quiet
python3 - <<'PY'
import json
import os
from pathlib import Path

state_path = Path(os.environ["VPNCTL_STATE_PATH"])
state = json.loads(state_path.read_text())
state["mixed_port"] = 17890
state["controller_port"] = 19090
state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
PY
python3 "$ROOT_DIR/vpn_on_linux/vpnctl.py" render --quiet

"$TMP_DIR/mihomo" -d "$VPNCTL_ETC_DIR" -f "$VPNCTL_CONFIG_PATH" > "$TMP_DIR/mihomo.log" 2>&1 &
MIHOMO_PID="$!"

for _ in $(seq 1 40); do
  if python3 "$ROOT_DIR/vpn_on_linux/vpnctl.py" info >/dev/null 2>&1 \
    && python3 "$ROOT_DIR/vpn_on_linux/vpnctl.py" nodes list >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

python3 "$ROOT_DIR/vpn_on_linux/vpnctl.py" nodes list | sed -n '1,10p'
if [[ "$RUN_AUTO" == "true" ]]; then
  python3 "$ROOT_DIR/vpn_on_linux/vpnctl.py" auto --timeout-ms 6000 --workers 8
else
  python3 "$ROOT_DIR/vpn_on_linux/vpnctl.py" nodes use AUTO
fi
python3 "$ROOT_DIR/vpn_on_linux/vpnctl.py" test --timeout 15 google openai anthropic

echo "Smoke test passed. Mihomo log: $TMP_DIR/mihomo.log"
