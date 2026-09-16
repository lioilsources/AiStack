#!/usr/bin/env bash
# Cloudflare Access apps z příkazové řádky — ať se neklikají v dashboardu.
#
# Creds: ~/.cloudflare/api.env (CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID).
# Token se nikdy nepředává v argv (curl -K -), takže neuteče do `ps`.
#
# Použití:
#   cf-access.sh check                    ověří token a účet
#   cf-access.sh tokens                   vypíše service tokeny (name + client id)
#   cf-access.sh apps                     vypíše Access aplikace (domain + AUD)
#   cf-access.sh aud <hostname>           AUD tag jedné aplikace
#   cf-access.sh create <hostname> <service-token-name-substring> [app-name]
#                                         založí self-hosted app + Service Auth policy
set -euo pipefail

CREDS="${CF_CREDS:-$HOME/.cloudflare/api.env}"
[[ -f "$CREDS" ]] || { echo "chybí $CREDS" >&2; exit 1; }
set -a; . "$CREDS"; set +a
: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN není vyplněný v $CREDS}"

API=https://api.cloudflare.com/client/v4

# $1=method $2=path [$3=json body]
# Token jde do curlu configem na stdin, ne v argv — neuteče tak do `ps`.
# Tělo requestu jde přes temp soubor, aby se nemuselo escapovat do configu.
cf() {
  local method="$1" path="$2" body="${3:-}"
  local tmp="" out rc
  local cfg=(
    "url = \"$API$path\""
    "header = \"Authorization: Bearer $CLOUDFLARE_API_TOKEN\""
    "header = \"Content-Type: application/json\""
    "request = \"$method\""
    "silent"
    "show-error"
  )
  if [[ -n "$body" ]]; then
    tmp="$(mktemp)"
    printf '%s' "$body" > "$tmp"
    cfg+=("data = @$tmp")
  fi
  out="$(printf '%s\n' "${cfg[@]}" | curl -K -)"; rc=$?
  [[ -n "$tmp" ]] && rm -f "$tmp"
  printf '%s' "$out"
  return $rc
}

die_on_error() {
  local resp="$1"
  if [[ "$(jq -r '.success' <<<"$resp")" != "true" ]]; then
    echo "Cloudflare API chyba:" >&2
    jq -r '.errors[]? | "  [\(.code)] \(.message)"' <<<"$resp" >&2
    exit 1
  fi
}

account_id() {
  if [[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]]; then
    echo "$CLOUDFLARE_ACCOUNT_ID"; return
  fi
  local resp; resp="$(cf GET /accounts)"
  die_on_error "$resp"
  local n; n="$(jq '.result | length' <<<"$resp")"
  if [[ "$n" != "1" ]]; then
    echo "účtů je $n — doplň CLOUDFLARE_ACCOUNT_ID do $CREDS:" >&2
    jq -r '.result[] | "  \(.id)  \(.name)"' <<<"$resp" >&2
    exit 1
  fi
  jq -r '.result[0].id' <<<"$resp"
}

cmd_check() {
  local resp; resp="$(cf GET /user/tokens/verify)"
  die_on_error "$resp"
  echo "token: $(jq -r '.result.status' <<<"$resp")"
  echo "účet:  $(account_id)"
}

cmd_tokens() {
  local acc resp; acc="$(account_id)"
  resp="$(cf GET "/accounts/$acc/access/service_tokens")"
  die_on_error "$resp"
  jq -r '.result[] | "\(.id)\t\(.name)\t\(.client_id[0:12])…"' <<<"$resp" |
    column -t -s $'\t'
}

cmd_apps() {
  local acc resp; acc="$(account_id)"
  resp="$(cf GET "/accounts/$acc/access/apps")"
  die_on_error "$resp"
  jq -r '.result[] | "\(.domain // "-")\t\(.name)\t\(.aud // "-")"' <<<"$resp" |
    column -t -s $'\t'
}

cmd_aud() {
  local host="${1:?použití: aud <hostname>}" acc resp
  acc="$(account_id)"
  resp="$(cf GET "/accounts/$acc/access/apps")"
  die_on_error "$resp"
  local aud
  aud="$(jq -r --arg h "$host" '.result[] | select(.domain == $h) | .aud' <<<"$resp")"
  [[ -n "$aud" && "$aud" != "null" ]] || { echo "aplikace pro $host nenalezena" >&2; exit 1; }
  echo "$aud"
}

cmd_create() {
  local host="${1:?použití: create <hostname> <service-token-name> [app-name]}"
  local tokmatch="${2:?chybí jméno (nebo jeho část) service tokenu — viz: cf-access.sh tokens}"
  local appname="${3:-$host}"
  local acc; acc="$(account_id)"

  # 1) najít service token podle jména
  local tresp; tresp="$(cf GET "/accounts/$acc/access/service_tokens")"
  die_on_error "$tresp"
  local tid tname
  tid="$(jq -r --arg m "$tokmatch" '[.result[] | select(.name | test($m; "i"))] | if length == 1 then .[0].id else "" end' <<<"$tresp")"
  if [[ -z "$tid" ]]; then
    echo "„$tokmatch\" neodpovídá právě jednomu service tokenu. Dostupné:" >&2
    jq -r '.result[] | "  \(.name)"' <<<"$tresp" >&2
    exit 1
  fi
  tname="$(jq -r --arg id "$tid" '.result[] | select(.id == $id) | .name' <<<"$tresp")"
  echo "service token: $tname"

  # 2) aplikace
  local body aresp appid
  body="$(jq -nc --arg n "$appname" --arg d "$host" \
    '{name:$n, domain:$d, type:"self_hosted", session_duration:"24h",
      app_launcher_visible:false, http_only_cookie_attribute:true}')"
  aresp="$(cf POST "/accounts/$acc/access/apps" "$body")"
  die_on_error "$aresp"
  appid="$(jq -r '.result.id' <<<"$aresp")"
  echo "aplikace:      $appid"

  # 3) policy — decision "non_identity" = to, čemu dashboard říká Service Auth
  local pbody presp
  pbody="$(jq -nc --arg t "$tid" \
    '{name:"service token", decision:"non_identity",
      include:[{service_token:{token_id:$t}}]}')"
  presp="$(cf POST "/accounts/$acc/access/apps/$appid/policies" "$pbody")"
  die_on_error "$presp"
  echo "policy:        Service Auth ✓"

  echo
  echo "AUD tag (do cloudflared/config.yml):"
  jq -r '.result.aud' <<<"$aresp"
}

case "${1:-}" in
  check)  shift; cmd_check "$@" ;;
  tokens) shift; cmd_tokens "$@" ;;
  apps)   shift; cmd_apps "$@" ;;
  aud)    shift; cmd_aud "$@" ;;
  create) shift; cmd_create "$@" ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
