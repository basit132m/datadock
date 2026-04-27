# DataDock – Deployment Guide

## Architecture

```
Browser ──(init/presign/complete)──► FastAPI (port 8000) ──► SQLite
   │
   └──(PUT chunk, 10 MB each)──────────────────────────────► Backblaze B2
```

File data **never passes through the backend**. Only tiny coordination calls do.
This gives you full upload speed straight to B2, with the backend staying lean.

---

## 1. Backblaze B2 – Required Setup

### 1a. Bucket CORS rules

Your B2 bucket must allow PUT requests from the browser.
Go to **B2 Cloud Storage → Buckets → your-bucket → CORS Rules** and add:

```json
[
  {
    "corsRuleName": "datadock",
    "allowedOrigins": ["https://datadock-host.site"],
    "allowedOperations": ["s3_put"],
    "allowedHeaders": ["*"],
    "exposeHeaders": ["ETag"],
    "maxAgeSeconds": 3600
  }
]
```

> `exposeHeaders: ["ETag"]` is **critical** — the browser must read the ETag
> from each part response to assemble the final multipart upload.

### 1b. Application Key

Create a key with **Read + Write** permissions scoped to your bucket.
Note down:
- **keyID** → `B2_KEY_ID`
- **applicationKey** → `B2_APPLICATION_KEY`
- **endpoint** (shown on bucket page, e.g. `https://s3.us-west-004.backblazeb2.com`) → `B2_ENDPOINT_URL`

---

## 2. Configuration

```bash
cp backend/.env.example backend/.env
nano backend/.env        # fill in B2 credentials + API_KEY
```

Generate a strong API key:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## 3a. Deploy with Docker (recommended)

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f

# Restart after .env changes
docker compose restart
```

The app listens on `127.0.0.1:8000`.

---

## 3b. Deploy without Docker

```bash
bash start.sh
```

The script creates a venv, installs deps, and launches uvicorn.

---

## 4. Nginx + HTTPS

```bash
# Install certbot if needed
sudo apt install certbot python3-certbot-nginx

# Copy nginx config
sudo cp nginx.conf /etc/nginx/sites-available/datadock
sudo ln -s /etc/nginx/sites-available/datadock /etc/nginx/sites-enabled/datadock

# Obtain certificate
sudo certbot --nginx -d datadock-host.site

# Reload
sudo nginx -s reload
```

---

## 5. Systemd service (bare-metal alternative to Docker)

```ini
# /etc/systemd/system/datadock.service
[Unit]
Description=DataDock upload service
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/datadock/backend
EnvironmentFile=/opt/datadock/backend/.env
ExecStart=/opt/datadock/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now datadock
```

---

## 6. Team Access

Share the URL `https://datadock-host.site` and the value of `API_KEY` with your team.
Each member pastes the key on first visit; it is stored in their browser's `localStorage`.

---

## Limits

| Setting | Default | Where to change |
|---------|---------|-----------------|
| Max file size | 10 GB | `MAX_FILE_SIZE_GB` in `.env` |
| Chunk size | 10 MB | `CHUNK_SIZE` in `frontend/app.js` |
| Parallel chunks | 3 | `MAX_CONC` in `frontend/app.js` |
| File list shown | 200 most recent | `main.py` → `list_files` |
