#!/usr/bin/env bash
# Bootstrap for a fresh deployment, and the clean way to switch sources. Run it through mise: `mise run setup` (flags in
# app/mise.toml, anything missing is asked; secrets are prompted for, hidden; values never print; re-running is safe).
# Both: the OpenTofu state encryption key (TF_ENCRYPTION) is generated once and kept, also across a switch.
#   env:       the three settings and TF_ENCRYPTION become GitHub secrets; TF_ENCRYPTION also goes in app/.env (add the
#              three settings there yourself for local tasks). Removes OP_VAULT (repository variable and app/.env).
#   1password: creates the vault (if missing) and the item ai-job-search that app/1password.env references, creates the CI service account (read and write on that vault
#              only) and stores its token as the secret OP_SERVICE_ACCOUNT_TOKEN, and records OP_VAULT (repository
#              variable and app/.env). Removes the three Cloudflare GitHub secrets, so both sources never coexist.
# Needs gh (signed in, admin on the repo), jq, curl, and for 1password a signed-in op (desktop app integration).
# The Cloudflare token is minted by hand first (DEPLOY.md, step 1).
set -euo pipefail
cd "$(dirname "$0")/.."
SOURCE="${usage_source:-${SOURCE:-}}"; HOST="${usage_hostname:-${CLOUDFLARE_HOSTNAME:-}}"
EMAILS="${usage_emails:-${EMAILS:-}}"; REPO="${usage_repo:-${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}}"
GUEST_LOCAL="${usage_guest_local-${GUEST_LOCAL-job-search-guest}}"; ITEM=ai-job-search
NAMES=(CLOUDFLARE_API_TOKEN CLOUDFLARE_HOSTNAME CLOUDFLARE_ACCESS_ALLOWED_EMAILS)
ask() { local v; read -rp "$1: " v; echo "$v"; }
setenv() { touch .env; grep -v "^$1=" .env > .env.tmp || true; [ -z "${2-}" ] || echo "$1=$2" >> .env.tmp; mv .env.tmp .env; }  # empty value: remove
[ -n "$SOURCE" ] || SOURCE=$(ask "Where should the settings live? (env / 1password)")
case "$SOURCE" in env|1password) ;; *) echo "source must be env or 1password" >&2; exit 1 ;; esac
[ -n "$HOST" ] || HOST=$(ask "Dashboard hostname on a zone in your Cloudflare account")
[ -n "$EMAILS" ] || EMAILS=$(ask "Sign-in email(s), comma-separated")
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then read -rsp "Cloudflare API token: " CLOUDFLARE_API_TOKEN; echo; fi

# Optional guest alias <GUEST_LOCAL>@<zone of the hostname> (zone found via the API; nothing hardcoded). GUEST_LOCAL="" skips it.
# Create the matching Email Routing route to your inbox in Cloudflare (DEPLOY.md).
if [ -n "$GUEST_LOCAL" ]; then
  zone=""; cand="$HOST"
  while [[ "$cand" == *.* ]]; do
    found=$(curl -fsS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" "https://api.cloudflare.com/client/v4/zones?name=$cand" | jq -r '.result[0].name // empty')
    if [ -n "$found" ]; then zone="$found"; break; fi
    cand="${cand#*.}"
  done
  [ -n "$zone" ] || { echo "no Cloudflare zone found for $HOST (token needs Zone: Read)" >&2; exit 1; }
  EMAILS="$EMAILS,$GUEST_LOCAL@$zone"; echo "guest alias added: $GUEST_LOCAL@$zone"
fi

# OpenTofu state and plan encryption (TF_ENCRYPTION), in both modes: R2 has no customer-managed keys and no
# object versioning. One-line JSON, so it fits app/.env and a GitHub secret. An existing key is always kept, from
# whichever source has it (switching modes carries it over): a new one would make the encrypted state unreadable.
new_tfe() {
  jq -cn --arg p "$(openssl rand -base64 48 | tr -d '\n')" '{key_provider:{pbkdf2:{main:{passphrase:$p}}},
    method:{aes_gcm:{main:{keys:"key_provider.pbkdf2.main"}}},
    state:{method:"method.aes_gcm.main",enforced:true}, plan:{method:"method.aes_gcm.main",enforced:true}}'
}
env_tfe() { [ -f .env ] && sed -n 's/^TF_ENCRYPTION=//p' .env | tail -n 1 | sed "s/^'//; s/'$//"; true; }
op_tfe() { [ -n "${1:-}" ] && command -v op >/dev/null && op read "op://$1/$ITEM/tf_encryption" 2>/dev/null; true; }

if [ "$SOURCE" = 1password ]; then
  VAULT="${usage_vault:-${OP_VAULT:-ai-job-search}}"
  op vault get "$VAULT" >/dev/null 2>&1 || op vault create "$VAULT" >/dev/null
  TFE=$(op_tfe "$VAULT"); [ -n "$TFE" ] || TFE=$(env_tfe); [ -n "$TFE" ] || TFE=$(new_tfe)
  # The secrets go to op on stdin, never on a command line. Re-running replaces the item (the old one is archived).
  op item get "$ITEM" --vault "$VAULT" >/dev/null 2>&1 && op item delete "$ITEM" --vault "$VAULT" --archive
  jq -n --arg t "$CLOUDFLARE_API_TOKEN" --arg h "$HOST" --arg e "$EMAILS" --arg x "$TFE" --arg title "$ITEM" '{
    title: $title, category: "API_CREDENTIAL", fields: [
      {id:"cloudflare_api_token", label:"cloudflare_api_token", type:"CONCEALED", value:$t},
      {id:"hostname", label:"hostname", type:"CONCEALED", value:$h},
      {id:"allowed_emails", label:"allowed_emails", type:"CONCEALED", value:$e},
      {id:"tf_encryption", label:"tf_encryption", type:"CONCEALED", value:$x}]}' | op item create --vault "$VAULT" - >/dev/null
  if [ -z "${usage_keep_service_account:-}" ]; then
    op service-account create "$ITEM-ci-$(date +%Y%m%d)" --vault "$VAULT:read_items,write_items" --raw | gh secret set OP_SERVICE_ACCOUNT_TOKEN -R "$REPO"
  fi
  gh variable set OP_VAULT -R "$REPO" --body "$VAULT"; setenv OP_VAULT "$VAULT"
  for n in "${NAMES[@]}" TF_ENCRYPTION; do gh secret delete "$n" -R "$REPO" >/dev/null 2>&1 || true; setenv "$n"; done
  echo "1Password mode: OP_VAULT=$VAULT (repository variable and app/.env); Cloudflare GitHub secrets and app/.env entries removed."
else
  TFE=$(env_tfe); [ -n "$TFE" ] || TFE=$(op_tfe "${OP_VAULT:-}")
  if [ -z "$TFE" ] && gh secret list -R "$REPO" --json name --jq '.[].name' | grep -qx TF_ENCRYPTION; then
    echo "Keeping the GitHub secret TF_ENCRYPTION (no local copy found, so it is not replaced)."
  else
    [ -n "$TFE" ] || TFE=$(new_tfe)
    printf %s "$TFE" | gh secret set TF_ENCRYPTION -R "$REPO"; setenv TF_ENCRYPTION "'$TFE'"
  fi
  printf %s "$CLOUDFLARE_API_TOKEN" | gh secret set CLOUDFLARE_API_TOKEN -R "$REPO"
  printf %s "$HOST" | gh secret set CLOUDFLARE_HOSTNAME -R "$REPO"
  printf %s "$EMAILS" | gh secret set CLOUDFLARE_ACCESS_ALLOWED_EMAILS -R "$REPO"
  gh variable delete OP_VAULT -R "$REPO" >/dev/null 2>&1 || true; setenv OP_VAULT
  gh secret delete OP_SERVICE_ACCOUNT_TOKEN -R "$REPO" >/dev/null 2>&1 || true
  echo "env mode: GitHub secrets set (the three settings and TF_ENCRYPTION); OP_VAULT removed. TF_ENCRYPTION is also in app/.env: keep a copy, since losing it makes the state unreadable. For local tasks, put the three settings in app/.env too."
fi
echo "Check: mise run settings, then mise run preflight. Then open a PR or run the Dashboard workflow (plan)."
