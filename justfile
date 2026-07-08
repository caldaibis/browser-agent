# Stekkies agent — task runner.  Run `just` to list commands.

# Auto-load .env (gitignored) so local secrets like VPS_HOST feed the recipes.
set dotenv-load := true

# VPS connection details — override via env (e.g. export VPS_HOST=root@1.2.3.4).
home    := env_var('HOME')
key     := env_var_or_default('VPS_SSH_KEY_PATH', home / ".ssh/id_ed25519")
vps     := env_var_or_default('VPS_HOST', "root@your-server-ip")
remote  := env_var_or_default('VPS_REMOTE_DIR', "/home/deploy/browser-agent")
domain  := env_var_or_default('DASHBOARD_DOMAIN', "your-agent.example.org")
ssh     := "ssh -o BatchMode=yes -i " + key + " " + vps

default:
    @just --list

# ---------------------------------------------------------------- local dev ---
# install/refresh the Python env from uv.lock
sync:
    uv sync

# offline sanity: lint + byte-compile + unit tests + import smoke + render the apply prompt (CI runs this)
# The unit tests are part of this gate ON PURPOSE: the self-improvement agent's
# verify step is `just check` (SELF_IMPROVEMENT_VERIFY_CMD), and an autonomous
# patch that pushes straight to main must not pass on lint alone.
check:
    uv run ruff check .
    uv run python -m compileall -q src
    uv run python -m unittest discover tests
    uv run python -m src.self_improvement_harness apply-eval
    uv run python -c "from src.apply import build_prompt; import src.browser_agent, src.orchestrator, src.stekkies, src.applicant_profile, src.credentials, src.gmail_watch, src.notify; import src.poller.watcher, src.poller.discover, src.poller.registry, src.poller.sniff; build_prompt({'source_url': 'https://example.test/x', 'address': 'Teststraat 1', 'price': 'EUR 1500', 'source_name': 'Kamernet'}); print('check ok')"

# preflight: verify everything needed to run the agent locally is in place.
doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    [ -f state/agent.env ] && set -a && . state/agent.env && set +a
    rc=0
    chk(){ if eval "$2" >/dev/null 2>&1; then printf "  ok    %s\n" "$1"; else printf "  FAIL  %s\n" "$1"; rc=1; fi; }
    echo "preflight:"
    chk "uv installed"                 "command -v uv"
    chk "node/npx present (MCP)"       "command -v npx"
    chk "DEEPSEEK_API_KEY set"         '[ -n "${DEEPSEEK_API_KEY:-}" ]'
    chk "claude CLI present (self-improvement)" "command -v claude"
    chk "documents/ non-empty"         '[ -n "$(ls -A documents 2>/dev/null)" ]'
    chk "CDP browser reachable :9222"  "curl -sf http://127.0.0.1:9222/json/version"
    chk "LiteLLM proxy reachable :4000 (self-improvement)" "curl -sf http://127.0.0.1:4000/health/liveliness"
    [ $rc -eq 0 ] && echo "all good" || echo "see FAILs above (start the host with 'just host', the claude CLI with 'just ensure-claude-cli', the proxy with 'just litellm-proxy')"
    exit $rc

# install the claude CLI locally if missing (self-improvement agent; no sudo attempted -- if your npm needs it, run this yourself)
ensure-claude-cli:
    command -v claude >/dev/null 2>&1 && echo "claude CLI already present: $(command -v claude)" || npm install -g @anthropic-ai/claude-code

# print the exact apply prompt for a saved listing JSON, without running anything.
dry-prompt path="logs/last_listing.json":
    uv run python -c "import json; from src.apply import build_prompt; print(build_prompt(json.load(open('{{path}}'))))"

# run the dashboard locally at http://127.0.0.1:8000
dashboard:
    uv run uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000 --reload

# run the always-on browser host locally (headed, via WSLg)
host:
    uv run python -m src.browser_host

# run the LiteLLM proxy the self-improvement agent uses to reach DeepSeek
# (Claude Code's ANTHROPIC_BASE_URL points here; loopback-only, like CDP :9222)
litellm-proxy:
    uv run litellm --config deploy/litellm.config.yaml --port 4000 --host 127.0.0.1

# open the browser host and the login pages for the one-time sign-ins
login:
    uv run python -m src.browser_host --login

# process a single Stekkies listing now (applies + submits)
once url:
    uv run python -m src.orchestrator --once "{{url}}"

# run the live inbox watcher locally
watch:
    uv run python -m src.orchestrator

# run the active site poller locally (watches all enabled sources directly)
poll:
    uv run python -m src.poller.watcher

# one diagnostic poll of a single site (no apply), e.g. `just poll-once pararius.nl`
poll-once name:
    uv run python -m src.poller.watcher --once "{{name}}"

# probe every registered site: which tier currently yields listings
discover *names:
    uv run python -m src.poller.discover {{names}}

# sniff a site's JSON/XHR APIs for tier-1 discovery, e.g. `just sniff vesteda.com`
sniff target:
    uv run python -m src.poller.sniff "{{target}}"

# run the health check locally (credit + Stekkies login)
healthcheck:
    uv run python -m src.healthcheck

# Mine recent failed apply transcripts into clustered self-improvement evidence.
self-improve-mine:
    uv run python -m src.self_improvement_harness mine

# Run offline self-improvement harness regression fixtures.
self-improve-eval:
    uv run python -m src.self_improvement_harness eval

# Run offline apply-agent harness regression fixtures.
self-improve-apply-eval:
    uv run python -m src.self_improvement_harness apply-eval

# print the weekly outcome digest (outcomes, guards, incidents, pending fixes)
digest:
    uv run python -m src.digest

# re-authorize Gmail (prints a consent URL to open in any browser)
reauth:
    uv run python -m src.reauth

# import a Google Password Manager CSV into state/sources_credentials.json
import-passwords file="passwords.csv":
    uv run python -m src.import_passwords "{{file}}"

# list current match listing URLs (pages 1..N)
matches pages="2":
    uv run python -m src.matches {{pages}}

# ------------------------------------------------------------ git / deploy ---
# commit everything with a message and push
commit msg:
    git add -A && git commit -m "{{msg}}" && git push

# pull latest on the VPS, sync deps, and restart the agent + dashboard
deploy:
    git push
    {{ssh}} 'sudo -u deploy git -C {{remote}} pull --ff-only'
    {{ssh}} 'sudo -u deploy bash -lc "cd {{remote}} && uv sync"'
    {{ssh}} 'bash {{remote}}/deploy/ensure-self-improvement.sh'
    {{ssh}} 'systemctl restart orchestrator dashboard; systemctl is-active poller >/dev/null 2>&1 && systemctl restart poller || true'
    @echo "deployed + restarted"

# just pull latest on the VPS (no restart)
pull:
    {{ssh}} 'sudo -u deploy git -C {{remote}} pull --ff-only'

# ------------------------------------------------------------------ vps ops ---
# run an arbitrary command on the VPS as root, e.g. `just vps 'df -h'`
vps +cmd:
    {{ssh}} {{cmd}}

# interactive SSH into the VPS
shell:
    ssh -i {{key}} {{vps}}

# follow the orchestrator (agent) journal
logs:
    {{ssh}} 'journalctl -u orchestrator -f'

# follow the dashboard journal
dash-logs:
    {{ssh}} 'journalctl -u dashboard -f'

# tail the human-readable activity log
activity:
    {{ssh}} 'tail -f {{remote}}/logs/activity.log'

# status of all services
status:
    {{ssh}} 'for s in orchestrator poller browser-host dashboard caddy xvfb healthcheck.timer; do printf "%-18s %s\\n" "$s" "$(systemctl is-active $s)"; done'

# restart a service, e.g. `just restart browser-host`
restart svc:
    {{ssh}} 'systemctl restart {{svc}}'

# pause / resume the live inbox watcher
pause:
    {{ssh}} 'systemctl stop orchestrator' && echo paused
resume:
    {{ssh}} 'systemctl start orchestrator' && echo resumed

# follow the poller journal
poll-logs:
    {{ssh}} 'journalctl -u poller -f'

# pause / resume the active site poller
poll-pause:
    {{ssh}} 'systemctl stop poller' && echo paused
poll-resume:
    {{ssh}} 'systemctl start poller' && echo resumed

# remaining DeepSeek credit (+ Stekkies login) via the health check on the VPS
credits:
    {{ssh}} 'systemctl start healthcheck.service && sleep 4 && journalctl -u healthcheck.service -n 5 --no-pager | grep -iE "credit|stekkies"'

# start VNC for one-time logins, and print the tunnel command
vnc:
    {{ssh}} 'systemctl start vnc.service'
    @echo "Now run:  ssh -L 5900:localhost:5900 -i {{key}} {{vps}}"
    @echo "Then point a VNC viewer at localhost:5900 and log in. After: just vnc-stop"
vnc-stop:
    {{ssh}} 'systemctl stop vnc.service'

# ------------------------------------------------------------ secrets push ---
# push updated rental-site credentials to the VPS (+ refresh dashboard redaction)
push-creds:
    scp -i {{key}} state/sources_credentials.json {{vps}}:{{remote}}/state/
    {{ssh}} 'chown deploy:deploy {{remote}}/state/sources_credentials.json && systemctl restart dashboard'
    @echo "credentials updated on VPS"

# push the Gmail token (after a local `just reauth`)
push-token:
    scp -i {{key}} state/gmail_token.json {{vps}}:{{remote}}/state/
    {{ssh}} 'chown deploy:deploy {{remote}}/state/gmail_token.json && systemctl restart orchestrator'
    @echo "gmail token updated on VPS"

# push agent.env (DEEPSEEK_API_KEY / model overrides), then restart agent
push-env:
    scp -i {{key}} state/agent.env {{vps}}:{{remote}}/state/
    {{ssh}} 'chown deploy:deploy {{remote}}/state/agent.env && systemctl restart orchestrator dashboard'
    @echo "agent.env updated on VPS"

# ------------------------------------------------------------------ dashboard ---
# print the dashboard URL
open:
    @echo "https://{{domain}}  (Basic Auth — your dashboard user)"
