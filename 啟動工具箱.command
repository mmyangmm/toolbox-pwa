#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
TOOLS_DIR="$SCRIPT_DIR/.local-tools"
YT_DLP_BIN="$TOOLS_DIR/yt-dlp"
DOWNLOAD_BIN="$TOOLS_DIR/yt-dlp.download"
APP_URL="http://127.0.0.1:8767"

mkdir -p "$TOOLS_DIR"

if curl --silent --fail "$APP_URL/api/health" >/dev/null 2>&1; then
  open "$APP_URL"
  exit 0
fi

NEEDS_DOWNLOAD=false
if [[ ! -x "$YT_DLP_BIN" ]]; then
  NEEDS_DOWNLOAD=true
elif [[ -n "$(find "$YT_DLP_BIN" -mtime +7 -print -quit 2>/dev/null)" ]]; then
  NEEDS_DOWNLOAD=true
fi

if [[ "$NEEDS_DOWNLOAD" == true ]]; then
  echo "正在準備 YouTube 逐字稿工具…"
  if curl --location --fail --progress-bar \
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos" \
    --output "$DOWNLOAD_BIN"; then
    chmod +x "$DOWNLOAD_BIN"
    mv -f "$DOWNLOAD_BIN" "$YT_DLP_BIN"
  elif [[ ! -x "$YT_DLP_BIN" ]]; then
    echo "下載失敗，請確認網路後重新開啟。"
    read -k 1 "?按任意鍵關閉…"
    exit 1
  fi
fi

cd "$SCRIPT_DIR"
python3 "$SCRIPT_DIR/transcript_server.py" \
  --root "$SCRIPT_DIR" \
  --port 8767 \
  --yt-dlp "$YT_DLP_BIN" &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for _ in {1..30}; do
  if curl --silent --fail "$APP_URL/api/health" >/dev/null 2>&1; then
    open "$APP_URL"
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 0.2
done

echo "逐字稿服務啟動失敗。"
cleanup
read -k 1 "?按任意鍵關閉…"
exit 1
