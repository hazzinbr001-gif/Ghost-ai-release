# GhostAI — Project Structure

```
ghostai/
├── app/                  # Client-side — the tool your users run
│   ├── ai_proxy_patch.py     Drop-in replacement for call_ai() in main.py
│   ├── build.ps1             Windows build script  → RuntimeBroker.exe
│   ├── build_linux.sh        Linux build script    → RuntimeBroker binary
│   └── build_mac.sh          macOS build script    → GhostAI.app
│
├── web/                  # Payment landing page
│   └── index.html            Host on Netlify/Vercel — update Stripe link inside
│
├── backend/              # API server — deploy to Fly.io
│   ├── main.py               FastAPI app (license validation + AI proxy)
│   ├── requirements.txt      Python dependencies
│   ├── .env.example          Copy to .env and fill in your keys
│   ├── Dockerfile            Container build
│   └── fly.toml              Fly.io deployment config
│
└── sql/
    └── schema.sql            Postgres schema + useful admin queries
```

## Deploy order

1. **Backend** — upload the release assets listed below to the GitHub Release tagged `v1.0`, then deploy:

   ```bash
   cd backend
   fly secrets set \
     DOWNLOAD_URL_WINDOWS="https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/download/v1.0/Ghost_AI-setup.exe" \
     DOWNLOAD_URL_LINUX="https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/download/v1.0/Ghost_AI-linux" \
     DOWNLOAD_URL_MAC="https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/download/v1.0/Ghost_AI-mac.zip"
   fly deploy
   ```

   The required GitHub Release assets are `Ghost_AI-setup.exe`, `Ghost_AI-linux`, and `Ghost_AI-mac.zip`.
2. **Web** — drag `web/` folder to Netlify drop. Update the Stripe link in `index.html`
3. **App** — copy `app/ai_proxy_patch.py` functions into your `main.py`, update `config.json`, build with the right script for each OS

## config.json (add these two keys, remove api_key)

```json
{
  "license_key": "GHOST-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX",
  "backend_url": "https://ghosty.fly.dev",
  "overlay_opacity": 160,
  "max_tokens": 1000,
  "auto_start": true
}
```

## Admin API (use with your ADMIN_SECRET header)

| Method | Path | What it does |
|--------|------|--------------|
| POST | `/admin/generate-key` | Create a key manually |
| GET  | `/admin/keys` | List all keys + usage |
| POST | `/admin/revoke/{id}` | Kill a key immediately |
| POST | `/admin/reset-hwid/{id}` | Unlock key for new machine |

```bash
# Example — generate a key manually
curl -X POST https://ghosty.fly.dev/admin/generate-key \
  -H "x-admin-secret: YOUR_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"email": "friend@email.com"}'
```

## Security layers

- **No API key in the exe** — all AI calls proxied through your backend
- **HWID locking** — key binds to the first machine that uses it
- **Rate limiting** — 60 requests/hour per key (set via `RATE_LIMIT_PER_HOUR`)
- **Stripe webhooks** — cancel subscription = key deactivated within seconds
- **PyArmor** (optional) — obfuscate bytecode before building exe: `pyarmor gen main.py`
