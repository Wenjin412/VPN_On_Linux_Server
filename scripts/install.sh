#!/usr/bin/env bash
set -euo pipefail

REPO_RAW_BASE="${REPO_RAW_BASE:-https://raw.githubusercontent.com/Wenjin412/VPN_On_Linux_Server/main}"
MIHOMO_REPO="${MIHOMO_REPO:-MetaCubeX/mihomo}"
INSTALL_DIR="${INSTALL_DIR:-/opt/vpn-on-linux}"
BIN_DIR="$INSTALL_DIR/bin"
ETC_DIR="${VPNCLI_ETC_DIR:-${VPNCTL_ETC_DIR:-/etc/vpn-on-linux}}"
VAR_DIR="${VPNCLI_VAR_DIR:-${VPNCTL_VAR_DIR:-/var/lib/vpn-on-linux}}"
SUBSCRIPTION_URL="${VPN_SUBSCRIPTION_URL:-}"
START_AFTER_INSTALL=auto
GITHUB_PROXY_LIST="${VPNCLI_GITHUB_PROXY_LIST:-${VPNCLI_GITHUB_PROXY:-${GITHUB_PROXY:-}}}"
MIHOMO_FILE="${VPNCLI_MIHOMO_FILE:-${MIHOMO_FILE:-}}"
FORCE_MIHOMO_DOWNLOAD="${VPNCLI_FORCE_MIHOMO_DOWNLOAD:-false}"
DOWNLOAD_CACHE="${VPNCLI_DOWNLOAD_CACHE:-/tmp/vpn-on-linux-install-cache}"
CURL_CONNECT_TIMEOUT="${VPNCLI_CURL_CONNECT_TIMEOUT:-20}"
CURL_MAX_TIME="${VPNCLI_CURL_MAX_TIME:-3600}"
CURL_SPEED_TIME="${VPNCLI_CURL_SPEED_TIME:-120}"
CURL_SPEED_LIMIT="${VPNCLI_CURL_SPEED_LIMIT:-1}"
CURL_COMMON=(
  --fail
  --location
  --retry 5
  --retry-delay 2
  --connect-timeout "$CURL_CONNECT_TIMEOUT"
  --max-time "$CURL_MAX_TIME"
  --speed-time "$CURL_SPEED_TIME"
  --speed-limit "$CURL_SPEED_LIMIT"
)

usage() {
  cat <<USAGE
Usage:
  sudo bash scripts/install.sh [--subscription URL] [--no-start]

Environment:
  VPN_SUBSCRIPTION_URL   Subscription URL to configure during install.
  MIHOMO_VERSION         Optional Mihomo tag, for example v1.19.24.
  VPNCLI_MIHOMO_FILE     Optional local Mihomo .gz or executable path.
  VPNCLI_FORCE_MIHOMO_DOWNLOAD=true
                         Re-download Mihomo even when already installed.
  VPNCLI_DOWNLOAD_CACHE  Download cache directory. Defaults to /tmp/vpn-on-linux-install-cache.
  VPNCLI_GITHUB_PROXY    Optional trusted GitHub mirror/proxy prefix for mainland servers.
                         Example: https://your-mirror.example.com/
                         The mirror receives full GitHub URLs as path, or use {url}.
  VPNCLI_GITHUB_PROXY_LIST
                         Comma-separated mirror/proxy prefixes tried before direct GitHub.
  REPO_RAW_BASE          Raw GitHub base used when the script is piped from curl.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subscription)
      SUBSCRIPTION_URL="${2:-}"
      shift 2
      ;;
    --no-start)
      START_AFTER_INSTALL=false
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Please run as root, for example: sudo bash scripts/install.sh" >&2
  exit 1
fi

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd curl
require_cmd gzip
require_cmd install
require_cmd uname
require_cmd python3

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer targets Linux. For development, run vpn_on_linux/vpnctl.py directly." >&2
  exit 1
fi

if [[ -n "$SUBSCRIPTION_URL" && "$SUBSCRIPTION_URL" != http://* && "$SUBSCRIPTION_URL" != https://* ]]; then
  cat >&2 <<MSG
Invalid subscription URL: $SUBSCRIPTION_URL

Please replace the placeholder with your real Clash subscription URL, for example:
  sudo bash scripts/install.sh --subscription 'https://example.com/your/subscription?clash=1'
MSG
  exit 2
fi

script_dir=""
source_path="${BASH_SOURCE[0]:-}"
if [[ -n "$source_path" && "$source_path" != "bash" ]]; then
  script_dir="$(cd "$(dirname "$source_path")" && pwd)"
fi
repo_dir=""
if [[ -n "$script_dir" && -f "$script_dir/../vpn_on_linux/vpnctl.py" ]]; then
  repo_dir="$(cd "$script_dir/.." && pwd)"
fi

fetch_file() {
  local rel="$1"
  local dest="$2"
  if [[ -n "$repo_dir" && -f "$repo_dir/$rel" ]]; then
    install -m 0755 "$repo_dir/$rel" "$dest"
  else
    download_url "$REPO_RAW_BASE/$rel" "$dest"
    chmod 0755 "$dest"
  fi
}

proxied_url() {
  local proxy="$1"
  local url="$2"
  if [[ -z "$proxy" ]]; then
    echo "$url"
  elif [[ "$proxy" == *"{url}"* ]]; then
    echo "${proxy//\{url\}/$url}"
  else
    echo "${proxy%/}/$url"
  fi
}

download_candidates() {
  local url="$1"
  local proxy
  if [[ -n "$GITHUB_PROXY_LIST" ]]; then
    IFS=',' read -ra proxies <<< "$GITHUB_PROXY_LIST"
    for proxy in "${proxies[@]}"; do
      proxy="${proxy//[[:space:]]/}"
      [[ -n "$proxy" ]] && proxied_url "$proxy" "$url"
    done
  fi
  echo "$url"
}

download_url() {
  local url="$1"
  local dest="$2"
  local resume="${3:-false}"
  local candidate
  mkdir -p "$(dirname "$dest")"
  while IFS= read -r candidate; do
    echo "Downloading: $candidate"
    if [[ "$resume" == "true" ]]; then
      if curl "${CURL_COMMON[@]}" --continue-at - "$candidate" -o "$dest"; then
        return 0
      fi
    else
      rm -f "$dest"
      if curl "${CURL_COMMON[@]}" "$candidate" -o "$dest"; then
        return 0
      fi
    fi
    echo "Download failed, trying next source if available." >&2
  done < <(download_candidates "$url")
  echo "All download sources failed for: $url" >&2
  exit 1
}

fetch_text() {
  local url="$1"
  local candidate
  while IFS= read -r candidate; do
    if curl "${CURL_COMMON[@]}" --silent --show-error "$candidate"; then
      return 0
    fi
  done < <(download_candidates "$url")
  echo "All text sources failed for: $url" >&2
  exit 1
}

effective_url() {
  local url="$1"
  local candidate effective
  while IFS= read -r candidate; do
    effective="$(curl --fail --silent --show-error --location --head --connect-timeout 15 --max-time 60 -o /dev/null -w '%{url_effective}' "$candidate" || true)"
    if [[ -n "$effective" ]]; then
      echo "$effective"
      return 0
    fi
  done < <(download_candidates "$url")
  echo "Could not resolve latest release URL: $url" >&2
  exit 1
}

latest_mihomo_tag() {
  if [[ -n "${MIHOMO_VERSION:-}" ]]; then
    echo "$MIHOMO_VERSION"
    return
  fi
  local effective
  effective="$(effective_url "https://github.com/$MIHOMO_REPO/releases/latest")"
  echo "${effective##*/}"
}

asset_pattern() {
  case "$(uname -m)" in
    x86_64|amd64)
      echo 'mihomo-linux-amd64-v1([^[:alnum:]].*)?\.gz|mihomo-linux-amd64-[^-].*\.gz|mihomo-linux-amd64-compatible-.*\.gz'
      ;;
    aarch64|arm64)
      echo 'mihomo-linux-arm64-.*\.gz'
      ;;
    armv7l|armv7)
      echo 'mihomo-linux-armv7-.*\.gz'
      ;;
    armv6l|armv6)
      echo 'mihomo-linux-armv6-.*\.gz'
      ;;
    armv5l|armv5)
      echo 'mihomo-linux-armv5-.*\.gz'
      ;;
    i386|i686)
      echo 'mihomo-linux-386-.*\.gz'
      ;;
    *)
      echo "Unsupported architecture: $(uname -m)" >&2
      exit 1
      ;;
  esac
}

select_asset() {
  local tag="$1"
  local pattern candidates preferred
  pattern="$(asset_pattern)"
  local html
  html="$(fetch_text "https://github.com/$MIHOMO_REPO/releases/expanded_assets/$tag")"
  candidates="$(printf '%s\n' "$html" \
    | grep -Eo 'mihomo-linux-[^"< ]+\.gz' \
    | sort -u \
    | grep -E "$pattern" || true)"
  preferred="$(printf '%s\n' "$candidates" | grep -E 'mihomo-linux-amd64-v1([.-]|$)|mihomo-linux-arm64-|mihomo-linux-armv7-|mihomo-linux-armv6-|mihomo-linux-armv5-|mihomo-linux-386-' | head -n 1 || true)"
  if [[ -n "$preferred" ]]; then
    echo "$preferred"
  else
    printf '%s\n' "$candidates" | head -n 1
  fi
}

install_mihomo_from_file() {
  local src="$1"
  local tmpdir kind
  if [[ ! -f "$src" ]]; then
    echo "VPNCLI_MIHOMO_FILE does not exist: $src" >&2
    exit 1
  fi
  tmpdir="$(mktemp -d)"
  kind="$(file -b "$src" || true)"
  if [[ "$src" == *.gz || "$kind" == *gzip* ]]; then
    gzip -dc "$src" > "$tmpdir/mihomo"
  else
    cp "$src" "$tmpdir/mihomo"
  fi
  chmod +x "$tmpdir/mihomo"
  if ! "$tmpdir/mihomo" -v >/dev/null 2>&1; then
    echo "Provided Mihomo file is not executable or is for the wrong architecture: $src" >&2
    rm -rf "$tmpdir"
    exit 1
  fi
  install -m 0755 "$tmpdir/mihomo" "$BIN_DIR/mihomo"
  rm -rf "$tmpdir"
}

install_mihomo() {
  local tag asset url tmpdir
  if [[ -n "$MIHOMO_FILE" ]]; then
    echo "Installing Mihomo from local file: $MIHOMO_FILE"
    install_mihomo_from_file "$MIHOMO_FILE"
    return
  fi
  if [[ -x "$BIN_DIR/mihomo" && "$FORCE_MIHOMO_DOWNLOAD" != "true" ]]; then
    echo "Mihomo already installed at $BIN_DIR/mihomo; skipping download."
    return
  fi
  tag="$(latest_mihomo_tag)"
  asset="$(select_asset "$tag")"
  if [[ -z "$asset" ]]; then
    echo "Could not find a Mihomo Linux asset for $(uname -m) in $tag." >&2
    exit 1
  fi
  url="https://github.com/$MIHOMO_REPO/releases/download/$tag/$asset"
  tmpdir="$(mktemp -d)"
  echo "Downloading Mihomo $tag: $asset"
  mkdir -p "$DOWNLOAD_CACHE"
  download_url "$url" "$DOWNLOAD_CACHE/$asset" true
  gzip -dc "$DOWNLOAD_CACHE/$asset" > "$tmpdir/mihomo"
  install -m 0755 "$tmpdir/mihomo" "$BIN_DIR/mihomo"
  rm -rf "$tmpdir"
}

install -d -m 0755 "$BIN_DIR"
install -d -m 0700 "$ETC_DIR" "$VAR_DIR"
install -d -m 0755 /usr/local/bin

install_mihomo
fetch_file "vpn_on_linux/vpnctl.py" "$BIN_DIR/vpncli"
ln -sf "$BIN_DIR/vpncli" /usr/local/bin/vpncli
ln -sf "$BIN_DIR/vpncli" /usr/local/bin/vpnctl

if command -v systemctl >/dev/null 2>&1; then
  if [[ -n "$repo_dir" && -f "$repo_dir/systemd/vpn-on-linux.service" ]]; then
    install -m 0644 "$repo_dir/systemd/vpn-on-linux.service" /etc/systemd/system/vpn-on-linux.service
  else
    tmp_unit="$(mktemp)"
    download_url "$REPO_RAW_BASE/systemd/vpn-on-linux.service" "$tmp_unit"
    install -m 0644 "$tmp_unit" /etc/systemd/system/vpn-on-linux.service
    rm -f "$tmp_unit"
  fi
  systemctl daemon-reload
else
  echo "systemctl was not found; installed vpncli and Mihomo only." >&2
fi

if [[ -n "$SUBSCRIPTION_URL" ]]; then
  if [[ "$START_AFTER_INSTALL" == "auto" ]]; then
    START_AFTER_INSTALL=true
  fi
  if [[ "$START_AFTER_INSTALL" == "true" ]]; then
    /usr/local/bin/vpncli setup "$SUBSCRIPTION_URL"
  else
    /usr/local/bin/vpncli subscription set "$SUBSCRIPTION_URL"
  fi
else
  cat <<MSG
Installed VPN On Linux Server.

Next:
  sudo vpncli setup '<your-clash-subscription-url>'
  vpncli status
  vpncli test

The default mode is proxy-only and targeted, so it does not change server routes
or interfere with inbound API services.
MSG
fi
