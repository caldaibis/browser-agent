# Stekkies agent — task runner.  Run `just` to list commands.

home    := env_var('HOME')
key     := home / ".ssh/id_ed25519"
vps     := "root@your-server-ip"
remote  := "/home/deploy/browser-agent"
domain  := "your-agent.example.org"
ssh     := "ssh -o BatchMode=yes -i " + key + " " + vps

default:
    @just --list

# ---------------------------------------------------------------- local dev ---
# install/refresh the Python env from uv.lock
sync:
    uv sync

# run the dashboard locally at http://127.0.0.1:8000
dashboard:
    uv run uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000 --reload

# run the always-on browser host locally (headed, via WSLg)
host:
    uv run python -m src.browser_host

# open the browser host and the login pages for the one-time sign-ins
login:
    uv run python -m src.browser_host --login

# process a single Stekkies listing now (applies + submits)
once url:
    uv run python -m src.orchestrator --once "{{url}}"

# run the live inbox watcher locally
watch:
    uv run python -m src.orchestrator

# run the health check locally (credit + Stekkies login)
healthcheck:
    uv run python -m src.healthcheck

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
    {{ssh}} 'systemctl restart orchestrator dashboard'
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
    {{ssh}} 'for s in orchestrator browser-host dashboard caddy xvfb healthcheck.timer; do printf "%-18s %s\\n" "$s" "$(systemctl is-active $s)"; done'

# restart a service, e.g. `just restart browser-host`
restart svc:
    {{ssh}} 'systemctl restart {{svc}}'

# pause / resume the live inbox watcher
pause:
    {{ssh}} 'systemctl stop orchestrator' && echo paused
resume:
    {{ssh}} 'systemctl start orchestrator' && echo resumed

# remaining OpenRouter credit (+ Stekkies login) via the health check on the VPS
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

# push agent.env (OPENROUTER_API_KEY / model overrides), then restart agent
push-env:
    scp -i {{key}} state/agent.env {{vps}}:{{remote}}/state/
    {{ssh}} 'chown deploy:deploy {{remote}}/state/agent.env && systemctl restart orchestrator dashboard'
    @echo "agent.env updated on VPS"

# ------------------------------------------------------------------ dashboard ---
# print the dashboard URL
open:
    @echo "https://{{domain}}  (login: caldaibis)"
