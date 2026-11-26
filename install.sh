#!/bin/bash
# Mi Cine — instalador para Ultra.cc
# Uso (por SSH en tu Ultra.cc):
#   bash <(curl -fsSL https://raw.githubusercontent.com/richtopro/mi-cine/main/server/install.sh)
set -e

REPO="https://github.com/richtopro/mi-cine"
DIR="$HOME/micine"
CONF="$DIR/server/config.json"
UNIT="$HOME/.config/systemd/user/micine.service"
NGINX="$HOME/.apps/nginx/proxy.d/micine.conf"

echo "=== Instalando Mi Cine ==="

# 1) Python 3.8 o superior
PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "Necesito Python 3.8 o superior. En Ultra.cc puedes instalarlo con:  app-pyenv install"
  exit 1
fi
echo "Python: $("$PY" --version)"

# 2) Descargar / actualizar el código
if git -C "$DIR" rev-parse >/dev/null 2>&1; then
  git -C "$DIR" pull -q --ff-only && echo "Código actualizado."
elif [ -f "$DIR/server/micine.py" ]; then
  echo "Usando los archivos de $DIR"
else
  git clone -q --depth 1 "$REPO" "$DIR"
  echo "Código descargado en $DIR"
fi

# 2b) ffmpeg (para el audio AC3/DTS que los navegadores no reproducen)
export PATH="$HOME/bin:$PATH"
if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  echo "ffmpeg: $(command -v ffmpeg)"
else
  echo "Descargando ffmpeg (una sola vez, ~40 MB)…"
  mkdir -p "$HOME/bin"; TMPD="$(mktemp -d)"
  if curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar xJ -C "$TMPD"; then
    cp "$TMPD"/ffmpeg-*/ffmpeg "$TMPD"/ffmpeg-*/ffprobe "$HOME/bin/" && chmod +x "$HOME/bin/ffmpeg" "$HOME/bin/ffprobe"
    echo "ffmpeg instalado en ~/bin"
  else
    echo "⚠️  No se pudo descargar ffmpeg. Los vídeos con audio AC3/DTS no sonarán en el navegador."
  fi
  rm -rf "$TMPD"
fi

# 3) Configuración (solo la primera vez)
if [ ! -f "$CONF" ]; then
  echo
  echo "Puertos libres de tu servicio:"
  app-ports free 2>/dev/null | head -5 || true
  read -rp "Puerto para Mi Cine (uno de la lista de arriba): " PORT
  [[ "$PORT" =~ ^[0-9]{4,5}$ ]] || { echo "Puerto no válido"; exit 1; }

  echo
  echo "Carpetas de tu home:"; ls -d "$HOME"/*/ 2>/dev/null | sed "s|$HOME/|  ~/|"
  read -rp "Carpeta con tus pelis y series [~/media]: " MEDIA
  MEDIA="${MEDIA:-~/media}"

  read -rsp "Pega tu clave de TMDB (no se verá al escribir): " TMDB; echo

  "$PY" - "$CONF" "$PORT" "$MEDIA" "$TMDB" <<'PYEOF'
import json, sys
conf, port, media, tmdb = sys.argv[1:5]
json.dump({
  "host": "127.0.0.1", "port": int(port), "base_url": "/micine",
  "media": [{"name": "Ultra.cc", "path": media}],
  "tmdb_key": tmdb.strip(), "data_dir": "~/.micine",
  "scan_interval_min": 30, "min_size_mb": 15, "session_days": 90, "cookie_secure": True
}, open(conf, "w"), indent=2)
PYEOF
  chmod 600 "$CONF"
  echo "Configuración guardada en $CONF"
else
  PORT="$("$PY" -c "import json;print(json.load(open('$CONF'))['port'])")"
  echo "Usando la configuración existente (puerto $PORT)."
fi

# 4) Administrador (solo si no hay usuarios)
if [ -z "$(MICINE_CONFIG="$CONF" "$PY" "$DIR/server/micine.py" users)" ]; then
  echo
  read -rp "Nombre de tu usuario administrador: " ADMIN
  MICINE_CONFIG="$CONF" "$PY" "$DIR/server/micine.py" adduser "$ADMIN" --admin
fi

# 5) Servicio (arranca solo y se reinicia si falla)
mkdir -p "$(dirname "$UNIT")" "$HOME/.micine"
cat > "$UNIT" <<EOF
[Unit]
Description=Mi Cine
After=network-online.target

[Service]
Type=exec
Environment=MICINE_CONFIG=$CONF
ExecStart=$PY $DIR/server/micine.py serve
Restart=on-failure
RestartSec=5
StandardOutput=append:$HOME/.micine/micine.log
StandardError=append:$HOME/.micine/micine.log

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now micine.service
systemctl --user restart micine.service

# 6) Publicarlo por HTTPS con el nginx de Ultra.cc
mkdir -p "$(dirname "$NGINX")"
cat > "$NGINX" <<EOF
location /micine/ {
    proxy_pass              http://127.0.0.1:$PORT;
    proxy_http_version      1.1;
    proxy_set_header        X-Forwarded-Host        \$http_host;
    proxy_set_header        X-Forwarded-For         \$remote_addr;
    proxy_buffering         off;
    proxy_request_buffering off;
    proxy_read_timeout      3600s;
    client_max_body_size    5m;
}
EOF
app-nginx restart >/dev/null 2>&1 || echo "Reinicia el servidor web desde el panel de Ultra.cc (UCP)."

sleep 2
if curl -fs "http://127.0.0.1:$PORT/micine/api/health" >/dev/null; then
  echo
  echo "✅ Mi Cine está funcionando."
  echo "   Ábrelo en:  https://$(whoami).$(hostname -s).usbx.me/micine/"
  echo "   El primer escaneo y la búsqueda de carátulas tardan un rato; verás el progreso en el Panel de administración."
else
  echo "⚠️  El servicio no responde todavía. Mira el registro con:  tail -n 50 ~/.micine/micine.log"
fi
