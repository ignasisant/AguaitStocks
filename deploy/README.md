# Deploy — Oracle Cloud Always Free VM + Cloudflare Tunnel

Runs the dashboard container on a free-forever Oracle ARM VM, exposed over a
Cloudflare Tunnel (no open ports, no public IP, TLS handled by Cloudflare).
Chosen after HF moved Docker Spaces behind PRO; the same `Dockerfile` runs
here unchanged.

```
  Google login ─► stocks.<domain> ─► Cloudflare edge ─► Tunnel ─► cloudflared ─► app:8501
                                                        (outbound from the VM only)
```

## 1. Create the VM (Oracle console)

1. Sign up / log in: <https://cloud.oracle.com> (Always Free needs a card for
   identity verification; it is **not** charged while you stay on Always-Free
   shapes).
2. **Compute → Instances → Create instance**:
   - **Image**: Canonical Ubuntu 24.04 (aarch64).
   - **Shape**: `VM.Standard.A1.Flex` → 2 OCPU / 12 GB (or up to 4/24, all
     free). If capacity is unavailable in your home region, retry or pick
     another AD/region.
   - **Networking**: keep the default VCN, assign a public IPv4.
   - **SSH keys**: upload your public key (or let it generate one).
   - **Advanced → Management → cloud-init script**: paste
     [`cloud-init.yaml`](cloud-init.yaml) (installs Docker + clones the repo).
3. Create. Note the **public IP**.

No ingress rule is needed — the tunnel is outbound. (If you skip cloud-init,
SSH in and install Docker manually, then `git clone` the repo to
`/opt/aguait-stocks`.)

## 2. Create the Cloudflare Tunnel

Requires a domain on your Cloudflare account (e.g. `amphoralogistics.com`).

1. <https://one.dash.cloudflare.com> → **Networks → Tunnels → Create a tunnel**
   → **Cloudflared** → name it `aguait-stocks`.
2. On the install screen, copy the **token** (the long string after
   `--token` in the shown command). That is `TUNNEL_TOKEN`.
3. **Public Hostname → Add**:
   - **Subdomain**: `stocks` · **Domain**: your domain → URL becomes
     `https://stocks.<domain>`.
   - **Service**: **HTTP** → `app:8501`  ← the compose service name/port.
4. Save. Cloudflare creates the DNS record automatically.

## 3. Configure secrets on the VM

SSH in as `ubuntu`, then:

```bash
cd /opt/aguait-stocks/deploy

cp .env.example .env
# edit .env → paste TUNNEL_TOKEN

# copy your real Streamlit secrets to deploy/secrets.toml (scp from your Mac):
#   scp .streamlit/secrets.toml ubuntu@<VM_IP>:/opt/aguait-stocks/deploy/secrets.toml
```

In that `secrets.toml`, set the deployed URL:

```toml
[auth]
redirect_uri = "https://stocks.<domain>/oauth2callback"
```

Include `[storage]` (R2) so user data persists — the VM disk is not backed up.

## 4. Update Google OAuth

Google console → your OAuth client → **Authorized redirect URIs** → add
`https://stocks.<domain>/oauth2callback` (must equal `redirect_uri` above).

## 5. Launch

```bash
cd /opt/aguait-stocks/deploy
docker compose up -d --build      # first build ~3-5 min on ARM
docker compose logs -f cloudflared # expect "Registered tunnel connection"
docker compose logs -f app         # expect "You can now view your Streamlit app"
```

Open `https://stocks.<domain>` — full page, no watermark, never sleeps.

## Updating later

```bash
cd /opt/aguait-stocks && git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

(Optional: a GitHub Actions SSH-deploy job can automate this, like
`deploy-hf.yml` did — ask if you want it.)

## Notes

- **Own IP** — Yahoo/yfinance throttles shared cloud IPs (Streamlit Cloud);
  a dedicated VM IP sidesteps that.
- **Persistence** — nothing on the box is precious; `data/users/` is mirrored
  to R2 (`[storage]`). A rebuild/redeploy restores from the bucket.
- **Cost guardrail** — stay on `VM.Standard.A1.Flex` within 4 OCPU / 24 GB and
  the boot volume within 200 GB total to remain on Always Free.
