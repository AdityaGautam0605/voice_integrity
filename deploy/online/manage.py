#!/usr/bin/env python3
"""VPS deployment helper. Uses Python's standard library; never sources shell env."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)


def load_config(path):
    config = {"TURN_PORT": "3478", "TURN_TLS_PORT": "5349"}
    allowed = {"DEMO_HOST", "TURN_HOST", "PUBLIC_IPV4", "RELAY_IPV4", "CERTBOT_EMAIL", "TURN_SECRET", "TURN_PORT", "TURN_TLS_PORT"}
    if not path.is_file():
        raise ValueError("Missing .env. Copy .env.example to .env and configure it first.")
    if path.stat().st_mode & 0o077:
        raise ValueError("The .env file contains secrets; run chmod 600 on it.")
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name.strip() not in allowed:
            raise ValueError(f"Unsupported .env entry on line {number}.")
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        config[name.strip()] = value
    for key in allowed - {"RELAY_IPV4"}:
        if not config.get(key):
            raise ValueError(f"Missing {key} in .env.")
    for key in ("DEMO_HOST", "TURN_HOST"):
        if not DOMAIN.fullmatch(config[key]) or config[key].endswith(".example.com"):
            raise ValueError(f"{key} must be your actual public DNS name.")
    if config["DEMO_HOST"] == config["TURN_HOST"]:
        raise ValueError("Use separate DEMO_HOST and TURN_HOST DNS names.")
    public_ip = ipaddress.IPv4Address(config["PUBLIC_IPV4"])
    if not public_ip.is_global:
        raise ValueError("PUBLIC_IPV4 must be the VPS public IPv4 address.")
    config.setdefault("RELAY_IPV4", str(public_ip))
    relay_ip = ipaddress.IPv4Address(config["RELAY_IPV4"])
    if relay_ip.is_loopback or relay_ip.is_unspecified or relay_ip.is_multicast:
        raise ValueError("RELAY_IPV4 must be an IPv4 address assigned to a VPS interface.")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{32,}", config["TURN_SECRET"]) or config["TURN_SECRET"].startswith("replace-"):
        raise ValueError("TURN_SECRET must be a random secret of at least 32 letters, digits, hyphens or underscores.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", config["CERTBOT_EMAIL"]) or config["CERTBOT_EMAIL"].endswith("@example.com"):
        raise ValueError("CERTBOT_EMAIL must be your email address.")
    for key in ("TURN_PORT", "TURN_TLS_PORT"):
        if not config[key].isdigit() or not 1024 <= int(config[key]) <= 65535 or int(config[key]) in range(49160, 49261):
            raise ValueError(f"{key} must be an unprivileged port outside the relay range.")
    if config["TURN_PORT"] == config["TURN_TLS_PORT"]:
        raise ValueError("TURN listener ports must differ.")
    return config


def render(template, config):
    def replace(match):
        return config[match.group(1)]
    return re.sub(r"@([A-Z0-9_]+)@", replace, template)


def compose(*args, capture=False):
    # Do not hand our secret .env to Compose interpolation or child environments.
    command = ["docker", "compose", "--env-file", "/dev/null", "-f", str(ROOT / "compose.yaml"), *args]
    return subprocess.run(command, cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def validate_static():
    static = ROOT / "static"
    if not (static / "index.html").is_file():
        raise ValueError("Missing static/index.html. Upload only the frontend dist/ contents to static/.")
    blocked = {".pem", ".key", ".p12", ".crt", ".env", ".py", ".sqlite", ".db"}
    for path in static.rglob("*"):
        if path.is_symlink() or path.name.startswith(".") or path.suffix.lower() in blocked:
            raise ValueError(f"Unexpected private file or symlink under static/: {path.relative_to(static)}")


def check_dns(config):
    for name in (config["DEMO_HOST"], config["TURN_HOST"]):
        addresses = {item[4][0] for item in socket.getaddrinfo(name, None)}
        if addresses != {config["PUBLIC_IPV4"]}:
            raise ValueError(f"DNS for {name} must resolve only to PUBLIC_IPV4; remove DNS proxying and unsupported AAAA records.")
    print("DNS points directly to the configured VPS IPv4.")


def check_port(port, kind=socket.SOCK_STREAM, address="0.0.0.0"):
    with socket.socket(socket.AF_INET, kind) as listener:
        try:
            listener.bind((address, port))
        except OSError as error:
            transport = "TCP" if kind == socket.SOCK_STREAM else "UDP"
            raise ValueError(f"Cannot bind {address}:{port}/{transport}: {error.strerror}. Existing services are never stopped automatically.") from error


def preflight(config, initial=False, require_backend=False):
    if sys.platform != "linux":
        raise ValueError("Run deployment commands on the Linux VPS. Compose uses host networking.")
    if os.geteuid() != 0:
        raise ValueError("Run the VPS helper with sudo; it manages port 80 and restricted certificate files.")
    if not shutil.which("docker"):
        raise ValueError("Install Docker Engine with the Compose plugin first.")
    compose("version", capture=True)
    compose("config", "--quiet", capture=True)
    subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=True, stdout=subprocess.DEVNULL)
    validate_static()
    check_dns(config)
    if initial:
        for port in (80, 443, int(config["TURN_PORT"]), int(config["TURN_TLS_PORT"])):
            check_port(port)
        for port in [int(config["TURN_PORT"]), *range(49160, 49261)]:
            check_port(port, socket.SOCK_DGRAM)
        check_port(0, socket.SOCK_DGRAM, config["RELAY_IPV4"])
        print("Website/TURN ports are free; relay address belongs to this VPS.")
    try:
        with urllib.request.urlopen("http://127.0.0.1:18000/health", timeout=5) as response:
            health = json.load(response)
        if (not isinstance(health, dict) or health.get("online_mode") is not True
                or health.get("public_origin") != f'https://{config["DEMO_HOST"].lower()}'):
            raise ValueError("The tunnel must serve the online backend for this public origin.")
        print("Laptop backend is reachable through loopback port 18000.")
    except (OSError, ValueError) as error:
        if require_backend:
            raise ValueError("Laptop backend is unreachable on port 18000. Start ./scripts/demo.sh --online on the laptop.") from error
        try:
            check_port(18000, address="127.0.0.1")
        except ValueError as port_error:
            raise ValueError("VPS port 18000 is occupied but did not answer backend /health; resolve the conflict first.") from port_error
        print("Laptop backend is not connected yet; port 18000 is free. Run check after starting the laptop launcher.")


def write_atomic(path, text, mode=0o640):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text)
    temporary.chmod(mode)
    temporary.replace(path)


def copy_certificates():
    live = ROOT / "runtime/letsencrypt/live/voice-integrity"
    tls = ROOT / "runtime/tls"
    for name in ("fullchain.pem", "privkey.pem"):
        source = live / name
        if not source.is_file():
            raise ValueError("Certificate issuance did not produce the expected certificate files.")
        target = tls / name
        write_atomic(target, source.read_text())
        os.chown(target, 0, 65534)
    tls.chmod(0o750)
    os.chown(tls, 0, 65534)


def config_fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def initialize(config, resume=False):
    runtime = ROOT / "runtime"
    state = runtime / "state.json"
    if state.exists():
        old = json.loads(state.read_text())
        if not resume or old.get("phase") != "bootstrap" or old.get("fingerprint") != config_fingerprint(config):
            raise ValueError("Deployment state already exists. Use up for an initialized stack; use init --resume only for an unchanged, interrupted bootstrap.")
    elif runtime.exists():
        raise ValueError("runtime/ already exists without known deployment state. Review it before proceeding; nothing was overwritten.")
    preflight(config, initial=True)
    for name in ("", "nginx", "tls", "acme", "letsencrypt"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    write_atomic(state, json.dumps({"phase": "bootstrap", "fingerprint": config_fingerprint(config)}), 0o600)
    write_atomic(runtime / "nginx/nginx.conf", render((ROOT / "bootstrap.conf.template").read_text(), config), 0o644)
    write_atomic(runtime / "turnserver.conf", render((ROOT / "turnserver.conf.template").read_text(), config))
    os.chown(runtime / "turnserver.conf", 0, 65534)
    completed = False
    try:
        compose("pull")
        compose("up", "-d", "nginx")
        for attempt in range(30):
            try:
                with socket.create_connection(("127.0.0.1", 80), timeout=1):
                    break
            except OSError:
                if attempt == 29:
                    raise ValueError("Nginx did not start on port 80. Inspect Compose logs.")
                time.sleep(1)
        compose("run", "--rm", "certbot", "certonly", "--non-interactive", "--agree-tos", "--webroot", "-w", "/var/www/acme",
                "--cert-name", "voice-integrity", "--email", config["CERTBOT_EMAIL"], "-d", config["DEMO_HOST"], "-d", config["TURN_HOST"])
        copy_certificates()
        write_atomic(runtime / "nginx/nginx.conf", render((ROOT / "nginx.conf.template").read_text(), config), 0o644)
        compose("exec", "-T", "nginx", "nginx", "-t", "-c", "/etc/nginx/voice-integrity/nginx.conf")
        compose("exec", "-T", "nginx", "nginx", "-s", "reload", "-c", "/etc/nginx/voice-integrity/nginx.conf")
        compose("up", "-d", "coturn")
        write_atomic(state, json.dumps({"phase": "ready", "fingerprint": config_fingerprint(config)}), 0o600)
        completed = True
    finally:
        if not completed:
            compose("stop", "nginx", "coturn")
    print("VPS initialized. Start the laptop launcher, then run manage.py check.")


def require_initialized(config):
    state = ROOT / "runtime/state.json"
    if not state.exists() or json.loads(state.read_text()).get("phase") != "ready":
        raise ValueError("Initialize this deployment first with manage.py init.")
    if json.loads(state.read_text()).get("fingerprint") != config_fingerprint(config):
        raise ValueError("The .env configuration changed after initialization. Restore it before using this helper; generated configs are not silently overwritten.")


def renew(dry_run=False):
    arguments = ["run", "--rm", "certbot", "renew", "--cert-name", "voice-integrity", "--non-interactive", "--webroot", "-w", "/var/www/acme"]
    if dry_run:
        compose(*arguments, "--dry-run")
        print("Certificate renewal dry run passed; live certificates and services were not changed.")
        return
    certificate = ROOT / "runtime/letsencrypt/live/voice-integrity/fullchain.pem"
    before = certificate.read_bytes()
    compose(*arguments)
    if before == certificate.read_bytes():
        print("No certificate renewal was due; services remain running.")
        return
    copy_certificates()
    compose("exec", "-T", "nginx", "nginx", "-t", "-c", "/etc/nginx/voice-integrity/nginx.conf")
    compose("exec", "-T", "nginx", "nginx", "-s", "reload", "-c", "/etc/nginx/voice-integrity/nginx.conf")
    compose("restart", "coturn")
    print("Certificates renewed; Nginx reloaded and coturn restarted. Existing relay calls were interrupted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "preflight", "init", "up", "check", "renew"))
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted initialization with unchanged configuration")
    parser.add_argument("--dry-run", action="store_true", help="Test renewal using the ACME staging service")
    args = parser.parse_args()
    config = load_config(ROOT / ".env")
    if args.action == "validate":
        for name in ("nginx", "bootstrap", "turnserver"):
            render((ROOT / f"{name}.conf.template").read_text(), config)
        print("Configuration and template substitutions are valid. No DNS, Docker or certificate checks were run.")
        return
    if args.action == "init":
        initialize(config, args.resume)
        return
    if args.action == "preflight":
        preflight(config, initial=True)
        return
    require_initialized(config)
    preflight(config, require_backend=args.action == "check")
    if args.action == "up":
        compose("up", "-d", "nginx", "coturn")
    elif args.action == "renew":
        renew(args.dry_run)
    elif args.action == "check":
        compose("ps")
        compose("exec", "-T", "nginx", "nginx", "-t", "-c", "/etc/nginx/voice-integrity/nginx.conf")
        with urllib.request.urlopen(f'https://{config["DEMO_HOST"]}/api/health', timeout=10) as response:
            health = json.load(response)
            if (response.status != 200 or not isinstance(health, dict)
                    or health.get("online_mode") is not True
                    or health.get("public_origin") != f'https://{config["DEMO_HOST"].lower()}'):
                raise ValueError("Public HTTPS backend health check failed.")
        subprocess.run(["openssl", "x509", "-in", str(ROOT / "runtime/tls/fullchain.pem"), "-noout", "-checkend", "604800"], check=True)
        print("Public HTTPS, backend tunnel and certificate checks passed. Verify TURN with a forced-relay browser call.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        # Config/command errors deliberately omit environment values and secret config content.
        print(f"Deployment failed: {error}", file=sys.stderr)
        sys.exit(1)
