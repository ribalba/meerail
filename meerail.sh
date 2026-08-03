#!/usr/bin/env bash
#
# meerail — install and run the whole thing from prebuilt containers.
#
#   curl -fsSL https://raw.githubusercontent.com/ribalba/meerail/main/meerail.sh -o meerail.sh
#   bash meerail.sh
#
# One file. It asks what it needs, writes a configuration, pulls the images from
# Docker Hub and starts them. No clone, no build, no Python on the host, and —
# because Proton Bridge runs as a container here too — the same commands on
# Linux, macOS and Windows (WSL2 or Git Bash).
#
# What it creates, all under ~/.meerail:
#
#   meerail.toml        your configuration, mode 0600 — it holds mail passwords
#   .env                sizing and image tags, read by docker compose
#   docker-compose.yml  fetched from the release you installed
#
# Your mail lives in Docker volumes (pg-data), not in that directory.
#
# The developer path — clone the repo, `make up`, agent on the host next to a
# desktop Bridge — is untouched and documented in README.md. This is the other
# one: for someone who wants their mail, not a checkout.

set -euo pipefail

# --- where everything lives ---------------------------------------------------

MEERAIL_HOME="${MEERAIL_HOME:-$HOME/.meerail}"
CONFIG_FILE="$MEERAIL_HOME/meerail.toml"
ENV_FILE="$MEERAIL_HOME/.env"
COMPOSE_FILE="$MEERAIL_HOME/docker-compose.yml"

# Where an upgrade and the version pin come from. Overridable so a fork — or a
# test of an unreleased branch — can point the whole script elsewhere.
REPO="${MEERAIL_REPO:-ribalba/meerail}"
RAW_BASE="${MEERAIL_RAW_BASE:-https://raw.githubusercontent.com/$REPO/main}"

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- output -------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YEL=$'\033[33m'; BLU=$'\033[34m'; R=$'\033[0m'
else
  B=""; DIM=""; RED=""; GRN=""; YEL=""; BLU=""; R=""
fi

say()  { printf '%s\n' "$*"; }
info() { printf '%s\n' "  $*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$R" "$*"; }
die()  { printf '%s✗ %s%s\n' "$RED" "$*" "$R" >&2; exit 1; }
head1() { printf '\n%s%s%s\n' "$B" "$*" "$R"; }
rule() { printf '%s%s%s\n' "$DIM" "────────────────────────────────────────────────────────" "$R"; }

# --- prompts ------------------------------------------------------------------
#
# Every answer is read from /dev/tty rather than stdin. Without that, the
# documented `curl … | bash` invocation would find stdin already occupied by the
# script itself and race through every prompt taking the default.

need_tty() {
  [ -e /dev/tty ] || die "This needs a terminal to ask you questions. Download the script and run it directly: bash meerail.sh"
}

ask() {  # ask <prompt> [default] -> echoes the answer
  local prompt="$1" default="${2:-}" reply=""
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$prompt" "$default" > /dev/tty
  else
    printf '%s: ' "$prompt" > /dev/tty
  fi
  IFS= read -r reply < /dev/tty || true
  printf '%s' "${reply:-$default}"
}

ask_required() {  # keeps asking until something is typed
  local prompt="$1" default="${2:-}" value=""
  while :; do
    value="$(ask "$prompt" "$default")"
    [ -n "$value" ] && { printf '%s' "$value"; return; }
    printf '  %sThat one is required.%s\n' "$YEL" "$R" > /dev/tty
  done
}

ask_secret() {  # no echo; asks twice only when it is a password we generated nothing for
  local prompt="$1" value=""
  printf '%s: ' "$prompt" > /dev/tty
  IFS= read -r -s value < /dev/tty || true
  printf '\n' > /dev/tty
  printf '%s' "$value"
}

ask_yn() {  # ask_yn <prompt> <y|n default> -> returns 0 for yes
  local prompt="$1" default="${2:-n}" reply="" hint="y/N"
  [ "$default" = "y" ] && hint="Y/n"
  while :; do
    printf '%s [%s]: ' "$prompt" "$hint" > /dev/tty
    IFS= read -r reply < /dev/tty || true
    reply="${reply:-$default}"
    case "$reply" in
      [Yy]|[Yy][Ee][Ss]) return 0 ;;
      [Nn]|[Nn][Oo])     return 1 ;;
      *) printf '  %sPlease answer y or n.%s\n' "$YEL" "$R" > /dev/tty ;;
    esac
  done
}

ask_choice() {  # ask_choice <default-index> <label…> -> echoes the chosen index
  local default="$1"; shift
  local n=$# i=1 reply=""
  for opt in "$@"; do printf '  %s%d)%s %s\n' "$B" "$i" "$R" "$opt" > /dev/tty; i=$((i + 1)); done
  while :; do
    printf 'Choice [%s]: ' "$default" > /dev/tty
    IFS= read -r reply < /dev/tty || true
    reply="${reply:-$default}"
    case "$reply" in
      ''|*[!0-9]*) ;;
      *) if [ "$reply" -ge 1 ] && [ "$reply" -le "$n" ]; then printf '%s' "$reply"; return; fi ;;
    esac
    printf '  %sPick a number between 1 and %d.%s\n' "$YEL" "$n" "$R" > /dev/tty
  done
}

# --- small helpers ------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

# Random secrets. openssl where it exists, /dev/urandom otherwise — the subshell
# disables pipefail because `head -c` closing the pipe kills `tr` with SIGPIPE,
# which under `set -o pipefail` would otherwise abort the whole script.
rand() {
  local n="${1:-48}"
  if have openssl; then
    openssl rand -base64 $((n * 2)) | tr -dc 'A-Za-z0-9' | cut -c1-"$n"
  else
    ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$n" )
  fi
}

# TOML string escaping. Passwords are arbitrary text and app passwords in
# particular carry spaces; a stray " or \ would otherwise produce a file that
# does not parse — after the user has already typed their credentials in.
toml_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }

fetch() {  # fetch <url> <dest>
  if have curl; then curl -fsSL "$1" -o "$2"
  elif have wget; then wget -qO "$2" "$1"
  else return 1; fi
}

fetch_stdout() {
  if have curl; then curl -fsSL "$1"
  elif have wget; then wget -qO- "$1"
  else return 1; fi
}

compose() {
  # --env-file and -f are absolute, but the project directory (which is what
  # `./meerail.toml` in the compose file resolves against) follows the compose
  # file, so this works from wherever the user happens to be standing.
  #
  # The profile is passed as a flag rather than left to COMPOSE_PROFILES in the
  # env file: compose does read that variable from there, but only as an
  # interpolation source in some versions, and a Bridge that silently fails to
  # start would look exactly like a Bridge that failed to log in. One flag ends
  # the ambiguity. Repeating it (the bridge subcommands pass their own) is
  # harmless.
  local profiles
  profiles="$(env_get COMPOSE_PROFILES)"
  if [ -n "$profiles" ]; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile "$profiles" "$@"
  else
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
  fi
}

configured() { [ -f "$COMPOSE_FILE" ] && [ -f "$ENV_FILE" ] && [ -f "$CONFIG_FILE" ]; }

require_configured() {
  configured || die "meerail is not set up yet on this machine. Run: bash $0 setup"
}

env_get() {  # read one KEY=value back out of .env
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

web_url() {
  local port; port="$(env_get MEERAIL_PORT)"
  printf 'http://localhost:%s' "${port:-8000}"
}

# --- preflight ----------------------------------------------------------------

check_docker() {
  have docker || die "Docker is not installed. Get Docker Desktop (macOS/Windows) or Docker Engine (Linux) from https://docs.docker.com/get-docker/ and run this again."
  docker compose version >/dev/null 2>&1 || die "Docker is installed but the Compose v2 plugin is missing (\`docker compose version\` fails). On Linux: install the docker-compose-plugin package. The old \`docker-compose\` script will not do."
  docker info >/dev/null 2>&1 || die "Docker is installed but not running — start Docker Desktop (or \`sudo systemctl start docker\`) and try again. If Docker needs sudo on this machine, either add yourself to the \`docker\` group or run this script with sudo."
  have curl || have wget || die "Neither curl nor wget is available, and one of them is needed to fetch the compose file."
}

# --- sizing -------------------------------------------------------------------
#
# Asking Docker rather than the OS is the point: on Docker Desktop the daemon
# runs in a VM with its own memory allowance, usually far below the Mac's or
# PC's own RAM, and that allowance — not the host's — is what the containers
# have to fit inside. On Linux the two are the same number anyway.

docker_ram_mb() {
  local bytes
  bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || true)"
  case "$bytes" in
    ''|*[!0-9]*) printf '0' ;;
    *) printf '%s' $((bytes / 1024 / 1024)) ;;
  esac
}

# Sets the PG_*/…_MEM_LIMIT variables for the tier the machine falls into.
# Postgres' own limit has to stay clear of shared_buffers + shm + a
# maintenance_work_mem (a GIN trigram build takes one), or the postmaster is
# OOM-killed mid-backfill — which looks like data loss and is nothing of the
# kind. AGENT_BATCH tracks the agent's limit because the fetch peak is one
# batch of complete raw messages held in memory at once.
size_for() {
  local mb="$1"
  if   [ "$mb" -ge 24000 ]; then TIER="large (>= 24 GB)"
    PG_SHARED_BUFFERS=4GB PG_EFFECTIVE_CACHE_SIZE=12GB PG_WORK_MEM=64MB PG_MAINTENANCE_WORK_MEM=2GB
    PG_MAX_WAL_SIZE=8GB PG_SHM_SIZE=1gb PG_PARALLEL_WORKERS=4
    PG_MEM_LIMIT=10g TIKA_MEM_LIMIT=3g SERVER_MEM_LIMIT=2g AGENT_MEM_LIMIT=6g AGENT_BATCH=200
  elif [ "$mb" -ge 12000 ]; then TIER="medium (12–24 GB)"
    PG_SHARED_BUFFERS=2GB PG_EFFECTIVE_CACHE_SIZE=6GB PG_WORK_MEM=48MB PG_MAINTENANCE_WORK_MEM=1GB
    PG_MAX_WAL_SIZE=4GB PG_SHM_SIZE=1gb PG_PARALLEL_WORKERS=4
    PG_MEM_LIMIT=5g TIKA_MEM_LIMIT=3g SERVER_MEM_LIMIT=2g AGENT_MEM_LIMIT=4g AGENT_BATCH=200
  elif [ "$mb" -ge 7000 ];  then TIER="small (7–12 GB)"
    PG_SHARED_BUFFERS=1GB PG_EFFECTIVE_CACHE_SIZE=3GB PG_WORK_MEM=32MB PG_MAINTENANCE_WORK_MEM=512MB
    PG_MAX_WAL_SIZE=4GB PG_SHM_SIZE=512mb PG_PARALLEL_WORKERS=2
    PG_MEM_LIMIT=3g TIKA_MEM_LIMIT=2g SERVER_MEM_LIMIT=1g AGENT_MEM_LIMIT=2g AGENT_BATCH=100
  else TIER="tight (< 7 GB)"
    PG_SHARED_BUFFERS=512MB PG_EFFECTIVE_CACHE_SIZE=1536MB PG_WORK_MEM=16MB PG_MAINTENANCE_WORK_MEM=256MB
    PG_MAX_WAL_SIZE=2GB PG_SHM_SIZE=256mb PG_PARALLEL_WORKERS=1
    PG_MEM_LIMIT=2g TIKA_MEM_LIMIT=1500m SERVER_MEM_LIMIT=768m AGENT_MEM_LIMIT=1500m AGENT_BATCH=50
  fi
}

# --- provider presets ---------------------------------------------------------
#
# Only the hosts that are stable, documented, and take an app password. Anything
# else is typed in by hand, which is a perfectly good path — the agent speaks
# plain IMAP and SMTP and does not care who is on the other end.
#
# Deliberately absent: Outlook/Microsoft 365 personal accounts, which no longer
# accept a password on IMAP at all (OAuth only), so a preset here would only
# lead people into a login that cannot succeed.
preset_for() {  # preset_for <email> -> sets P_* or leaves them empty
  P_IMAP_HOST=""; P_IMAP_PORT=""; P_IMAP_SEC=""; P_SMTP_HOST=""; P_SMTP_PORT=""; P_SMTP_SEC=""
  P_NOTE=""; P_BATCH=""
  case "$(printf '%s' "${1##*@}" | tr 'A-Z' 'a-z')" in
    gmail.com|googlemail.com)
      P_IMAP_HOST=imap.gmail.com; P_IMAP_PORT=993; P_IMAP_SEC=ssl
      P_SMTP_HOST=smtp.gmail.com; P_SMTP_PORT=465; P_SMTP_SEC=ssl
      # Gmail answers a large BODY.PEEK[] fetch with missing UIDs or an outright
      # disconnect often enough that a big mailbox never finishes a backfill.
      P_BATCH=25
      P_NOTE="Gmail needs 2-Step Verification switched on, an App Password (16 characters, from myaccount.google.com/apppasswords), and IMAP enabled in Gmail's settings. Your normal Google password will not work."
      ;;
    fastmail.com|fastmail.fm)
      P_IMAP_HOST=imap.fastmail.com; P_IMAP_PORT=993; P_IMAP_SEC=ssl
      P_SMTP_HOST=smtp.fastmail.com; P_SMTP_PORT=465; P_SMTP_SEC=ssl
      P_NOTE="Fastmail needs an app password from Settings → Privacy & Security → Integrations."
      ;;
    icloud.com|me.com|mac.com)
      P_IMAP_HOST=imap.mail.me.com; P_IMAP_PORT=993; P_IMAP_SEC=ssl
      P_SMTP_HOST=smtp.mail.me.com; P_SMTP_PORT=587; P_SMTP_SEC=starttls
      P_NOTE="iCloud needs an app-specific password from account.apple.com, and the username is usually the part of your address before the @."
      ;;
  esac
}

# --- account questions --------------------------------------------------------
#
# Each of these appends one [[agent.account]] block to $ACCOUNTS_TOML.

ACCOUNTS_TOML=""
ACCOUNT_COUNT=0

account_common_tail() {  # shared timeouts + optional aliases; $1 = extra batch line
  local aliases=""
  aliases="$(ask "Other addresses this account can send from (comma-separated, blank for none)" "")"
  {
    printf 'imap_connect_timeout = 10\n'
    printf 'imap_read_timeout    = 60\n'
    printf 'smtp_timeout         = 60\n'
    [ -n "$1" ] && printf '%s\n' "$1"
    if [ -n "$aliases" ]; then
      printf 'addresses = ['
      local first=1 a
      # `read -a` is bash-4-only on macOS's ancient bash; splitting on commas
      # with IFS in a subshell-free loop keeps this working on /bin/bash 3.2.
      local rest="$aliases" item
      while [ -n "$rest" ]; do
        case "$rest" in
          *,*) item="${rest%%,*}"; rest="${rest#*,}" ;;
          *)   item="$rest"; rest="" ;;
        esac
        item="$(printf '%s' "$item" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [ -z "$item" ] && continue
        [ "$first" = 1 ] || printf ', '
        printf '%s' "$(toml_str "$item")"
        first=0
      done
      printf ']\n'
    fi
  }
}

ask_proton_account() {
  head1 "Proton Mail account"
  say "Bridge is running as a container and already holds the account you just"
  say "logged in with. What it needs now are the credentials ${B}it${R} generated —"
  say "the ones \`info\` printed a moment ago, not your Proton password."
  say ""

  local email username password block
  email="$(ask_required "Your Proton address (the one you log in with)")"
  username="$(ask "Bridge username (from \`info\`)" "$email")"
  while :; do
    password="$(ask_secret "Bridge password (from \`info\`, input hidden)")"
    [ -n "$password" ] && break
    warn "Bridge will not accept an empty password — paste the one \`info\` printed."
  done

  # "bridge" below is the compose service name, and it is the whole trick that
  # makes Proton work the same on macOS and Windows as on Linux: the agent
  # reaches Bridge over the compose network instead of a host loopback that
  # Docker Desktop would never let it see. Inside the container Bridge listens
  # on 143 and 25, STARTTLS with a self-signed certificate — hence
  # verify_cert = false.
  #
  # (Comments stay out of the command substitution below: bash pre-scans it for
  # the closing paren, and a stray backtick or apostrophe in there is a syntax
  # error rather than a comment.)
  block="$(
    printf '\n[[agent.account]]\n'
    printf 'email        = %s\n' "$(toml_str "$email")"
    printf 'imap_host    = "bridge"\n'
    printf 'imap_port    = 143\n'
    printf 'imap_security = "starttls"\n'
    printf 'smtp_host    = "bridge"\n'
    printf 'smtp_port    = 25\n'
    printf 'smtp_security = "starttls"\n'
    printf 'username     = %s\n' "$(toml_str "$username")"
    printf 'password     = %s\n' "$(toml_str "$password")"
    printf 'verify_cert  = false\n'
    account_common_tail ""
  )"
  ACCOUNTS_TOML="$ACCOUNTS_TOML$block"
  ACCOUNT_COUNT=$((ACCOUNT_COUNT + 1))
}

ask_imap_account() {
  head1 "Mail account"
  local email imap_host imap_port imap_sec smtp_host smtp_port smtp_sec username password batch block

  email="$(ask_required "Email address")"
  preset_for "$email"
  if [ -n "$P_IMAP_HOST" ]; then
    ok "Recognised the provider — its servers are filled in below."
    [ -n "$P_NOTE" ] && { say ""; printf '  %s%s%s\n' "$YEL" "$P_NOTE" "$R"; say ""; }
  fi

  imap_host="$(ask_required "IMAP host" "${P_IMAP_HOST:-}")"
  imap_port="$(ask "IMAP port" "${P_IMAP_PORT:-993}")"
  imap_sec="$(ask "IMAP security (ssl / starttls / plain)" "${P_IMAP_SEC:-ssl}")"
  smtp_host="$(ask_required "SMTP host" "${P_SMTP_HOST:-$imap_host}")"
  smtp_port="$(ask "SMTP port" "${P_SMTP_PORT:-465}")"
  smtp_sec="$(ask "SMTP security (ssl / starttls / plain)" "${P_SMTP_SEC:-ssl}")"
  username="$(ask "Username" "$email")"
  while :; do
    password="$(ask_secret "Password (an app password for most providers — input hidden)")"
    [ -n "$password" ] && break
    warn "A password is required."
  done
  batch="${P_BATCH:-}"

  block="$(
    printf '\n[[agent.account]]\n'
    printf 'email        = %s\n' "$(toml_str "$email")"
    printf 'imap_host    = %s\n' "$(toml_str "$imap_host")"
    printf 'imap_port    = %s\n' "$imap_port"
    printf 'imap_security = %s\n' "$(toml_str "$imap_sec")"
    printf 'smtp_host    = %s\n' "$(toml_str "$smtp_host")"
    printf 'smtp_port    = %s\n' "$smtp_port"
    printf 'smtp_security = %s\n' "$(toml_str "$smtp_sec")"
    printf 'username     = %s\n' "$(toml_str "$username")"
    printf 'password     = %s\n' "$(toml_str "$password")"
    printf 'verify_cert  = true\n'
    if [ -n "$batch" ]; then
      account_common_tail "batch_size   = $batch   # this provider dislikes large fetches"
    else
      account_common_tail ""
    fi
  )"
  ACCOUNTS_TOML="$ACCOUNTS_TOML$block"
  ACCOUNT_COUNT=$((ACCOUNT_COUNT + 1))
}

# --- writing the files --------------------------------------------------------

write_env() {
  # Written before the compose file is ever invoked, because `docker compose`
  # interpolates from it — including for the one-off `run bridge init`.
  umask 077
  cat > "$ENV_FILE" <<EOF
# meerail — written by meerail.sh on first run. Safe to edit; re-run
# \`meerail.sh start\` afterwards to apply.
#
# This file is read by docker compose only. Application settings live in
# meerail.toml beside it — with one exception that cannot: the Postgres image
# takes its credentials from the environment and nowhere else, so POSTGRES_*
# lives here and database.url in meerail.toml has to match it.

# --- images -------------------------------------------------------------------
# Pinned so an install stays put until you ask for an upgrade. \`meerail.sh
# update\` moves this to the newest release and pulls it.
MEERAIL_VERSION=$MEERAIL_VERSION
MEERAIL_IMAGE_SERVER=$IMAGE_NS/meerail-server
MEERAIL_IMAGE_AGENT=$IMAGE_NS/meerail-agent
MEERAIL_IMAGE_TIKA=$IMAGE_NS/meerail-tika

# Empty = no Proton Bridge container. "proton" = run one.
COMPOSE_PROFILES=$COMPOSE_PROFILES

# --- web ----------------------------------------------------------------------
# 127.0.0.1 keeps the UI on this machine. Change to 0.0.0.0 only with a password
# set in meerail.toml and something doing TLS in front of it.
MEERAIL_BIND=$MEERAIL_BIND
MEERAIL_PORT=$MEERAIL_PORT

# --- database credentials -----------------------------------------------------
# Generated. If you change the password here, change database.url in
# meerail.toml to match — these credentials create the database, that URL
# connects to it, and nothing reconciles the two for you.
POSTGRES_USER=meerail
POSTGRES_PASSWORD=$PG_PASSWORD
POSTGRES_DB=meerail

# --- sizing -------------------------------------------------------------------
# Chosen for a $TIER machine — Docker reports ${RAM_MB} MB available to it.
PG_SHARED_BUFFERS=$PG_SHARED_BUFFERS
PG_EFFECTIVE_CACHE_SIZE=$PG_EFFECTIVE_CACHE_SIZE
PG_WORK_MEM=$PG_WORK_MEM
PG_MAINTENANCE_WORK_MEM=$PG_MAINTENANCE_WORK_MEM
PG_MAX_WAL_SIZE=$PG_MAX_WAL_SIZE
PG_SHM_SIZE=$PG_SHM_SIZE
PG_PARALLEL_WORKERS=$PG_PARALLEL_WORKERS
PG_MEM_LIMIT=$PG_MEM_LIMIT
TIKA_MEM_LIMIT=$TIKA_MEM_LIMIT
SERVER_MEM_LIMIT=$SERVER_MEM_LIMIT
AGENT_MEM_LIMIT=$AGENT_MEM_LIMIT
EOF
  chmod 600 "$ENV_FILE"
}

write_config() {
  umask 077
  cat > "$CONFIG_FILE" <<EOF
# meerail configuration — one file for the server and the agent.
#
# Written by meerail.sh. Edit it and run \`meerail.sh restart\` to apply.
# Every setting here can be overridden by an environment variable of the same
# name in upper case (server.password -> SERVER_PASSWORD).
#
# It holds mailbox passwords in plaintext, so it is mode 0600 and the agent
# refuses to pass its self-test if anyone else can read it.
#
# The full annotated reference is meerail.example.toml in the repository:
# https://github.com/$REPO/blob/main/meerail.example.toml

[database]
# db:5432 is the Postgres container on the compose network. The password comes
# from POSTGRES_PASSWORD in .env — change one and you must change the other.
url = "postgresql+psycopg://meerail:$PG_PASSWORD@db:5432/meerail"

[server]
# Encrypts stored credentials and signs session cookies. Generated for this
# install; changing it logs every browser out.
secret_key = $(toml_str "$SECRET_KEY")

# Password for the web UI. Empty means anyone who can reach the port gets the
# mail — which is fine while the port is on 127.0.0.1 and nothing else.
password = $(toml_str "$UI_PASSWORD")

session_max_age_days = 30
default_search_years = 0
contacts_scan_years  = 1

# Once a day, ask github whether a newer meerail is out and say so in the UI.
# It sends nothing about you — see Settings → About.
update_check = $UPDATE_CHECK

[agent]
# The Tika container on the compose network.
tika_url = "http://tika:9998"

poll_interval      = 30
reconcile_interval = 900
# Sized to this machine: the fetch peak is one batch of complete raw messages
# held in memory at once, so this and AGENT_MEM_LIMIT in .env move together.
batch_size         = $AGENT_BATCH

# Keep the original bytes of every message. Roughly half the database's size;
# nothing reads them today, they are held so a future export or re-parse has
# the original to work from.
store_raw_mime = $STORE_RAW_MIME

# Only keep the *content* of mail this many months old; 0 keeps everything.
# Older mail still lists, threads and answers a search by subject or
# correspondent — it just has no body stored. Nothing on the mail server is
# touched either way.
content_window_months = $CONTENT_WINDOW
$ACCOUNTS_TOML
EOF
  chmod 600 "$CONFIG_FILE"
}

install_compose_file() {
  # A checkout beside the script wins, so that testing an unreleased change does
  # not need it published first. Otherwise take the one from the release.
  if [ -f "$SELF_DIR/docker-compose.hub.yml" ]; then
    cp "$SELF_DIR/docker-compose.hub.yml" "$COMPOSE_FILE"
    info "Using docker-compose.hub.yml from $SELF_DIR"
  else
    fetch "$RAW_BASE/docker-compose.hub.yml" "$COMPOSE_FILE" \
      || die "Could not download the compose file from $RAW_BASE/docker-compose.hub.yml"
  fi
}

latest_version() {
  # The release itself: CI tags the images with exactly this file's contents.
  local v
  v="$(fetch_stdout "$RAW_BASE/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
  case "$v" in
    ''|*[!0-9.a-zA-Z_-]*) printf 'latest' ;;
    *) printf '%s' "$v" ;;
  esac
}

# --- setup --------------------------------------------------------------------

cmd_setup() {
  need_tty
  check_docker

  head1 "meerail setup"
  say "This asks a handful of questions, writes two files under"
  say "${B}$MEERAIL_HOME${R}, and starts the containers. Nothing is sent anywhere."
  say ""

  if configured; then
    warn "meerail is already set up in $MEERAIL_HOME."
    ask_yn "Reconfigure from scratch? (your mail and database are kept)" n || {
      say "Nothing changed. \`meerail.sh status\` shows how it is doing."
      return 0
    }
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    cp "$CONFIG_FILE" "$CONFIG_FILE.$stamp.bak" 2>/dev/null || true
    cp "$ENV_FILE" "$ENV_FILE.$stamp.bak" 2>/dev/null || true
    info "Previous configuration saved as *.${stamp}.bak"
  fi

  mkdir -p "$MEERAIL_HOME"
  chmod 700 "$MEERAIL_HOME"

  # --- 1. what mail ---
  head1 "1. Where does your mail live?"
  local provider
  provider="$(ask_choice 1 \
    "Proton Mail — run Proton Bridge as a container (works on macOS, Windows and Linux)" \
    "Any other IMAP/SMTP account — Gmail, Fastmail, iCloud, your own server")"

  # --- 2. sizing (before anything is written, so the plan can be shown) ---
  RAM_MB="$(docker_ram_mb)"
  if [ "$RAM_MB" -le 0 ]; then
    warn "Could not read how much memory Docker has; assuming 8 GB."
    RAM_MB=8000
  fi
  size_for "$RAM_MB"

  head1 "2. This machine"
  info "Docker has ${B}${RAM_MB} MB${R} of memory — sizing the stack as ${B}$TIER${R}."
  if [ "$RAM_MB" -lt 6000 ]; then
    warn "Below the ~6 GB the stack really wants. It will run, but a first"
    warn "backfill of a large mailbox may be slow, and OCR of scanned PDFs"
    warn "may fall over. On Docker Desktop you can raise the VM's memory in"
    warn "Settings → Resources."
    ask_yn "Carry on anyway?" y || die "Stopped. Nothing was written."
  fi

  # --- 3. web ---
  head1 "3. The web app"
  MEERAIL_PORT="$(ask "Port to serve the UI on" "8000")"
  MEERAIL_BIND="127.0.0.1"
  UI_PASSWORD=""
  if ask_yn "Reach it from other machines on your network? (default: this machine only)" n; then
    say ""
    say "Then it needs a password: the UI is the mail, and it would otherwise be"
    say "open to anyone who can reach the port."
    while [ -z "$UI_PASSWORD" ]; do
      UI_PASSWORD="$(ask_secret "Password for the web UI (input hidden)")"
      [ -z "$UI_PASSWORD" ] && warn "Required when the port is open to the network."
    done
    MEERAIL_BIND="0.0.0.0"
    say ""
    warn "There is no TLS in front of this. Put it behind a reverse proxy with a"
    warn "certificate before using it over anything but a trusted local network."
  else
    UI_PASSWORD="$(ask_secret "Optional password for the UI (blank for none — input hidden)")"
  fi

  # --- 4. storage ---
  head1 "4. What to keep"
  say "Mail is stored in Postgres. Two settings decide how much disk that takes;"
  say "both can be changed later in meerail.toml."
  say ""
  STORE_RAW_MIME=true
  ask_yn "Keep each message's original raw bytes? (roughly doubles the database)" y || STORE_RAW_MIME=false
  CONTENT_WINDOW=0
  if ask_yn "Limit stored message *bodies* to recent mail only?" n; then
    CONTENT_WINDOW="$(ask "Keep bodies for mail from the last how many months?" "24")"
    info "Older mail still lists, threads and answers searches by subject and"
    info "correspondent — it simply has no body stored. Nothing is deleted from"
    info "the mail server."
  fi
  UPDATE_CHECK=true
  ask_yn "Let the server check github once a day for new meerail releases?" y || UPDATE_CHECK=false

  # --- 5. secrets and files ---
  SECRET_KEY="$(rand 48)"
  PG_PASSWORD="$(rand 32)"
  IMAGE_NS="${MEERAIL_IMAGE_NS:-ribalba}"
  MEERAIL_VERSION="${MEERAIL_VERSION_OVERRIDE:-$(latest_version)}"
  COMPOSE_PROFILES=""
  [ "$provider" = "1" ] && COMPOSE_PROFILES="proton"

  head1 "5. Fetching"
  install_compose_file
  write_env
  ok "Wrote $ENV_FILE"
  info "meerail $MEERAIL_VERSION, images from $IMAGE_NS/*"
  say ""
  say "Pulling the images. Tika is a multi-GB download (it carries the OCR"
  say "engine that reads scanned PDFs), so this is the slow part — once."
  say ""
  compose pull || die "Could not pull the images. If you are offline, try again when you
are not; if this is a fork, check MEERAIL_IMAGE_NS and that the images have been
published. Nothing is running yet — re-run this script to pick up where it stopped."

  # --- 6. the account(s) ---
  if [ "$provider" = "1" ]; then
    head1 "6. Log Proton Bridge in"
    rule
    say "Bridge cannot take an account from a config file — 2FA is why — so it"
    say "gets one interactive login, once, and remembers it in its own volume."
    say ""
    say "In the session that opens next, type:"
    say ""
    say "  ${B}login${R}   then your Proton address, password, and 2FA code if you use one"
    say "  ${B}info${R}    prints the IMAP/SMTP username and password Bridge generated"
    say "  ${B}exit${R}    when you have copied those two values down"
    say ""
    say "${YEL}Copy what \`info\` prints before you exit${R} — the next question asks for it."
    rule
    ask "Press Enter when you are ready" "" >/dev/null

    compose --profile proton run --rm bridge init || {
      warn "The Bridge login session exited non-zero."
      ask_yn "Carry on and enter the credentials anyway?" y || die "Stopped. Re-run \`bash $0 setup\` when ready."
    }
    ask_proton_account
    while ask_yn "Add another Proton address handled by this same Bridge?" n; do
      ask_proton_account
    done
  else
    head1 "6. Your mail account"
    ask_imap_account
    while ask_yn "Add another account?" n; do
      ask_imap_account
    done
  fi

  write_config
  ok "Wrote $CONFIG_FILE (mode 0600 — it holds your mail password)"

  # --- 7. go ---
  head1 "7. Starting"
  compose up -d
  say ""
  wait_for_health || warn "The server did not answer in time. \`meerail.sh logs\` should say why."

  head1 "meerail is up"
  ok "Web app:  ${B}$(web_url)${R}"
  info "Config:   $CONFIG_FILE"
  info "Data:     docker volumes (meerail_pg-data) — not in that directory"
  say ""
  say "The first sync walks your whole mailbox and can take from minutes to"
  say "hours. Mail appears as it lands; the sidebar shows the agent's progress."
  say ""
  say "  ${B}bash $0 status${R}    how the sync is going"
  say "  ${B}bash $0 logs agent${R}  watch it work"
  say "  ${B}bash $0 test${R}      check every connection"
  say "  ${B}bash $0 help${R}      everything else"
}

wait_for_health() {
  local url; url="$(web_url)/healthz"
  local i=0
  printf 'Waiting for the server'
  while [ "$i" -lt 60 ]; do
    if have curl && curl -fsS "$url" >/dev/null 2>&1; then printf ' \n'; ok "Server is answering."; return 0; fi
    if ! have curl && have wget && wget -qO- "$url" >/dev/null 2>&1; then printf ' \n'; ok "Server is answering."; return 0; fi
    printf '.'
    sleep 2
    i=$((i + 1))
  done
  printf ' \n'
  return 1
}

# --- lifecycle ----------------------------------------------------------------

cmd_start()   { require_configured; compose up -d; ok "Running — $(web_url)"; }
cmd_stop()    { require_configured; compose stop; ok "Stopped. \`start\` brings it back with everything intact."; }
cmd_restart() { require_configured; compose up -d --force-recreate; ok "Restarted — $(web_url)"; }
cmd_logs()    { require_configured; compose logs -f --tail 200 ${1:+"$1"}; }
cmd_psql()    { require_configured; compose exec db psql -U meerail -d meerail; }

cmd_status() {
  require_configured
  head1 "Containers"
  compose ps
  head1 "Version"
  local current latest
  current="$(env_get MEERAIL_VERSION)"
  latest="$(latest_version)"
  info "installed: ${current:-unknown}"
  info "latest:    $latest"
  if [ -n "$current" ] && [ "$current" != "$latest" ] && [ "$latest" != "latest" ]; then
    say ""
    warn "A newer meerail is out. Upgrade with: bash $0 update"
  fi
  head1 "Web"
  info "$(web_url)"
}

cmd_test() {
  require_configured
  # The agent's own preflight: config permissions, database, Tika, and a real
  # IMAP and SMTP login per account. Read-only — it sends nothing and writes
  # nothing.
  compose run --rm agent --test
}

cmd_requeue() {
  require_configured
  # Only ever finds anything an *older* agent left: the five-attempt cap that
  # retired queued work — sends included — is gone, so nothing new lands here.
  # A one-off container, because the flag exits rather than staying live; the
  # running agent picks the rows up on its next pass.
  compose run --rm agent --requeue-abandoned
}

cmd_update() {
  require_configured
  local current latest
  current="$(env_get MEERAIL_VERSION)"
  latest="$(latest_version)"
  head1 "Update"
  info "installed: ${current:-unknown}"
  info "latest:    $latest"
  if [ "$current" = "$latest" ]; then
    ok "Already on the newest release."
    ask_yn "Re-pull the images anyway?" n || return 0
  fi

  # The compose file ships with the release, so it moves with it — a new
  # service or a renamed variable would otherwise be missed on upgrade.
  install_compose_file
  if [ -n "$latest" ] && [ "$latest" != "latest" ]; then
    # sed -i differs between GNU and BSD; write a new file and move it instead.
    sed "s/^MEERAIL_VERSION=.*/MEERAIL_VERSION=$latest/" "$ENV_FILE" > "$ENV_FILE.new"
    mv "$ENV_FILE.new" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  fi
  compose pull
  compose up -d
  ok "Now on $latest — $(web_url)"
  info "The database migrates itself on first boot; nothing else to do."
}

cmd_bridge() {
  require_configured
  case "${1:-init}" in
    init|login)
      say "Type ${B}login${R}, then ${B}info${R} to see the credentials, then ${B}exit${R}."
      say "If the username or password changed, put them in $CONFIG_FILE and run \`$0 restart\`."
      compose --profile proton run --rm bridge init
      ;;
    logs) compose --profile proton logs -f --tail 200 bridge ;;
    *) die "usage: $0 bridge [init|logs]" ;;
  esac
}

cmd_config() {
  require_configured
  if [ "${1:-}" = "path" ]; then printf '%s\n' "$CONFIG_FILE"; return; fi
  "${EDITOR:-vi}" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
  ask_yn "Restart so the change takes effect?" y && compose up -d --force-recreate
}

cmd_uninstall() {
  require_configured
  need_tty
  head1 "Uninstall"
  say "This stops and removes the containers."
  if ask_yn "Also delete the database volume — every message meerail has stored?" n; then
    say ""
    warn "That cannot be undone. Your mail is still on the mail server; what is"
    warn "deleted is meerail's copy, its search index and its sync state."
    if ask_yn "Delete it?" n; then
      compose --profile proton down -v
      ok "Containers and volumes removed."
    else
      compose --profile proton down
      ok "Containers removed; volumes kept."
    fi
  else
    compose --profile proton down
    ok "Containers removed; volumes kept. \`start\` brings it all back."
  fi
  say ""
  info "Configuration is still in $MEERAIL_HOME — delete it by hand if you want it gone."
}

cmd_help() {
  cat <<EOF
${B}meerail${R} — your mail, in containers.

  ${B}bash $0${R}                 set up on first run, then start

Everyday:
  ${B}start${R}                 start (or restart after a config change)
  ${B}stop${R}                  stop everything; nothing is lost
  ${B}status${R}                containers, version, URL
  ${B}logs${R} [service]        follow the logs (try: logs agent)
  ${B}test${R}                  check every connection the agent needs

Occasionally:
  ${B}requeue${R}               re-queue anything an older agent gave up on
  ${B}update${R}                pull the newest release and restart
  ${B}config${R}                edit meerail.toml in \$EDITOR
  ${B}bridge init${R}           log Proton Bridge in again
  ${B}psql${R}                  a SQL shell on the mail database
  ${B}setup${R}                 reconfigure from scratch
  ${B}uninstall${R}             remove the containers (asks about the data)

Files live in ${B}$MEERAIL_HOME${R} (override with MEERAIL_HOME).
Mail lives in Docker volumes, not there.

Source, issues and the annotated configuration reference:
  https://github.com/$REPO
EOF
}

# --- dispatch -----------------------------------------------------------------

main() {
  case "${1:-}" in
    ""|start|up)   if configured; then check_docker; cmd_start; else cmd_setup; fi ;;
    setup|install) cmd_setup ;;
    stop|down)     check_docker; cmd_stop ;;
    restart)       check_docker; cmd_restart ;;
    status|ps)     check_docker; cmd_status ;;
    logs)          check_docker; shift; cmd_logs "${1:-}" ;;
    test|check)    check_docker; cmd_test ;;
    requeue)       check_docker; cmd_requeue ;;
    update|upgrade) check_docker; cmd_update ;;
    bridge)        check_docker; shift; cmd_bridge "${1:-init}" ;;
    config)        shift; cmd_config "${1:-}" ;;
    psql)          check_docker; cmd_psql ;;
    uninstall|remove) check_docker; cmd_uninstall ;;
    version)       printf 'installed: %s\nlatest:    %s\n' "$(env_get MEERAIL_VERSION)" "$(latest_version)" ;;
    help|-h|--help) cmd_help ;;
    *) die "Unknown command: $1 — try \`$0 help\`" ;;
  esac
}

main "$@"
