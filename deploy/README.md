# Deploy — Oracle Cloud Always Free VM + DuckDNS + Caddy

Runs the dashboard container on a free-forever Oracle ARM VM, with a free
DuckDNS hostname and automatic HTTPS via Caddy (Let's Encrypt). Chosen after
HF moved Docker Spaces behind PRO, and because no owned domain is available
(so Cloudflare Tunnel isn't an option). The same `Dockerfile` runs here
unchanged.

```
  Google login ─► https://<name>.duckdns.org ─► Caddy :443 (auto-TLS) ─► app:8501
                  (DNS: DuckDNS → VM public IP)
```

## 1. Create the VM (Oracle console)   ✅ done

`VM.Standard.A1.Flex`, 2 OCPU / 12 GB, Ubuntu 24.04 aarch64, cloud-init from
[`cloud-init.yaml`](cloud-init.yaml) (installs Docker, opens 80/443 on the host
firewall, clones the repo to `/opt/aguait-stocks`). Note the **public IP**.

## 2. DuckDNS hostname

1. <https://www.duckdns.org> → sign in (Google/GitHub).
2. Add a subdomain, e.g. `aguait-stocks` → gives `aguait-stocks.duckdns.org`.
3. Set its IP to the VM's **public IP** (the `duckdns` container also keeps
   this updated automatically once running).
4. Copy your **token** (top of the page — one per account).

## 3. Open ports 80 + 443 in the VCN security list

The host firewall is handled by cloud-init, but Oracle's cloud firewall (the
VCN security list) still blocks inbound. In the console:

**Networking → Virtual Cloud Networks →** your VCN **→ Security Lists →**
the default list **→ Add Ingress Rules**, twice:

| Source CIDR | IP Protocol | Destination Port |
|-------------|-------------|------------------|
| `0.0.0.0/0` | TCP         | `80`             |
| `0.0.0.0/0` | TCP         | `443`            |

(Port 80 is required for the Let's Encrypt HTTP challenge and the redirect to
443; leave both open.)

## 4. Secrets on the VM

From your Mac, copy the real Streamlit secrets up:

```bash
scp .streamlit/secrets.toml ubuntu@<VM_IP>:/opt/aguait-stocks/deploy/secrets.toml
```

SSH in and set the env:

```bash
ssh ubuntu@<VM_IP>
cd /opt/aguait-stocks && git pull        # get the latest deploy/ files
cd deploy
cp .env.example .env
# edit .env → SITE_ADDRESS=<name>.duckdns.org, DUCKDNS_SUBDOMAIN=<name>, DUCKDNS_TOKEN=...
nano .env
```

In `deploy/secrets.toml` set the deployed URL and keep R2 storage:

```toml
[auth]
redirect_uri = "https://<name>.duckdns.org/oauth2callback"

[storage]   # keep this — the VM disk is not backed up; user data lives in R2
# endpoint_url / bucket / access_key_id / secret_access_key ...
```

## 5. Update Google OAuth

[Google console → Credentials](https://console.cloud.google.com/apis/credentials)
→ your OAuth client → **Authorized redirect URIs** → add
`https://<name>.duckdns.org/oauth2callback` (must equal `redirect_uri` above).

## 6. Launch

```bash
cd /opt/aguait-stocks/deploy
docker compose up -d --build      # first build ~3-5 min on ARM
docker compose logs -f caddy      # expect "certificate obtained successfully"
docker compose logs -f app        # expect "You can now view your Streamlit app"
```

Open `https://<name>.duckdns.org` — full page, real HTTPS, no watermark,
never sleeps.

## Updating later

```bash
cd /opt/aguait-stocks && git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

## Troubleshooting

- **Caddy can't get a cert** → ports 80/443 not reachable. Recheck the VCN
  security-list ingress (step 3) and that DuckDNS points at the current IP
  (`dig +short <name>.duckdns.org`).
- **502 from Caddy** → app still building/unhealthy; `docker compose logs app`.
- **Login loops / redirect_uri mismatch** → the three URLs must match exactly:
  DuckDNS host, `[auth] redirect_uri`, Google console URI (all `https`, same
  subdomain, `/oauth2callback`).

## Notes

- **Own IP** — sidesteps the yfinance throttling that hits shared cloud IPs.
- **Persistence** — nothing on the box is precious; `data/users/` mirrors to
  R2 (`[storage]`). A rebuild restores from the bucket.
- **Always Free** — stay on `VM.Standard.A1.Flex` within 4 OCPU / 24 GB and
  total boot volume ≤ 200 GB.
