# v2uptime startup configuration (copy to config.py; install.sh writes it for you).
# Only values needed before the database opens live here. Everything else
# (interval, timeout, Telegram, site title, admin path, ...) is edited in the
# admin panel → Settings and stored in the database.

HOST = "127.0.0.1"           # always behind nginx; never 0.0.0.0
PORT = 8080
DB_PATH = "data/uptime.db"   # relative to the project directory
SECRET_KEY = ""              # empty: a random key is created in data/secret_key
XRAY_BIN = "bin/xray"
XRAY_API_PORT = 10085        # Xray gRPC API (HandlerService / RoutingService) on 127.0.0.1
SOCKS_PORT_BASE = 20000      # local SOCKS inbounds: one stable port per server from here
LOG_DIR = "logs"             # JSON lines, rotated (10 MB × 5)
LOG_LEVEL = "INFO"
COOKIE_SECURE = True         # session cookie only over HTTPS (False for plain-http dev)
