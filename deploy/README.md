# Deploy: 24/7 on a Hetzner Cloud VM

Runs the responder always-on: headful Chromium under **Xvfb**, managed by
**systemd**, with **VNC** (over SSH tunnel) for the one-time interactive logins.

Recommended box: **Hetzner Cloud CX22** (2 vCPU / 4 GB, EU region — an EU IP
reduces Google/rental-site bot-flagging and is low-latency to the NL sites).

## 1. Create the server
Console or CLI (`hcloud`):
```bash
hcloud server create --name stekkies --type cx22 --image ubuntu-24.04 \
  --location nbg1 --ssh-key "your-key"
```

## 2. Give the server access to the private repo
On the server, create a key and add it to GitHub as a **deploy key** (read-only):
```bash
ssh-keygen -t ed25519 -C stekkies -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub   # -> GitHub repo > Settings > Deploy keys > Add
```

## 3. Provision
```bash
git clone git@github.com:caldaibis/browser-agent.git
bash browser-agent/deploy/setup.sh
```
This installs uv + Chromium + Xvfb + VNC, deploys the systemd units, and starts
`xvfb` + `browser-host` (but NOT the orchestrator yet).

## 4. Upload secrets (from your laptop)
These are gitignored and must be copied up:
```bash
# Gmail OAuth client + token + rental-site credentials:
scp deploy/../state/gmail_client_secret.json deploy@SERVER:/home/deploy/browser-agent/state/
scp state/sources_credentials.json           deploy@SERVER:/home/deploy/browser-agent/state/
# Hermes API keys / config:
scp -r ~/.hermes deploy@SERVER:/home/deploy/
```
(Optionally also copy `state/chromium-profile` to try reusing sessions, but expect
Google to require a fresh login from the new IP — step 5 covers that.)

## 5. One-time interactive logins via VNC
```bash
sudo systemctl start vnc.service
# from your laptop:
ssh -L 5900:localhost:5900 deploy@SERVER
# connect any VNC viewer to localhost:5900 -> in the Chromium window:
#   sign into Google FIRST (enables SSO), then Stekkies + the rental sites.
sudo systemctl stop vnc.service
```
Sessions persist in `state/chromium-profile`, so this is one-time.

## 6. Go live
```bash
sudo systemctl enable --now orchestrator.service
journalctl -u orchestrator -f          # live logs
```

## Ops
```bash
systemctl status browser-host orchestrator xvfb
journalctl -u browser-host -f
tail -f ~/browser-agent/logs/activity.log       # one concise line per email/listing
tail -f ~/browser-agent/logs/mail_summary.jsonl # structured per-mail outcomes
sudo systemctl restart browser-host    # reattach a clean browser
git -C ~/browser-agent pull && sudo systemctl restart orchestrator   # deploy update
```

## Notes
- Flip `DRY_RUN=False` in `src/config.py` (commit + pull) only after a good dry run.
- `browser-host` keeps the CDP browser on `:9222`; `orchestrator` and Hermes
  attach to it. If logins expire, re-run step 5.
- Keep VNC stopped except during logins; it's localhost-only + SSH-tunneled, but
  no reason to leave it running.
