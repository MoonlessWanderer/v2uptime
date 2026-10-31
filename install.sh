#!/usr/bin/env bash
# v2uptime — installer and management menu (Ubuntu 22.04+ / Debian 12+, run as root)
#
#   curl -fsSL https://raw.githubusercontent.com/MoonlessWanderer/v2uptime/main/install.sh | sudo -E bash
#   curl -fsSL .../install.sh | sudo -E bash -s -- --proxy socks5h://1.2.3.4:1080
#   sudo bash install.sh --from-archive ./v2uptime-1.0.0-linux-amd64.tar.gz   # fully offline
#
# After installation the menu is available as:  sudo v2uptime
set -Eeuo pipefail

# ---------------------------------------------------------------- settings
REPO_SLUG="${V2UPTIME_REPO:-MoonlessWanderer/v2uptime}"   # GitHub "owner/repo" of this project
BRANCH="${V2UPTIME_BRANCH:-main}"
INSTALL_DIR="/opt/v2uptime"
SERVICE="v2uptime"
APP_USER="v2uptime"
CMD_PATH="/usr/local/bin/v2uptime"
NGINX_SITE="/etc/nginx/sites-available/v2uptime.conf"
NGINX_LINK="/etc/nginx/sites-enabled/v2uptime.conf"
STATE_FILE="$INSTALL_DIR/data/install-state"
BACKUP_DIR="/root/v2uptime-backups"
# Update from the admin panel: the panel (service user) only creates CONTROL_REQUEST; the root unit
# ${SERVICE}-control.path sees it and runs "install.sh --control". Results go to CONTROL_DIR
# (owned by root, readable by the panel).
CONTROL_REQUEST="$INSTALL_DIR/data/control-request"
CONTROL_DIR="$INSTALL_DIR/control"
FONT_URLS=(
  "https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/fonts/webfonts/Vazirmatn%5Bwght%5D.woff2"
  "https://raw.githubusercontent.com/rastikerdar/vazirmatn/v33.003/fonts/webfonts/Vazirmatn%5Bwght%5D.woff2"
)

PROXY="${V2UPTIME_PROXY:-}"
FROM_ARCHIVE=""
PIP_MIRROR=""
APT_MIRROR=""
XRAY_ZIP=""
ACTION=""
TMP_ROOT=""

# ------------------------------------------------------------------ output
if [[ -t 1 ]]; then
  RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BLUE=$'\e[34m'; BOLD=$'\e[1m'; NC=$'\e[0m'
else
  RED=""; GREEN=""; YELLOW=""; BLUE=""; BOLD=""; NC=""
fi
# All messages go to stderr so $(...) captures only real return values.
info() { printf '%s[i]%s %s\n' "$BLUE" "$NC" "$*" >&2; }
ok()   { printf '%s[✓]%s %s\n' "$GREEN" "$NC" "$*" >&2; }
warn() { printf '%s[!]%s %s\n' "$YELLOW" "$NC" "$*" >&2; }
die()  { printf '%s[✗] %s%s\n' "$RED" "$*" "$NC" >&2; exit 1; }
title() { printf '\n%s%s== %s ==%s\n' "$BOLD" "$BLUE" "$*" "$NC" >&2; }

cleanup() { [[ -n "$TMP_ROOT" && -d "$TMP_ROOT" ]] && rm -rf "$TMP_ROOT"; return 0; }
trap cleanup EXIT
trap 'die "Unexpected error on line $LINENO (exit code $?)."' ERR

tmpdir() { mktemp -d "$TMP_ROOT/XXXX"; }  # TMP_ROOT is created in main()

run_step() {  # run a menu action with errexit in a subshell; a failure never kills the menu
  local rc
  set +e
  ( set -e; "$@" )
  rc=$?
  set -e
  (( rc == 0 )) || warn "Action failed (exit code $rc)."
  return 0
}

# The script usually runs from a pipe, so every answer is read from /dev/tty.
ask() {  # ask "Prompt" [default] -> stdout
  local prompt="$1" default="${2:-}" answer=""
  if [[ -n "$default" ]]; then prompt="$prompt [$default]"; fi
  read -r -p "$prompt: " answer < /dev/tty || true
  printf '%s' "${answer:-$default}"
}
ask_secret() {
  local answer=""
  read -r -s -p "$1: " answer < /dev/tty || true
  printf '\n' > /dev/tty
  printf '%s' "$answer"
}
confirm() {  # confirm "Question" [y|n]
  local default="${2:-n}" answer
  answer="$(ask "$1 (y/n)" "$default")"
  [[ "$answer" =~ ^[Yy] ]]
}
pause() { read -r -p "Press Enter…" _ < /dev/tty || true; }

usage() {
  cat <<EOF
v2uptime installer / manager

Options:
  --install                 run the installation directly (no menu)
  --proxy URL               proxy for git, pip, apt and downloads (socks5h://IP:PORT or http://IP:PORT)
  --from-archive FILE       offline install/update from a release archive (source + wheels + Xray)
  --pip-mirror URL          PyPI mirror index URL (e.g. https://mirror-pypi.runflare.com/simple)
  --apt-mirror URL          temporary APT mirror base (e.g. https://mirror.arvancloud.ir)
  --xray-zip FILE           install Xray from a local Xray-linux-*.zip
  -h, --help                show this help

Commands:
  probe ID [--count N]      time cold and warm requests through server ID (see README, Troubleshooting)
  --setup-control           set up the root unit behind Settings → Maintenance → Update panel
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install) ACTION="install"; shift ;;
      --proxy) PROXY="${2:?--proxy needs a value}"; shift 2 ;;
      --from-archive) FROM_ARCHIVE="$(readlink -f "${2:?--from-archive needs a file}")"; shift 2 ;;
      --pip-mirror) PIP_MIRROR="${2:?--pip-mirror needs a URL}"; shift 2 ;;
      --apt-mirror) APT_MIRROR="${2%/}"; shift 2 ;;
      --xray-zip) XRAY_ZIP="$(readlink -f "${2:?--xray-zip needs a file}")"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1 (see --help)" ;;
    esac
  done
  [[ -z "$FROM_ARCHIVE" || -f "$FROM_ARCHIVE" ]] || die "Archive not found: $FROM_ARCHIVE"
  [[ -z "$XRAY_ZIP" || -f "$XRAY_ZIP" ]] || die "Xray zip not found: $XRAY_ZIP"
  # Respect http(s)_proxy passed through `sudo -E`.
  if [[ -z "$PROXY" ]]; then PROXY="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}"; fi
  if [[ -n "$PROXY" ]]; then info "Using proxy: $PROXY"; fi
}

# ------------------------------------------------------------------ checks
require_root() { [[ $EUID -eq 0 ]] || die "Run as root (sudo)."; }

check_os() {
  [[ -r /etc/os-release ]] || die "/etc/os-release not found; unsupported system."
  # shellcheck disable=SC1091
  . /etc/os-release
  local major="${VERSION_ID%%.*}"
  case "${ID:-}" in
    ubuntu) (( major >= 22 )) || die "Ubuntu 22.04 or newer is required (found $VERSION_ID)." ;;
    debian) (( major >= 12 )) || die "Debian 12 or newer is required (found $VERSION_ID; Debian 11 ships Python 3.9)." ;;
    *) die "Unsupported OS: ${PRETTY_NAME:-unknown}. Use Ubuntu 22.04+ or Debian 12+." ;;
  esac
  ok "OS: $PRETTY_NAME"
}

check_python() {
  command -v python3 >/dev/null || die "python3 not found."
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10+ is required (found $(python3 -V 2>&1))."
  python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is missing (apt-get install python3-venv)."
  ok "$(python3 -V)"
}

is_installed() { [[ -f "$INSTALL_DIR/main.py" && -f "$INSTALL_DIR/config.py" ]]; }

# ------------------------------------------------------------ state file
load_state() {
  DOMAIN=""; EMAIL=""; PORT="8080"; SSL_MODE=""; CERT_PATH=""; KEY_PATH=""
  if [[ -f "$STATE_FILE" ]]; then
    # shellcheck disable=SC1090
    . "$STATE_FILE"
  fi
  if [[ -f "$INSTALL_DIR/config.py" ]]; then
    PORT="$(sed -n 's/^PORT *= *\([0-9]\+\).*/\1/p' "$INSTALL_DIR/config.py" | head -n1)"
    PORT="${PORT:-8080}"
  fi
}

save_state() {
  mkdir -p "$(dirname "$STATE_FILE")"
  {
    printf 'DOMAIN=%q\nEMAIL=%q\nSSL_MODE=%q\nCERT_PATH=%q\nKEY_PATH=%q\n' \
      "$DOMAIN" "$EMAIL" "$SSL_MODE" "$CERT_PATH" "$KEY_PATH"
  } > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
}

# --------------------------------------------------------------- network
proxy_is_socks() { [[ "$1" == socks* ]]; }

panel_proxy() {  # SOCKS URL of a healthy monitored server (needs a running panel)
  load_state
  curl -fsS --max-time 4 --noproxy '*' "http://127.0.0.1:${PORT}/internal/proxy?format=text" 2>/dev/null | head -n1 || true
}

fetch() {  # fetch URL OUTFILE — direct (or --proxy), then through a healthy monitored server
  local url="$1" out="$2" p
  if [[ -n "$PROXY" ]]; then
    curl -fL --retry 2 --connect-timeout 20 --proxy "$PROXY" -o "$out" "$url" && return 0
  else
    curl -fL --retry 1 --connect-timeout 15 --noproxy '*' -o "$out" "$url" && return 0
  fi
  p="$(panel_proxy)"
  if [[ -n "$p" ]]; then
    warn "Direct download failed; retrying through monitored server ($p)…"
    curl -fL --retry 2 --connect-timeout 20 --proxy "$p" -o "$out" "$url" && return 0
  fi
  return 1
}

apt_opts() {
  local opts=(-y -q -o Dpkg::Options::=--force-confold)
  if [[ -n "$PROXY" ]]; then
    opts+=(-o "Acquire::http::Proxy=$PROXY" -o "Acquire::https::Proxy=$PROXY")
  fi
  printf '%s\n' "${opts[@]}"
}

with_apt_mirror() {  # run a command with distribution mirrors temporarily replaced
  if [[ -z "$APT_MIRROR" ]]; then "$@"; return; fi
  local backup files f
  backup="$(tmpdir)"
  files=$(ls /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true)
  for f in $files; do cp -a "$f" "$backup/$(echo "$f" | tr / _)"; done
  info "Using APT mirror $APT_MIRROR"
  for f in $files; do
    sed -i -E \
      -e "s#https?://([a-z]{2}\.)?archive\.ubuntu\.com/ubuntu#${APT_MIRROR}/ubuntu#g" \
      -e "s#https?://security\.ubuntu\.com/ubuntu#${APT_MIRROR}/ubuntu#g" \
      -e "s#https?://deb\.debian\.org/debian-security#${APT_MIRROR}/debian-security#g" \
      -e "s#https?://security\.debian\.org/debian-security#${APT_MIRROR}/debian-security#g" \
      -e "s#https?://deb\.debian\.org/debian#${APT_MIRROR}/debian#g" "$f"
  done
  local rc=0
  "$@" || rc=$?
  for f in $files; do cp -a "$backup/$(echo "$f" | tr / _)" "$f"; done
  return $rc
}

install_packages() {
  title "System packages"
  local opts
  mapfile -t opts < <(apt_opts)
  export DEBIAN_FRONTEND=noninteractive
  with_apt_mirror bash -c "apt-get ${opts[*]} update && apt-get ${opts[*]} install \
    python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx unzip curl ca-certificates openssl" \
    || die "Package installation failed. Use --apt-mirror or --proxy if the default mirrors are unreachable."
  ok "Packages installed"
}

# ------------------------------------------------------------ app files
create_user() {
  if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$APP_USER"
    ok "System user $APP_USER created"
  fi
}

get_source() {  # prints the path of an extracted source tree
  local dir
  dir="$(tmpdir)"
  if [[ -n "$FROM_ARCHIVE" ]]; then
    tar -xzf "$FROM_ARCHIVE" -C "$dir"
  else
    fetch "https://codeload.github.com/${REPO_SLUG}/tar.gz/refs/heads/${BRANCH}" "$dir/src.tar.gz" \
      || die "Could not download the source. Use --proxy URL or --from-archive FILE."
    tar -xzf "$dir/src.tar.gz" -C "$dir" && rm -f "$dir/src.tar.gz"
  fi
  local root
  root="$(find "$dir" -maxdepth 2 -name main.py -printf '%h\n' | head -n1)"
  [[ -n "$root" && -d "$root/app" ]] || die "Source archive does not contain main.py and app/."
  printf '%s' "$root"
}

# Files owned by the installation and never replaced by an update.
PRESERVE=(config.py data logs bin venv .staging .rollback wheels xray control)

copy_source() {  # copy_source SRC DEST
  local src="$1" dest="$2" item name skip p
  mkdir -p "$dest"
  shopt -s dotglob nullglob
  for item in "$src"/*; do
    name="$(basename "$item")"
    skip=0
    for p in "${PRESERVE[@]}" .git; do [[ "$name" == "$p" ]] && skip=1; done
    (( skip )) && continue
    rm -rf "${dest:?}/$name"
    cp -a "$item" "$dest/$name"
  done
  shopt -u dotglob nullglob
}

pip_bootstrap_socks() {  # pip needs PySocks for SOCKS proxies: fetch the wheel with curl first
  local venv="$1" proxy="$2" d url
  proxy_is_socks "$proxy" || return 0
  "$venv/bin/python" -c 'import socks' 2>/dev/null && return 0
  d="$(tmpdir)"
  url="$(curl -fsSL --proxy "$proxy" https://pypi.org/pypi/PySocks/1.7.1/json \
    | python3 -c 'import json,sys; print([u["url"] for u in json.load(sys.stdin)["urls"] if u["filename"].endswith(".whl")][0])')"
  curl -fsSL --proxy "$proxy" -o "$d/PySocks-1.7.1-py3-none-any.whl" "$url"
  "$venv/bin/python" -m pip install --disable-pip-version-check -q "$d/PySocks-1.7.1-py3-none-any.whl"
}

pip_install() {  # pip_install VENV REQUIREMENTS [WHEELS_DIR]
  local venv="$1" req="$2" wheels="${3:-}" p
  # Always "python -m pip": bin/pip has an absolute shebang that breaks when a venv is moved.
  local pip=("$venv/bin/python" -m pip install --disable-pip-version-check -q)
  if [[ -n "$wheels" && -d "$wheels" ]]; then
    "${pip[@]}" --no-index --find-links "$wheels" -r "$req"
    return
  fi
  if [[ -n "$PIP_MIRROR" ]]; then
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      "${pip[@]}" --index-url "$PIP_MIRROR" -r "$req" && return 0
    warn "pip mirror failed; trying other routes"
  fi
  if [[ -n "$PROXY" ]]; then
    pip_bootstrap_socks "$venv" "$PROXY" && "${pip[@]}" --proxy "$PROXY" -r "$req" && return 0
  else
    "${pip[@]}" -r "$req" && return 0
  fi
  p="$(panel_proxy)"
  [[ -n "$p" ]] || return 1
  warn "Retrying pip through monitored server ($p)…"
  pip_bootstrap_socks "$venv" "$p" && "${pip[@]}" --proxy "$p" -r "$req"
}

make_venv() {  # make_venv DIR SRC_ROOT
  local venv="$1" src="$2"
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip --version >/dev/null
  pip_install "$venv" "$src/requirements.txt" "$src/wheels" \
    || die "Installing Python packages failed. Try --pip-mirror URL, --proxy URL or --from-archive FILE."
}

fetch_fonts() {
  local target="$INSTALL_DIR/app/static/fonts/Vazirmatn-Variable.woff2" url
  [[ -s "$target" ]] && return 0
  mkdir -p "$(dirname "$target")"
  for url in "${FONT_URLS[@]}"; do
    if fetch "$url" "$target" 2>/dev/null && [[ -s "$target" ]]; then ok "Vazirmatn font installed"; return 0; fi
  done
  rm -f "$target"
  warn "Vazirmatn font could not be downloaded; the page falls back to system fonts."
}

write_config() {
  local cfg="$INSTALL_DIR/config.py"
  if [[ -f "$cfg" ]]; then
    info "Keeping existing config.py"
    return
  fi
  local secret
  secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  cat > "$cfg" <<EOF
# v2uptime startup configuration — written by install.sh, preserved by updates.
# Runtime settings (interval, Telegram, ...) live in the database: admin panel → Settings.
HOST = "127.0.0.1"
PORT = ${PORT}
DB_PATH = "data/uptime.db"
SECRET_KEY = "${secret}"
XRAY_BIN = "bin/xray"
XRAY_API_PORT = 10085
SOCKS_PORT_BASE = 20000
LOG_DIR = "logs"
LOG_LEVEL = "INFO"
COOKIE_SECURE = True
EOF
  ok "config.py written"
}

set_config_value() {  # set_config_value KEY VALUE (VALUE is a Python literal)
  local cfg="$INSTALL_DIR/config.py"
  if grep -q "^$1 *=" "$cfg"; then
    sed -i "s|^$1 *=.*|$1 = $2|" "$cfg"
  else
    printf '%s = %s\n' "$1" "$2" >> "$cfg"
  fi
}

fix_permissions() {
  mkdir -p "$INSTALL_DIR"/{data,logs,bin}
  chown -R root:root "$INSTALL_DIR"
  chown -R "$APP_USER:$APP_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
  chmod 755 "$INSTALL_DIR"
  chown root:"$APP_USER" "$INSTALL_DIR/config.py"
  chmod 640 "$INSTALL_DIR/config.py"
  [[ -f "$INSTALL_DIR/bin/xray" ]] && chmod 755 "$INSTALL_DIR/bin/xray"
  chmod 755 "$INSTALL_DIR/install.sh" 2>/dev/null || true
  if [[ -d "$CONTROL_DIR" ]]; then
    chown -R root:"$APP_USER" "$CONTROL_DIR"
    chmod 750 "$CONTROL_DIR"
    find "$CONTROL_DIR" -type f -exec chmod 640 {} +
  fi
}

installed_version() { sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INSTALL_DIR/app/__init__.py" 2>/dev/null; }

app_cmd() {  # run a main.py sub-command as the service user
  ( cd "$INSTALL_DIR" && runuser -u "$APP_USER" -- "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/main.py" "$@" )
}

# ------------------------------------------------------------------- Xray
xray_asset() {
  case "$(uname -m)" in
    x86_64|amd64) echo "Xray-linux-64.zip" ;;
    aarch64|arm64) echo "Xray-linux-arm64-v8a.zip" ;;
    armv7l|armv7) echo "Xray-linux-arm32-v7a.zip" ;;
    *) die "Unsupported CPU architecture: $(uname -m)" ;;
  esac
}

install_xray_from_zip() {
  local zip="$1" dir
  dir="$(tmpdir)"
  unzip -o -q "$zip" -d "$dir" || die "Invalid Xray zip: $zip"
  [[ -f "$dir/xray" ]] || die "The zip does not contain the xray binary."
  "$dir/xray" version >/dev/null 2>&1 || die "This Xray binary does not run on this machine (wrong architecture?)."
  mkdir -p "$INSTALL_DIR/bin"
  install -m 0755 "$dir/xray" "$INSTALL_DIR/bin/xray.new"
  mv -f "$INSTALL_DIR/bin/xray.new" "$INSTALL_DIR/bin/xray"
  for f in geoip.dat geosite.dat; do [[ -f "$dir/$f" ]] && install -m 0644 "$dir/$f" "$INSTALL_DIR/bin/$f"; done
  ok "Xray installed: $("$INSTALL_DIR/bin/xray" version | head -n1)"
}

install_xray() {
  title "Xray-core"
  local zip src_zip
  if [[ -n "$XRAY_ZIP" ]]; then
    install_xray_from_zip "$XRAY_ZIP"; return
  fi
  if [[ -n "$FROM_ARCHIVE" ]]; then
    src_zip="$(find "${1:-/nonexistent}" -maxdepth 2 -path '*xray*' -name "$(xray_asset)" 2>/dev/null | head -n1 || true)"
    if [[ -n "$src_zip" ]]; then install_xray_from_zip "$src_zip"; return; fi
    warn "The archive has no $(xray_asset); trying to download it."
  fi
  local asset dir url expected actual
  asset="$(xray_asset)"
  dir="$(tmpdir)"
  url="https://github.com/XTLS/Xray-core/releases/latest/download/$asset"
  info "Downloading $asset …"
  if ! fetch "$url" "$dir/$asset"; then
    warn "Could not download Xray from GitHub."
    warn "Copy $asset to the server and run: sudo v2uptime  →  'Update Xray core' (or install.sh --xray-zip FILE)."
    return 1
  fi
  if fetch "$url.dgst" "$dir/$asset.dgst" 2>/dev/null; then
    expected="$(awk -F'= ' '/^SHA2-256/ {print $2}' "$dir/$asset.dgst" | tr -d '[:space:]')"
    actual="$(sha256sum "$dir/$asset" | awk '{print $1}')"
    [[ -z "$expected" || "$expected" == "$actual" ]] || die "Xray checksum mismatch!"
    [[ -n "$expected" ]] && ok "Checksum verified"
  else
    warn "Checksum file unavailable; skipping verification."
  fi
  install_xray_from_zip "$dir/$asset"
}

# ------------------------------------------------------------------ nginx
nginx_proxy_block() {
  cat <<EOF
    client_max_body_size 25m;
    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;

    # Localhost-only endpoint used by this script; never exposed.
    location /internal/ { return 404; }

    location /static/ {
        alias ${INSTALL_DIR}/app/static/;
        expires 7d;
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 90s;
    }
EOF
}

write_nginx_http() {  # plain HTTP (used before the certificate exists, or HTTP-only mode)
  cat > "$NGINX_SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
$(nginx_proxy_block)
}
EOF
  ln -sf "$NGINX_SITE" "$NGINX_LINK"
  nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx configuration test failed."; }
  systemctl reload nginx || systemctl restart nginx
}

write_nginx_https() {
  cat > "$NGINX_SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name ${DOMAIN};

    ssl_certificate ${CERT_PATH};
    ssl_certificate_key ${KEY_PATH};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:v2uptime:10m;
    ssl_session_timeout 1d;
    add_header Strict-Transport-Security "max-age=31536000" always;

$(nginx_proxy_block)
}
EOF
  ln -sf "$NGINX_SITE" "$NGINX_LINK"
  nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx configuration test failed."; }
  systemctl reload nginx || systemctl restart nginx
  ok "nginx: https://${DOMAIN} → 127.0.0.1:${PORT}"
}

certbot_issue() {
  local args=(certonly --non-interactive --agree-tos -d "$DOMAIN" --keep-until-expiring
              --deploy-hook "systemctl reload nginx")
  if [[ -n "$EMAIL" ]]; then args+=(-m "$EMAIL"); else args+=(--register-unsafely-without-email); fi
  info "Requesting a Let's Encrypt certificate for $DOMAIN (nginx plugin)…"
  if certbot "${args[@]}" --nginx; then return 0; fi
  warn "nginx plugin failed; retrying with webroot…"
  mkdir -p /var/www/html
  certbot "${args[@]}" --webroot -w /var/www/html
}

use_existing_cert() {
  local cert key
  cert="$(ask "Path to fullchain.pem")"
  key="$(ask "Path to privkey.pem")"
  [[ -f "$cert" && -f "$key" ]] || { warn "File not found."; return 1; }
  if [[ "$(openssl x509 -in "$cert" -noout -pubkey 2>/dev/null)" != "$(openssl pkey -in "$key" -pubout 2>/dev/null)" ]]; then
    warn "Certificate and private key do not match."; return 1
  fi
  CERT_PATH="$cert"; KEY_PATH="$key"; SSL_MODE="custom"
}

setup_ssl() {
  title "Domain and SSL"
  write_nginx_http
  while true; do
    if certbot_issue; then
      CERT_PATH="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
      KEY_PATH="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"
      SSL_MODE="letsencrypt"
      systemctl enable --now certbot.timer >/dev/null 2>&1 || true
      break
    fi
    warn "Getting the certificate failed. Common causes:"
    warn "  • DNS of ${DOMAIN} does not point to this server"
    warn "  • port 80 is closed by a firewall / provider"
    warn "  • Let's Encrypt is unreachable from this server (try --proxy)"
    echo "  1) Retry   2) Use an existing certificate   3) Continue with HTTP only (not recommended)"
    case "$(ask "Choice" "1")" in
      2) use_existing_cert && break ;;
      3) SSL_MODE="none"; break ;;
      *) ;;
    esac
  done
  if [[ "$SSL_MODE" == "none" ]]; then
    set_config_value COOKIE_SECURE False
    warn "HTTP only: the admin login travels unencrypted. Re-run 'Domain and SSL' from the menu later."
  else
    set_config_value COOKIE_SECURE True
    write_nginx_https
  fi
  save_state
}

# ---------------------------------------------------------------- systemd
write_service() {
  cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=v2uptime — V2Ray/Xray uptime monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/main.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/data ${INSTALL_DIR}/logs
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
}

write_control_units() {  # root helper behind Settings → Maintenance → Update panel
  rm -f "$CONTROL_REQUEST"  # never act on a request left from before
  cat > "/etc/systemd/system/${SERVICE}-control.path" <<EOF
[Unit]
Description=v2uptime — watch for update requests from the admin panel

[Path]
PathExists=${CONTROL_REQUEST}
Unit=${SERVICE}-control.service

[Install]
WantedBy=multi-user.target
EOF
  cat > "/etc/systemd/system/${SERVICE}-control.service" <<EOF
[Unit]
Description=v2uptime — run an update requested from the admin panel

[Service]
Type=oneshot
ExecStart=${INSTALL_DIR}/install.sh --control
Environment=V2UPTIME_REPO=${REPO_SLUG} V2UPTIME_BRANCH=${BRANCH}
TimeoutStartSec=45min
EOF
  systemctl daemon-reload
  systemctl enable --now "${SERVICE}-control.path" >/dev/null 2>&1
  mkdir -p "$CONTROL_DIR"
  printf '{"repo": %s, "branch": %s}\n' "$(json_str "$REPO_SLUG")" "$(json_str "$BRANCH")" > "$CONTROL_DIR/ready"
  fix_permissions
}

json_str() {  # JSON string literal
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "${s//$'\n'/ }"
}

control_status() {  # control_status STATE STARTED FINISHED MESSAGE — written atomically for the panel
  local tmp="$CONTROL_DIR/status.json.tmp"
  printf '{"action": "update", "state": %s, "started_at": %s, "finished_at": %s, "version": %s, "message": %s}\n' \
    "$(json_str "$1")" "$2" "${3:-null}" "$(json_str "$(installed_version)")" "$(json_str "$4")" > "$tmp"
  chown root:"$APP_USER" "$tmp"
  chmod 640 "$tmp"
  mv -f "$tmp" "$CONTROL_DIR/status.json"
}

run_control() {  # started by ${SERVICE}-control.service when the panel has created CONTROL_REQUEST
  local action="" started rc=0 log="$CONTROL_DIR/last.log"
  # The request file belongs to the service user: read at most a few bytes, never follow a link,
  # and accept only the one known word. Nothing in it can change what is downloaded or run.
  if [[ -f "$CONTROL_REQUEST" && ! -L "$CONTROL_REQUEST" ]]; then
    action="$(head -c 16 "$CONTROL_REQUEST" | tr -cd 'a-z')"
  fi
  rm -f "$CONTROL_REQUEST"
  if [[ "$action" != "update" ]]; then
    warn "Ignoring an unknown request from the panel."
    return 0
  fi
  mkdir -p "$CONTROL_DIR"
  chown root:"$APP_USER" "$CONTROL_DIR"
  chmod 750 "$CONTROL_DIR"
  started="$(date +%s)"
  : > "$log"
  chown root:"$APP_USER" "$log"
  chmod 640 "$log"
  control_status running "$started" "" "Updating from version $(installed_version)…"
  ( do_update ) >> "$log" 2>&1 || rc=$?
  if (( rc == 0 )); then
    control_status ok "$started" "$(date +%s)" "Updated to version $(installed_version)."
  else
    control_status failed "$started" "$(date +%s)" "The update failed; version $(installed_version) is running. See the log."
  fi
  return 0
}

wait_healthy() {
  local i
  for i in $(seq 1 40); do
    if curl -fsS --max-time 2 --noproxy '*' "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

install_command() {
  ln -sf "$INSTALL_DIR/install.sh" "$CMD_PATH"
  chmod 755 "$INSTALL_DIR/install.sh"
  ok "Management menu: sudo v2uptime"
}

# ----------------------------------------------------------------- install
create_admin() {
  local user pass pass2
  if [[ -n "$(app_cmd_admin_exists)" ]]; then
    info "An admin user already exists (use 'Reset admin password' to change it)."
    return
  fi
  user="$(ask "Admin username" "admin")"
  while true; do
    pass="$(ask_secret "Admin password (min 10 characters)")"
    pass2="$(ask_secret "Repeat password")"
    [[ "$pass" == "$pass2" ]] || { warn "Passwords do not match."; continue; }
    (( ${#pass} >= 10 )) || { warn "At least 10 characters."; continue; }
    break
  done
  printf '%s\n%s\n' "$pass" "$pass2" | app_cmd create-admin -u "$user" --password-stdin
}

app_cmd_admin_exists() {
  ( cd "$INSTALL_DIR" && runuser -u "$APP_USER" -- "$INSTALL_DIR/venv/bin/python" -c \
    'from app.cli import _open_db; _open_db(); from app.models import Admin; print("y" if Admin.select().exists() else "")' ) 2>/dev/null
}

do_install() {
  title "v2uptime installation"
  check_os
  load_state
  if is_installed; then
    warn "v2uptime is already installed in $INSTALL_DIR."
    confirm "Re-run the installation (data, config.py and the admin are kept)?" || return 0
  fi
  install_packages
  check_python

  DOMAIN="$(ask "Domain (e.g. status.example.com)" "${DOMAIN:-}")"
  [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "Invalid domain: $DOMAIN"
  EMAIL="$(ask "Email for the certificate (optional)" "${EMAIL:-}")"
  PORT="$(ask "Internal port (127.0.0.1)" "${PORT:-8080}")"
  [[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT > 1023 && PORT < 65536 )) || die "Invalid port."

  title "Application"
  create_user
  local src
  src="$(get_source)"
  mkdir -p "$INSTALL_DIR"
  copy_source "$src" "$INSTALL_DIR"
  if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    info "Creating virtualenv and installing requirements…"
    make_venv "$INSTALL_DIR/venv" "$src"
  else
    pip_install "$INSTALL_DIR/venv" "$INSTALL_DIR/requirements.txt" "$src/wheels" || die "pip install failed."
  fi
  ok "Python environment ready"
  write_config
  set_config_value PORT "$PORT"
  fetch_fonts || true  # release archives already ship it in app/static/fonts
  install_xray "$src" || warn "Continuing without Xray; checks start once it is installed."
  fix_permissions

  title "Admin"
  local current_path admin_path
  current_path="$(app_cmd get-setting admin_path 2>/dev/null || echo /admin)"
  if [[ "$current_path" == "/admin" ]]; then
    local suggested
    suggested="/panel-$(head -c 64 /dev/urandom | tr -dc 'a-z0-9' | head -c 8)"
    while true; do
      admin_path="$(ask "Admin panel path (hard to guess)" "$suggested")"
      app_cmd set-setting admin_path "$admin_path" >/dev/null && break
      warn "Invalid path: use letters, digits, - and _ (e.g. $suggested)."
    done
  fi
  # Print what was actually stored (the app normalises e.g. "ln" to "/ln").
  admin_path="$(app_cmd get-setting admin_path)"
  create_admin

  write_service
  write_control_units
  setup_ssl
  fix_permissions
  systemctl restart "$SERVICE"
  install_command
  if wait_healthy; then ok "Service is running"; else warn "Service did not answer yet; check: journalctl -u $SERVICE -n 50"; fi

  local scheme="https"
  [[ "$SSL_MODE" == "none" ]] && scheme="http"
  title "Done"
  echo "  Status page : ${scheme}://${DOMAIN}/"
  echo "  Admin panel : ${scheme}://${DOMAIN}${admin_path}"
  echo "  Menu        : sudo v2uptime"
}

# ------------------------------------------------------------------ update
do_update() {
  is_installed || die "Not installed."
  title "Update v2uptime"
  load_state
  local stage="$INSTALL_DIR/.staging" rollback="$INSTALL_DIR/.rollback" src
  rm -rf "$stage" && mkdir -p "$stage"
  src="$(get_source)"
  local new_version old_version
  new_version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$src/app/__init__.py")"
  old_version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INSTALL_DIR/app/__init__.py")"
  info "Installed: $old_version   New: $new_version"

  info "Preparing a new virtualenv in staging…"
  make_venv "$stage/venv" "$src"

  info "Backing up the current version…"
  rm -rf "$rollback" && mkdir -p "$rollback/files"
  copy_source "$INSTALL_DIR" "$rollback/files"
  app_cmd backup-db -o "$rollback/uptime.db" >/dev/null 2>&1 || cp -a "$INSTALL_DIR/data/uptime.db" "$rollback/" 2>/dev/null || true

  systemctl stop "$SERVICE"
  copy_source "$src" "$INSTALL_DIR"
  mv "$INSTALL_DIR/venv" "$rollback/venv"
  mv "$stage/venv" "$INSTALL_DIR/venv"
  fix_permissions
  systemctl start "$SERVICE"
  if wait_healthy; then
    rm -rf "$stage" "$rollback"
    # The new install.sh refreshes the panel-update helper units it ships with.
    "$INSTALL_DIR/install.sh" --setup-control || warn "Could not refresh the panel update helper."
    ok "Updated to $new_version"
    return 0
  fi
  warn "The new version did not start — rolling back."
  systemctl stop "$SERVICE" || true
  copy_source "$rollback/files" "$INSTALL_DIR"
  rm -rf "$INSTALL_DIR/venv" && mv "$rollback/venv" "$INSTALL_DIR/venv"
  fix_permissions
  systemctl start "$SERVICE"
  wait_healthy && warn "Rolled back to $old_version." || die "Rollback did not start either; see journalctl -u $SERVICE."
  return 1
}

do_update_xray() {
  is_installed || die "Not installed."
  local before
  before="$("$INSTALL_DIR/bin/xray" version 2>/dev/null | head -n1 || echo none)"
  if [[ -z "$XRAY_ZIP" ]]; then
    local path
    path="$(ask "Local Xray zip path (empty = download latest)" "")"
    [[ -n "$path" ]] && XRAY_ZIP="$(readlink -f "$path")"
  fi
  FROM_ARCHIVE=""  # always fetch Xray itself here
  install_xray || return 1
  fix_permissions
  systemctl restart "$SERVICE"
  ok "Before: $before"
  ok "After : $("$INSTALL_DIR/bin/xray" version | head -n1)"
  XRAY_ZIP=""
}

# ------------------------------------------------------------------- menus
service_menu() {
  echo "  1) Start   2) Stop   3) Restart   0) Back"
  case "$(ask "Choice" "0")" in
    1) systemctl start "$SERVICE" && ok "Started" ;;
    2) systemctl stop "$SERVICE" && ok "Stopped" ;;
    3) systemctl restart "$SERVICE" && ok "Restarted" ;;
    *) ;;
  esac
}

show_logs() {
  info "Ctrl+C to return to the menu"
  trap ':' INT
  journalctl -u "$SERVICE" -n 100 -f || true
  trap - INT
}

ssl_menu() {
  load_state
  echo "  Domain: ${DOMAIN:-—}   SSL: ${SSL_MODE:-—}"
  echo "  1) Re-issue certificate   2) Change domain   3) Use existing certificate   4) Remove domain/SSL   0) Back"
  case "$(ask "Choice" "0")" in
    1) [[ -n "$DOMAIN" ]] || die "No domain configured."
       certbot renew --cert-name "$DOMAIN" --force-renewal --deploy-hook "systemctl reload nginx" \
         && ok "Certificate renewed" || setup_ssl ;;
    2) local old="$DOMAIN"
       DOMAIN="$(ask "New domain")"
       [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "Invalid domain."
       EMAIL="$(ask "Email (optional)" "$EMAIL")"
       setup_ssl
       if [[ -n "$old" && "$old" != "$DOMAIN" ]] && confirm "Delete the old certificate of $old?"; then
         certbot delete --cert-name "$old" -n >/dev/null 2>&1 || true
       fi ;;
    3) use_existing_cert && { set_config_value COOKIE_SECURE True; write_nginx_https; save_state; systemctl restart "$SERVICE"; } ;;
    4) confirm "Remove the nginx site and certificate? The panel will only be reachable on 127.0.0.1" || return 0
       rm -f "$NGINX_LINK" "$NGINX_SITE"; systemctl reload nginx || true
       [[ "$SSL_MODE" == "letsencrypt" && -n "$DOMAIN" ]] && certbot delete --cert-name "$DOMAIN" -n || true
       DOMAIN=""; SSL_MODE=""; CERT_PATH=""; KEY_PATH=""; save_state; ok "Removed" ;;
    *) ;;
  esac
}

change_port() {
  load_state
  local new
  new="$(ask "New internal port" "$PORT")"
  [[ "$new" =~ ^[0-9]+$ ]] && (( new > 1023 && new < 65536 )) || die "Invalid port."
  if [[ "$new" != "$PORT" ]] && ss -ltn "( sport = :$new )" | grep -q LISTEN; then die "Port $new is in use."; fi
  set_config_value PORT "$new"
  PORT="$new"
  if [[ -f "$NGINX_SITE" ]]; then
    sed -i -E "s#proxy_pass http://127\.0\.0\.1:[0-9]+;#proxy_pass http://127.0.0.1:${PORT};#" "$NGINX_SITE"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx
  fi
  systemctl restart "$SERVICE"
  wait_healthy && ok "Now listening on 127.0.0.1:$PORT" || warn "Service not healthy; see logs."
}

show_status() {
  load_state
  systemctl --no-pager status "$SERVICE" | head -n 12 || true
  echo
  echo "Version : $(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INSTALL_DIR/app/__init__.py" 2>/dev/null)"
  echo "Xray    : $("$INSTALL_DIR/bin/xray" version 2>/dev/null | head -n1 || echo 'not installed')"
  echo "Domain  : ${DOMAIN:-—} (SSL: ${SSL_MODE:-—})"
  echo "Listen  : 127.0.0.1:${PORT}"
  echo "Admin   : $(app_cmd get-setting admin_path 2>/dev/null || echo '?')"
  echo -n "Health  : "
  curl -fsS --max-time 3 --noproxy '*' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo "not responding"
  echo
  local proxy
  proxy="$(panel_proxy)"
  echo "Healthy proxy for updates: ${proxy:-none}"
}

backup_menu() {
  echo "  1) Backup database   2) Restore database   0) Back"
  case "$(ask "Choice" "0")" in
    1) mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
       local name
       name="$BACKUP_DIR/v2uptime-$(date -u +%Y%m%dT%H%M%SZ)"
       app_cmd backup-db -o "/tmp/v2uptime-backup.db" >/dev/null
       mv /tmp/v2uptime-backup.db "$name.db"
       cp -a "$INSTALL_DIR/config.py" "$name.config.py"
       ok "Saved: $name.db (+ config.py)" ;;
    2) local files=() i choice
       mapfile -t files < <(ls -1t "$BACKUP_DIR"/*.db 2>/dev/null || true)
       (( ${#files[@]} )) || { warn "No backups in $BACKUP_DIR"; return; }
       for i in "${!files[@]}"; do echo "  $((i + 1))) ${files[$i]}"; done
       choice="$(ask "Backup number" "1")"
       [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#files[@]} )) || die "Invalid choice."
       confirm "Replace the current database with ${files[$((choice - 1))]}?" || return 0
       systemctl stop "$SERVICE"
       cp -a "$INSTALL_DIR/data/uptime.db" "$INSTALL_DIR/data/uptime.db.before-restore" 2>/dev/null || true
       rm -f "$INSTALL_DIR/data/uptime.db-wal" "$INSTALL_DIR/data/uptime.db-shm"
       install -m 0640 -o "$APP_USER" -g "$APP_USER" "${files[$((choice - 1))]}" "$INSTALL_DIR/data/uptime.db"
       systemctl start "$SERVICE"
       ok "Restored (previous database kept as data/uptime.db.before-restore)" ;;
    *) ;;
  esac
}

reset_password() {
  local user pass pass2
  user="$(ask "Admin username" "admin")"
  pass="$(ask_secret "New password (min 10 characters)")"
  pass2="$(ask_secret "Repeat password")"
  [[ "$pass" == "$pass2" ]] || die "Passwords do not match."
  printf '%s\n%s\n' "$pass" "$pass2" | app_cmd reset-password -u "$user" --password-stdin
}

uninstall() {
  load_state
  warn "This removes the service, ALL files, the database, the nginx site and the certificate."
  confirm "Continue?" || return 0
  [[ "$(ask "Type DELETE to confirm")" == "DELETE" ]] || { info "Cancelled."; return 0; }
  systemctl disable --now "$SERVICE" "${SERVICE}-control.path" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/${SERVICE}.service" "/etc/systemd/system/${SERVICE}-control.path" \
    "/etc/systemd/system/${SERVICE}-control.service"
  systemctl daemon-reload
  rm -f "$NGINX_LINK" "$NGINX_SITE"; systemctl reload nginx >/dev/null 2>&1 || true
  if [[ "${SSL_MODE:-}" == "letsencrypt" && -n "${DOMAIN:-}" ]]; then certbot delete --cert-name "$DOMAIN" -n >/dev/null 2>&1 || true; fi
  rm -f "$CMD_PATH"
  rm -rf "$INSTALL_DIR"
  id "$APP_USER" >/dev/null 2>&1 && userdel "$APP_USER" || true
  ok "v2uptime removed. Backups in $BACKUP_DIR were kept."
  exit 0
}

menu() {
  while true; do
    load_state
    local state="not installed"
    if is_installed; then
      if systemctl is-active --quiet "$SERVICE"; then state="${GREEN}running${NC}"; else state="${RED}stopped${NC}"; fi
    fi
    printf '\n%s v2uptime %s — %b  %s\n' "$BOLD" "$NC" "$state" "${DOMAIN:+($DOMAIN)}"
    cat <<'EOF'
  1) Install
  2) Update
  3) Update Xray core
  4) Start / Stop / Restart
  5) View logs
  6) Domain and SSL
  7) Change port
  8) Service status
  9) Backup and restore database
 10) Reset admin password
 11) Uninstall
  0) Exit
EOF
    local choice
    choice="$(ask "Choice" "0")"
    case "$choice" in
      1) run_step do_install ;;
      2) run_step do_update ;;
      3) run_step do_update_xray ;;
      4) run_step service_menu ;;
      5) show_logs ;;
      6) run_step ssl_menu ;;
      7) run_step change_port ;;
      8) run_step show_status ;;
      9) run_step backup_menu ;;
      10) run_step reset_password ;;
      11) uninstall ;;
      0|q|exit) exit 0 ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

main() {
  if [[ "${1:-}" == "probe" ]]; then  # sudo v2uptime probe ID [--count N]
    shift
    require_root
    is_installed || die "v2uptime is not installed."
    app_cmd probe "$@" || exit $?
    exit 0
  fi
  if [[ "${1:-}" == "--control" || "${1:-}" == "--setup-control" ]]; then  # used by systemd and by updates
    require_root
    is_installed || die "v2uptime is not installed."
    TMP_ROOT="$(mktemp -d /tmp/v2uptime.XXXXXX)"
    if [[ "$1" == "--control" ]]; then run_control; else write_control_units; fi
    exit 0
  fi
  parse_args "$@"
  require_root
  TMP_ROOT="$(mktemp -d /tmp/v2uptime.XXXXXX)"
  if is_installed && [[ ! -f "$CONTROL_DIR/ready" ]]; then
    write_control_units && ok "Update from the admin panel is enabled (Settings → Maintenance)."
  fi
  if [[ -n "$PROXY" ]]; then
    export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" ALL_PROXY="$PROXY"
    export no_proxy="127.0.0.1,localhost" NO_PROXY="127.0.0.1,localhost"
  fi
  if [[ "$ACTION" == "install" ]] || ! is_installed; then
    if [[ "$ACTION" != "install" ]] && ! confirm "v2uptime is not installed. Install now?" "y"; then
      menu
    fi
    do_install
    exit 0
  fi
  menu
}

main "$@"
