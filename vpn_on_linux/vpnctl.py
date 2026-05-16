#!/usr/bin/env python3
"""Command line manager for VPN On Linux Server.

The project intentionally keeps this file dependency-free so installation on
minimal Linux servers is predictable. Mihomo does the proxy work; vpncli handles
configuration, subscription refreshes, service control, and health checks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import grp
import http.client
import json
import os
import platform
import pwd
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


APP_NAME = "vpn-on-linux"
CLI_NAME = "vpncli"


def env_value(name: str, default: str) -> str:
    return os.environ.get(f"VPNCLI_{name}", os.environ.get(f"VPNCTL_{name}", default))


SERVICE_NAME = env_value("SERVICE_NAME", "vpn-on-linux.service")
ETC_DIR = Path(env_value("ETC_DIR", "/etc/vpn-on-linux"))
VAR_DIR = Path(env_value("VAR_DIR", "/var/lib/vpn-on-linux"))
STATE_PATH = Path(env_value("STATE_PATH", str(ETC_DIR / "state.json")))
CONFIG_PATH = Path(env_value("CONFIG_PATH", str(ETC_DIR / "config.yaml")))
PROVIDER_PATH = Path(
    env_value("PROVIDER_PATH", str(ETC_DIR / "providers" / "subscription.yaml"))
)

DEFAULT_MIXED_PORT = 7890
DEFAULT_CONTROLLER_PORT = 9090
DEFAULT_CONTROLLER_HOST = "127.0.0.1"
DEFAULT_AUTO_TIMEOUT_MS = 5000

NO_PROXY_VALUE = ",".join(
    [
        "localhost",
        "127.0.0.1",
        "::1",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
    ]
)

PRODUCTS = {
    "google": {
        "label": "Google",
        "url": "https://www.google.com/generate_204",
        "home": "https://www.google.com",
    },
    "openai": {
        "label": "OpenAI",
        "url": "https://api.openai.com/v1/models",
        "home": "https://chat.openai.com",
    },
    "anthropic": {
        "label": "Anthropic",
        "url": "https://api.anthropic.com/v1/messages",
        "home": "https://claude.ai",
    },
}

TARGETED_RULES = [
    "DOMAIN-SUFFIX,google.com,VPN",
    "DOMAIN-SUFFIX,gstatic.com,VPN",
    "DOMAIN-SUFFIX,googleapis.com,VPN",
    "DOMAIN-SUFFIX,googleusercontent.com,VPN",
    "DOMAIN-SUFFIX,ggpht.com,VPN",
    "DOMAIN-SUFFIX,ytimg.com,VPN",
    "DOMAIN-SUFFIX,youtube.com,VPN",
    "DOMAIN-SUFFIX,openai.com,VPN",
    "DOMAIN-SUFFIX,chatgpt.com,VPN",
    "DOMAIN-SUFFIX,oaistatic.com,VPN",
    "DOMAIN-SUFFIX,oaiusercontent.com,VPN",
    "DOMAIN-SUFFIX,auth0.openai.com,VPN",
    "DOMAIN-SUFFIX,anthropic.com,VPN",
    "DOMAIN-SUFFIX,claude.ai,VPN",
    "DOMAIN-SUFFIX,claudeusercontent.com,VPN",
    "MATCH,DIRECT",
]

BASE_DIRECT_RULES = [
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,224.0.0.0/4,DIRECT,no-resolve",
    "IP-CIDR6,::1/128,DIRECT,no-resolve",
    "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
    "IP-CIDR6,fe80::/10,DIRECT,no-resolve",
]


class CliError(RuntimeError):
    """Expected user-facing error."""


def default_state() -> dict[str, Any]:
    return {
        "subscription_url": "",
        "subscription_tls_verify": "auto",
        "mixed_port": DEFAULT_MIXED_PORT,
        "controller_host": DEFAULT_CONTROLLER_HOST,
        "controller_port": DEFAULT_CONTROLLER_PORT,
        "controller_secret": secrets.token_urlsafe(24),
        "route_mode": "targeted",
        "tun_enabled": False,
        "auto_timeout_ms": DEFAULT_AUTO_TIMEOUT_MS,
        "auto_products": ["google", "openai", "anthropic"],
    }


def merge_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = default_state()
    secret = raw.get("controller_secret")
    state.update(raw)
    if secret:
        state["controller_secret"] = secret
    return state


def read_state(required: bool = False) -> dict[str, Any]:
    try:
        exists = STATE_PATH.exists()
    except PermissionError as exc:
        raise CliError(
            f"Permission denied reading {STATE_PATH}. Run 'sudo {CLI_NAME} fix-permissions' "
            f"or retry this command with sudo."
        ) from exc
    if not exists:
        if required:
            raise CliError(
                f"{STATE_PATH} does not exist. Run 'sudo {CLI_NAME} setup <subscription-url>' first."
            )
        return default_state()
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except PermissionError as exc:
        raise CliError(
            f"Permission denied reading {STATE_PATH}. Run 'sudo {CLI_NAME} fix-permissions' "
            f"or retry this command with sudo."
        ) from exc
    if not isinstance(data, dict):
        raise CliError(f"{STATE_PATH} is not a JSON object.")
    return merge_state(data)


def atomic_write(path: Path, content: str | bytes, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        if isinstance(content, str):
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def write_state(state: dict[str, Any]) -> None:
    atomic_write(STATE_PATH, json.dumps(state, indent=2, sort_keys=True) + "\n", 0o600)


def yaml_quote(value: Any) -> str:
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def yaml_bool(value: bool) -> str:
    return "true" if bool(value) else "false"


def redact_url(url: str) -> str:
    if not url:
        return "<not configured>"
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc or "<host>"
    suffix = ""
    if parsed.query:
        suffix = "..."
    return urllib.parse.urlunsplit((parsed.scheme, host, "/...", suffix, ""))


def need_subscription(state: dict[str, Any]) -> str:
    url = state.get("subscription_url", "")
    if not url:
        raise CliError(f"No subscription URL configured. Run 'sudo {CLI_NAME} setup <subscription-url>'.")
    return str(url)


def ensure_dirs() -> None:
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ETC_DIR, 0o2750)
        os.chmod(VAR_DIR, 0o2750)
    except PermissionError:
        pass


def download_url(url: str, verify_tls: bool, timeout: int = 45) -> tuple[bytes, dict[str, str]]:
    headers = {
        "User-Agent": "VPN-On-Linux-Server/1.0",
        "Accept": "*/*",
    }
    req = urllib.request.Request(url, headers=headers)
    context = None
    if urllib.parse.urlsplit(url).scheme == "https" and not verify_tls:
        context = ssl._create_unverified_context()  # noqa: SLF001 - stdlib has no public equivalent.
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return resp.read(), dict(resp.headers.items())


def tls_verify_mode(state: dict[str, Any]) -> str:
    value = state.get("subscription_tls_verify", "auto")
    if isinstance(value, bool):
        return "on" if value else "off"
    if str(value) in {"on", "off", "auto"}:
        return str(value)
    return "auto"


def is_cert_verify_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        text = str(current)
        if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
            return True
        reason = getattr(current, "reason", None)
        current = reason if isinstance(reason, BaseException) else current.__cause__ or current.__context__
    return False


def provider_is_fresh(max_age_seconds: int | None) -> bool:
    if not max_age_seconds or max_age_seconds <= 0 or not PROVIDER_PATH.exists():
        return False
    age = time.time() - PROVIDER_PATH.stat().st_mtime
    return age < max_age_seconds


def refresh_subscription(args: argparse.Namespace | None = None) -> None:
    quiet = bool(getattr(args, "quiet", False)) if args else False
    if_stale = getattr(args, "if_stale", None) if args else None
    if provider_is_fresh(if_stale):
        if not quiet:
            print(f"Subscription cache is fresh; skipped refresh for {PROVIDER_PATH}")
        return
    state = read_state(required=True)
    url = need_subscription(state)
    mode = tls_verify_mode(state)
    try:
        data, headers = download_url(url, mode != "off")
    except urllib.error.URLError as exc:
        if mode == "auto" and is_cert_verify_error(exc):
            if not quiet:
                print("TLS verification failed for the subscription; retrying once with TLS verification disabled.")
            try:
                data, headers = download_url(url, False)
            except urllib.error.URLError as retry_exc:
                raise CliError(f"Failed to download subscription: {retry_exc}") from retry_exc
        else:
            raise CliError(f"Failed to download subscription: {exc}") from exc
    if len(data) < 32:
        raise CliError("Subscription response is unexpectedly small.")
    probe = data[:8192].decode("utf-8", errors="ignore")
    if "proxies:" not in probe and "- {" not in probe:
        raise CliError("Subscription does not look like a Clash/Mihomo YAML subscription.")
    ensure_dirs()
    atomic_write(PROVIDER_PATH, data, 0o600)
    if not quiet:
        expire = headers.get("subscription-userinfo", "")
        extra = f" ({expire})" if expire else ""
        print(f"Refreshed subscription: {len(data)} bytes{extra}")


def rules_for_mode(route_mode: str) -> list[str]:
    if route_mode == "global":
        return BASE_DIRECT_RULES + ["MATCH,VPN"]
    return BASE_DIRECT_RULES + TARGETED_RULES


def render_config_text(state: dict[str, Any]) -> str:
    mixed_port = int(state.get("mixed_port", DEFAULT_MIXED_PORT))
    controller_host = str(state.get("controller_host", DEFAULT_CONTROLLER_HOST))
    controller_port = int(state.get("controller_port", DEFAULT_CONTROLLER_PORT))
    secret = str(state.get("controller_secret", ""))
    tun_enabled = bool(state.get("tun_enabled", False))
    timeout_ms = int(state.get("auto_timeout_ms", DEFAULT_AUTO_TIMEOUT_MS))
    route_mode = str(state.get("route_mode", "targeted"))
    rules = rules_for_mode(route_mode)

    lines = [
        f"# Generated by {CLI_NAME}. Edit with {CLI_NAME} commands; manual edits may be overwritten.",
        f"mixed-port: {mixed_port}",
        "allow-lan: false",
        "bind-address: 127.0.0.1",
        "mode: rule",
        "log-level: info",
        "ipv6: false",
        "unified-delay: true",
        "tcp-concurrent: true",
        f"external-controller: {yaml_quote(controller_host + ':' + str(controller_port))}",
        f"secret: {yaml_quote(secret)}",
        "",
        "profile:",
        "  store-selected: true",
        "  store-fake-ip: true",
        "",
        "tun:",
        f"  enable: {yaml_bool(tun_enabled)}",
        "  stack: system",
        "  auto-route: true",
        "  auto-detect-interface: true",
        "  strict-route: false",
        "",
        "proxy-providers:",
        "  subscription:",
        "    type: file",
        f"    path: {yaml_quote(PROVIDER_PATH)}",
        "    health-check:",
        "      enable: true",
        "      interval: 300",
        "      lazy: false",
        "      url: https://www.gstatic.com/generate_204",
        "",
        "proxy-groups:",
        "  - name: VPN",
        "    type: select",
        "    proxies:",
        "      - AUTO",
        "      - DIRECT",
        "    use:",
        "      - subscription",
        "  - name: AUTO",
        "    type: url-test",
        "    use:",
        "      - subscription",
        "    url: https://www.gstatic.com/generate_204",
        "    interval: 300",
        f"    timeout: {timeout_ms}",
        "    tolerance: 50",
        "",
        "rules:",
    ]
    lines.extend(f"  - {rule}" for rule in rules)
    lines.append("")
    return "\n".join(lines)


def render_config(args: argparse.Namespace | None = None) -> None:
    quiet = bool(getattr(args, "quiet", False)) if args else False
    state = read_state(required=True)
    need_subscription(state)
    ensure_dirs()
    atomic_write(CONFIG_PATH, render_config_text(state), 0o600)
    if not quiet:
        print(f"Rendered {CONFIG_PATH}")


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("systemctl")
    if not exe:
        raise CliError("systemctl is not available on this machine.")
    return subprocess.run([exe, *args], text=True, check=check)


def service_is_active() -> bool:
    try:
        return systemctl("is-active", "--quiet", SERVICE_NAME, check=False).returncode == 0
    except CliError:
        return False


def maybe_restart_service() -> None:
    if service_is_active():
        systemctl("restart", SERVICE_NAME)


def cmd_setup(args: argparse.Namespace) -> None:
    state = read_state()
    state["subscription_url"] = args.subscription_url
    write_state(state)
    refresh_subscription(argparse.Namespace(quiet=args.quiet))
    render_config(argparse.Namespace(quiet=args.quiet))
    try:
        systemctl("daemon-reload")
        systemctl("enable", SERVICE_NAME)
        systemctl("restart", SERVICE_NAME)
    except CliError as exc:
        if platform.system() == "Linux":
            raise
        if not args.quiet:
            print(f"Configured locally; service control skipped: {exc}")
    if not args.quiet:
        print(f"VPN service is configured. Use '{CLI_NAME} status' and '{CLI_NAME} test'.")


def cmd_subscription(args: argparse.Namespace) -> None:
    if args.subscription_command == "set":
        state = read_state()
        state["subscription_url"] = args.subscription_url
        write_state(state)
        refresh_subscription(argparse.Namespace(quiet=args.quiet))
        render_config(argparse.Namespace(quiet=args.quiet))
        maybe_restart_service()
        if not args.quiet:
            print("Subscription updated.")
    elif args.subscription_command == "show":
        state = read_state(required=True)
        print(redact_url(str(state.get("subscription_url", ""))))
        print(f"tls_verify={tls_verify_mode(state)}")
    elif args.subscription_command == "refresh":
        refresh_subscription(args)
        render_config(argparse.Namespace(quiet=True))
        maybe_restart_service()
    elif args.subscription_command == "tls-verify":
        state = read_state()
        state["subscription_tls_verify"] = args.value
        write_state(state)
        print(f"subscription_tls_verify={state['subscription_tls_verify']}")
    else:
        raise CliError("Missing subscription subcommand.")


def cmd_mode(args: argparse.Namespace) -> None:
    state = read_state(required=True)
    if args.value is None:
        print(state.get("route_mode", "targeted"))
        return
    state["route_mode"] = args.value
    write_state(state)
    render_config(argparse.Namespace(quiet=args.quiet))
    maybe_restart_service()
    if not args.quiet:
        if args.value == "targeted":
            print("Mode set to targeted: only Google/OpenAI/Anthropic routes use VPN.")
        else:
            print("Mode set to global: outbound proxy clients route through VPN by default.")


def cmd_tun(args: argparse.Namespace) -> None:
    state = read_state(required=True)
    if args.value is None:
        print("enabled" if state.get("tun_enabled") else "disabled")
        return
    state["tun_enabled"] = args.value == "enable"
    write_state(state)
    render_config(argparse.Namespace(quiet=args.quiet))
    maybe_restart_service()
    if not args.quiet:
        if state["tun_enabled"]:
            print("TUN enabled. This changes server outbound routing; monitor hosted APIs carefully.")
        else:
            print("TUN disabled. The service is back to local proxy-only mode.")


def cmd_service(args: argparse.Namespace) -> None:
    action = args.command
    if action == "start":
        refresh_subscription(argparse.Namespace(quiet=True, if_stale=3600))
        render_config(argparse.Namespace(quiet=True))
        systemctl("start", SERVICE_NAME)
    elif action == "restart":
        refresh_subscription(argparse.Namespace(quiet=True, if_stale=3600))
        render_config(argparse.Namespace(quiet=True))
        systemctl("restart", SERVICE_NAME)
    elif action == "stop":
        systemctl("stop", SERVICE_NAME)
    elif action == "enable":
        systemctl("enable", SERVICE_NAME)
    elif action == "disable":
        systemctl("disable", SERVICE_NAME)
    elif action == "status":
        result = systemctl("status", "--no-pager", SERVICE_NAME, check=False)
        raise SystemExit(result.returncode)
    elif action == "logs":
        cmd = ["journalctl", "-u", SERVICE_NAME, "-n", str(args.lines), "--no-pager"]
        if args.follow:
            cmd.remove("--no-pager")
            cmd.append("-f")
        raise SystemExit(subprocess.call(cmd))
    else:
        raise CliError(f"Unknown service command: {action}")


def controller_base(state: dict[str, Any]) -> str:
    host = str(state.get("controller_host", DEFAULT_CONTROLLER_HOST))
    port = int(state.get("controller_port", DEFAULT_CONTROLLER_PORT))
    return f"http://{host}:{port}"


def controller_request(
    method: str,
    path: str,
    state: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> Any:
    state = state or read_state(required=True)
    url = controller_base(state) + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    secret = str(state.get("controller_secret", ""))
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CliError(f"Controller error {exc.code}: {detail}") from exc
    except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionError, TimeoutError) as exc:
        raise CliError(
            f"Cannot reach Mihomo controller. Is the service running? Try 'sudo {CLI_NAME} start'."
        ) from exc
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def get_provider_nodes(state: dict[str, Any]) -> list[str]:
    data = controller_request("GET", "/providers/proxies", state)
    providers = data.get("providers", {}) if isinstance(data, dict) else {}
    provider = providers.get("subscription")
    if provider is None and providers:
        provider = next(iter(providers.values()))
    proxies = provider.get("proxies", []) if isinstance(provider, dict) else []
    names = []
    for item in proxies:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def selected_node(state: dict[str, Any]) -> str:
    try:
        data = controller_request("GET", "/proxies/VPN", state)
        return str(data.get("now", "")) if isinstance(data, dict) else ""
    except CliError:
        return ""


def switch_node(name: str, state: dict[str, Any]) -> None:
    encoded = urllib.parse.quote("VPN", safe="")
    controller_request("PUT", f"/proxies/{encoded}", state, {"name": name})


def cmd_nodes(args: argparse.Namespace) -> None:
    state = read_state(required=True)
    nodes_command = args.nodes_command or ("interactive" if sys.stdin.isatty() else "list")
    if nodes_command == "list":
        print_nodes(state)
    elif nodes_command == "interactive":
        interactive_nodes(state, args)
    elif nodes_command == "use":
        name = resolve_node_name(args.node, state)
        switch_node(name, state)
        print(f"Switched VPN group to: {name}")
    elif nodes_command == "auto":
        cmd_auto(args)
    else:
        raise CliError("Missing nodes subcommand.")


def print_nodes(state: dict[str, Any]) -> tuple[list[str], str]:
    nodes = get_provider_nodes(state)
    current = selected_node(state)
    if not nodes:
        raise CliError(f"No nodes found. Try 'sudo {CLI_NAME} subscription refresh' or check service logs.")
    for idx, name in enumerate(nodes, 1):
        marker = "*" if name == current else " "
        print(f"{marker} {idx:03d} {name}")
    if current and current not in nodes:
        print(f"* current group selection: {current}")
    return nodes, current


def interactive_nodes(state: dict[str, Any], args: argparse.Namespace) -> None:
    nodes, _current = print_nodes(state)
    print("")
    print("输入节点编号切换；输入 a/auto 或其他任意非空内容自动选择最优节点；直接回车不更改。")
    choice = input("请选择: ").strip()
    if choice == "":
        print("未更改节点。")
        return
    if choice.isdigit():
        index = int(choice)
        if not 1 <= index <= len(nodes):
            raise CliError(f"Node number out of range: {choice}")
        switch_node(nodes[index - 1], state)
        print(f"Switched VPN group to: {nodes[index - 1]}")
        return
    auto_args = argparse.Namespace(
        products=getattr(args, "products", None),
        timeout_ms=getattr(args, "timeout_ms", None),
        workers=getattr(args, "workers", 8),
        verbose=getattr(args, "verbose", False),
    )
    cmd_auto(auto_args)


def resolve_node_name(selector: str, state: dict[str, Any]) -> str:
    if selector in {"AUTO", "DIRECT"}:
        return selector
    nodes = get_provider_nodes(state)
    if selector.isdigit():
        index = int(selector)
        if 1 <= index <= len(nodes):
            return nodes[index - 1]
    exact = [name for name in nodes if name == selector]
    if exact:
        return exact[0]
    fuzzy = [name for name in nodes if selector.lower() in name.lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        preview = "\n".join(f"  - {name}" for name in fuzzy[:10])
        raise CliError(f"Selector matches multiple nodes:\n{preview}")
    raise CliError(f"Node not found: {selector}")


def node_delay(name: str, url: str, timeout_ms: int, state: dict[str, Any]) -> int | None:
    encoded_name = urllib.parse.quote(name, safe="")
    encoded_url = urllib.parse.quote(url, safe="")
    path = f"/proxies/{encoded_name}/delay?timeout={timeout_ms}&url={encoded_url}"
    data = controller_request("GET", path, state, timeout=(timeout_ms / 1000) + 3)
    if isinstance(data, dict) and isinstance(data.get("delay"), int):
        return int(data["delay"])
    return None


def score_node(name: str, products: list[str], timeout_ms: int, state: dict[str, Any]) -> dict[str, Any]:
    delays: dict[str, int | None] = {}
    score = 0
    failures = 0
    for product in products:
        url = PRODUCTS[product]["url"]
        try:
            delay = node_delay(name, url, timeout_ms, state)
        except CliError:
            delay = None
        delays[product] = delay
        if delay is None:
            failures += 1
            score += timeout_ms * 2
        else:
            score += delay
    return {"name": name, "score": score, "failures": failures, "delays": delays}


def cmd_auto(args: argparse.Namespace) -> None:
    state = read_state(required=True)
    nodes = get_provider_nodes(state)
    if not nodes:
        raise CliError(f"No nodes found. Try 'sudo {CLI_NAME} subscription refresh' or check service logs.")
    products = args.products or list(state.get("auto_products", ["google", "openai", "anthropic"]))
    for product in products:
        if product not in PRODUCTS:
            raise CliError(f"Unknown product: {product}")
    timeout_ms = int(args.timeout_ms or state.get("auto_timeout_ms", DEFAULT_AUTO_TIMEOUT_MS))
    workers = max(1, int(args.workers))
    print(f"Testing {len(nodes)} nodes against {', '.join(products)}...")
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(score_node, node, products, timeout_ms, state) for node in nodes]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            if args.verbose:
                compact = ", ".join(
                    f"{key}={value if value is not None else 'fail'}ms"
                    for key, value in result["delays"].items()
                )
                print(f"{result['name']}: {compact}")
    viable = [item for item in results if item["failures"] == 0]
    ranked = sorted(viable or results, key=lambda item: (item["failures"], item["score"], item["name"]))
    best = ranked[0]
    switch_node(str(best["name"]), state)
    print("Top candidates:")
    for item in ranked[: min(10, len(ranked))]:
        compact = " ".join(
            f"{key}:{value if value is not None else 'fail'}"
            for key, value in item["delays"].items()
        )
        print(f"  {item['score']:>6}  {item['name']}  {compact}")
    print(f"Selected best node: {best['name']}")


def proxy_url(state: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{int(state.get('mixed_port', DEFAULT_MIXED_PORT))}"


def proxy_env(state: dict[str, Any]) -> dict[str, str]:
    url = proxy_url(state)
    return {
        "http_proxy": url,
        "https_proxy": url,
        "all_proxy": url,
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "ALL_PROXY": url,
        "no_proxy": NO_PROXY_VALUE,
        "NO_PROXY": NO_PROXY_VALUE,
    }


def cmd_env(args: argparse.Namespace) -> None:
    state = read_state(required=True)
    env = proxy_env(state)
    if args.shell == "fish":
        for key in ["http_proxy", "https_proxy", "all_proxy", "no_proxy"]:
            print(f"set -gx {key} {env[key]};")
    else:
        for key in ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "no_proxy", "NO_PROXY"]:
            print(f"export {key}={sh_quote(env[key])}")


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def target_access_group() -> str:
    explicit = os.environ.get("VPNCLI_ACCESS_GROUP") or os.environ.get("VPNCTL_ACCESS_GROUP")
    if explicit:
        return explicit
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        user_entry = pwd.getpwnam(sudo_user)
        return grp.getgrgid(user_entry.pw_gid).gr_name
    return "root"


def chmod_if_exists(path: Path, mode: int) -> None:
    try:
        if path.exists():
            os.chmod(path, mode)
    except PermissionError as exc:
        raise CliError(f"Permission denied changing mode for {path}; run with sudo.") from exc


def chgrp_if_exists(path: Path, gid: int) -> None:
    try:
        if path.exists():
            os.chown(path, -1, gid)
    except PermissionError as exc:
        raise CliError(f"Permission denied changing group for {path}; run with sudo.") from exc


def cmd_fix_permissions(args: argparse.Namespace) -> None:
    group_name = target_access_group()
    try:
        gid = grp.getgrnam(group_name).gr_gid
    except KeyError as exc:
        raise CliError(f"Group does not exist: {group_name}") from exc

    dirs = [ETC_DIR, VAR_DIR, PROVIDER_PATH.parent]
    files = [STATE_PATH, CONFIG_PATH, PROVIDER_PATH]

    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
        chgrp_if_exists(directory, gid)
        chmod_if_exists(directory, 0o2750)
    for file_path in files:
        if file_path.exists():
            chgrp_if_exists(file_path, gid)
            chmod_if_exists(file_path, 0o640)

    if not getattr(args, "quiet", False):
        print(f"Permissions fixed for group: {group_name}")


def cmd_run(args: argparse.Namespace) -> None:
    if args.argv and args.argv[0] == "--":
        args.argv = args.argv[1:]
    if not args.argv:
        raise CliError(f"Usage: {CLI_NAME} run -- <command> [args...]")
    state = read_state(required=True)
    env = os.environ.copy()
    env.update(proxy_env(state))
    raise SystemExit(subprocess.call(args.argv, env=env))


def product_health(product: str, state: dict[str, Any], timeout: float) -> tuple[str, bool, float, str]:
    url = PRODUCTS[product]["url"]
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url(state), "https": proxy_url(state)}),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),  # noqa: SLF001
    )
    req = urllib.request.Request(url, headers={"User-Agent": "VPN-On-Linux-Server/1.0"})
    started = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(512)
            elapsed = (time.perf_counter() - started) * 1000
            return product, True, elapsed, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        if exc.code < 500:
            return product, True, elapsed, f"HTTP {exc.code}"
        return product, False, elapsed, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - user-facing connectivity report.
        elapsed = (time.perf_counter() - started) * 1000
        return product, False, elapsed, str(exc)


def cmd_test(args: argparse.Namespace) -> None:
    state = read_state(required=True)
    products = args.products or ["google", "openai", "anthropic"]
    failed = False
    for product in products:
        if product not in PRODUCTS:
            raise CliError(f"Unknown product: {product}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(products)) as pool:
        futures = [pool.submit(product_health, product, state, args.timeout) for product in products]
        for future in concurrent.futures.as_completed(futures):
            product, ok, elapsed, detail = future.result()
            label = PRODUCTS[product]["label"]
            status = "ok" if ok else "fail"
            print(f"{label:<10} {status:<4} {elapsed:>7.0f} ms  {detail}")
            failed = failed or not ok
    raise SystemExit(1 if failed else 0)


def cmd_open(args: argparse.Namespace) -> None:
    state = read_state(required=True)
    product = PRODUCTS[args.product]
    url = product["home"]
    opener = shutil.which("xdg-open")
    if opener and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        env = os.environ.copy()
        env.update(proxy_env(state))
        subprocess.Popen([opener, url], env=env)
        print(f"Opened {product['label']}: {url}")
    else:
        print(url)
        print(f"Use through proxy: {proxy_url(state)}")


def cmd_info(args: argparse.Namespace) -> None:
    state = read_state(required=False)
    print(f"config: {CONFIG_PATH}")
    print(f"state: {STATE_PATH}")
    print(f"provider: {PROVIDER_PATH}")
    print(f"subscription: {redact_url(str(state.get('subscription_url', '')))}")
    print(f"mixed_proxy: {proxy_url(state)}")
    print(f"controller: {controller_base(state)}")
    print(f"route_mode: {state.get('route_mode', 'targeted')}")
    print(f"tun: {'enabled' if state.get('tun_enabled') else 'disabled'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=CLI_NAME,
        description="Manage a Mihomo-powered VPN/proxy service for Linux servers.",
    )
    sub = parser.add_subparsers(dest="command")

    setup = sub.add_parser("setup", help="configure subscription, enable autostart, and start service")
    setup.add_argument("subscription_url")
    setup.add_argument("--quiet", action="store_true")
    setup.set_defaults(func=cmd_setup)

    subscription = sub.add_parser("subscription", help="manage subscription URL")
    subscription_sub = subscription.add_subparsers(dest="subscription_command")
    subscription_set = subscription_sub.add_parser("set", help="set subscription URL")
    subscription_set.add_argument("subscription_url")
    subscription_set.add_argument("--quiet", action="store_true")
    subscription_set.set_defaults(func=cmd_subscription)
    subscription_show = subscription_sub.add_parser("show", help="show redacted subscription URL")
    subscription_show.set_defaults(func=cmd_subscription)
    subscription_refresh = subscription_sub.add_parser("refresh", help="download latest subscription")
    subscription_refresh.add_argument("--quiet", action="store_true")
    subscription_refresh.add_argument(
        "--if-stale",
        type=int,
        metavar="SECONDS",
        help="skip refresh when the cached subscription is newer than SECONDS",
    )
    subscription_refresh.set_defaults(func=cmd_subscription)
    subscription_tls = subscription_sub.add_parser("tls-verify", help="set subscription TLS verification")
    subscription_tls.add_argument("value", choices=["on", "off", "auto"])
    subscription_tls.set_defaults(func=cmd_subscription)

    render = sub.add_parser("render", help="render Mihomo config")
    render.add_argument("--quiet", action="store_true")
    render.set_defaults(func=render_config)

    mode = sub.add_parser("mode", help="show or set routing mode")
    mode.add_argument("value", nargs="?", choices=["targeted", "global"])
    mode.add_argument("--quiet", action="store_true")
    mode.set_defaults(func=cmd_mode)

    tun = sub.add_parser("tun", help="show or set TUN transparent proxy mode")
    tun.add_argument("value", nargs="?", choices=["enable", "disable"])
    tun.add_argument("--quiet", action="store_true")
    tun.set_defaults(func=cmd_tun)

    nodes = sub.add_parser("nodes", help="list, switch, or auto-select nodes")
    nodes_sub = nodes.add_subparsers(dest="nodes_command")
    nodes_list = nodes_sub.add_parser("list", help="list nodes")
    nodes_list.set_defaults(func=cmd_nodes)
    nodes_use = nodes_sub.add_parser("use", help="switch to a node by index/name/fuzzy text")
    nodes_use.add_argument("node")
    nodes_use.set_defaults(func=cmd_nodes)
    nodes_auto = nodes_sub.add_parser("auto", help="test nodes and switch to the best one")
    add_auto_args(nodes_auto)
    nodes_auto.set_defaults(func=cmd_nodes)

    auto = sub.add_parser("auto", help="alias for 'nodes auto'")
    add_auto_args(auto)
    auto.set_defaults(func=cmd_auto)

    test = sub.add_parser("test", help="test active proxy access to target products")
    test.add_argument("products", nargs="*", choices=sorted(PRODUCTS.keys()))
    test.add_argument("--timeout", type=float, default=8.0)
    test.set_defaults(func=cmd_test)

    env = sub.add_parser("env", help="print shell proxy environment exports")
    env.add_argument("--shell", choices=["sh", "fish"], default="sh")
    env.set_defaults(func=cmd_env)

    run = sub.add_parser("run", help="run a command with proxy environment variables")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    open_cmd = sub.add_parser("open", help="open a product URL through the proxy when GUI is available")
    open_cmd.add_argument("product", choices=sorted(PRODUCTS.keys()))
    open_cmd.set_defaults(func=cmd_open)

    info = sub.add_parser("info", help="show local configuration paths and mode")
    info.set_defaults(func=cmd_info)

    fix_permissions = sub.add_parser("fix-permissions", help="allow the sudo caller to use read-only commands")
    fix_permissions.add_argument("--quiet", action="store_true")
    fix_permissions.set_defaults(func=cmd_fix_permissions)

    logs = sub.add_parser("logs", help="show service logs")
    logs.add_argument("-n", "--lines", type=int, default=100)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=cmd_service)

    for command in ["start", "stop", "restart", "enable", "disable", "status"]:
        svc = sub.add_parser(command, help=f"systemd {command} for {SERVICE_NAME}")
        svc.set_defaults(func=cmd_service)

    return parser


def add_auto_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--products", nargs="+", choices=sorted(PRODUCTS.keys()))
    parser.add_argument("--timeout-ms", type=int)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--verbose", action="store_true")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        args.func(args)
        return 0
    except BrokenPipeError:
        return 1
    except CliError as exc:
        print(f"{CLI_NAME}: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        cmd = " ".join(str(part) for part in exc.cmd)
        print(f"{CLI_NAME}: command failed ({exc.returncode}): {cmd}", file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
