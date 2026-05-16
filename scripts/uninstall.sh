#!/usr/bin/env bash
set -euo pipefail

PURGE=false

usage() {
  cat <<USAGE
Usage:
  sudo bash scripts/uninstall.sh [--purge]

Options:
  --purge   Remove /etc/vpn-on-linux and /var/lib/vpn-on-linux too.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge)
      PURGE=true
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
  echo "Please run as root, for example: sudo bash scripts/uninstall.sh" >&2
  exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now vpn-on-linux.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/vpn-on-linux.service
  systemctl daemon-reload || true
fi

rm -f /usr/local/bin/vpncli /usr/local/bin/vpnctl
rm -rf /opt/vpn-on-linux

if [[ "$PURGE" == "true" ]]; then
  rm -rf /etc/vpn-on-linux /var/lib/vpn-on-linux
  echo "Uninstalled VPN On Linux Server and purged configuration."
else
  echo "Uninstalled binaries and service. Configuration remains in /etc/vpn-on-linux and /var/lib/vpn-on-linux."
fi
