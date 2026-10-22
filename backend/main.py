"""
GhostAI Backend
===============
- POST /ai          → validate license key, proxy prompt to DeepSeek, return answer
- POST /ai/vision   → same but with base64 image (uses DeepSeek vision model)
- POST /webhook     → Stripe webhook: activate / deactivate keys on payment events
- POST /admin/generate-key  → manually create a key (admin-only)
- GET  /admin/keys          → list all keys (admin-only)

Environment variables — set via  fly secrets set  on Fly.io:
  DEEPSEEK_API_KEY       your DeepSeek API key  (platform.deepseek.com)
  STRIPE_SECRET_KEY      sk_live_...
  STRIPE_WEBHOOK_SECRET  whsec_...
  ADMIN_SECRET           a long random secret for /admin/* routes
  DATABASE_URL           postgresql://...  (Supabase → Settings → Database → URI)
"""

import os
import secrets
import string
import hashlib
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import stripe

# ── Optional: load .env file in development ──────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Database — Supabase Postgres ──────────────────────────────────────────────
# Get the connection string from:
#   Supabase dashboard → Project Settings → Database → Connection string → URI
# It looks like: postgresql://postgres:[password]@db.[ref].supabase.co:5432/postgres
#
# Fly.io requires SSL.  Append  ?sslmode=require  if not already present.
import databases
import sqlalchemy

_RAW_DB_URL = os.getenv("DATABASE_URL", "")
if not _RAW_DB_URL:
    raise RuntimeError("DATABASE_URL is not set. Run: fly secrets set DATABASE_URL=postgresql://...")

# Supabase connection strings use  postgres://  but SQLAlchemy 1.4+ needs  postgresql://
DATABASE_URL = _RAW_DB_URL.replace("postgres://", "postgresql://", 1)

# Ensure SSL is on (Supabase requires it; harmless if already present)
if "sslmode" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"

# asyncpg driver for async FastAPI
DATABASE_URL_ASYNC = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

database   = databases.Database(DATABASE_URL_ASYNC)
metadata   = sqlalchemy.MetaData()

keys_table = sqlalchemy.Table(
    "license_keys",
    metadata,
    sqlalchemy.Column("id",                     sqlalchemy.Integer,  primary_key=True),
    sqlalchemy.Column("key_hash",               sqlalchemy.String,   unique=True, index=True),
    sqlalchemy.Column("key_prefix",             sqlalchemy.String),
    sqlalchemy.Column("stripe_customer_id",     sqlalchemy.String,   nullable=True),
    sqlalchemy.Column("stripe_subscription_id", sqlalchemy.String,   nullable=True),
    sqlalchemy.Column("email",                  sqlalchemy.String,   nullable=True),
    sqlalchemy.Column("active",                 sqlalchemy.Boolean,  default=True),
    sqlalchemy.Column("created_at",             sqlalchemy.DateTime),
    sqlalchemy.Column("last_used",              sqlalchemy.DateTime, nullable=True),
    sqlalchemy.Column("request_count",          sqlalchemy.Integer,  default=0),
    sqlalchemy.Column("hwid",                   sqlalchemy.String,   nullable=True),
)

# Sync engine just for table creation at startup (psycopg2)
_sync_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
engine = sqlalchemy.create_engine(
    _sync_url,
    connect_args={"sslmode": "require"},
)
metadata.create_all(engine)   # creates table if it doesn't exist yet

# ── Config ───────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY      = os.getenv("DEEPSEEK_API_KEY", "")
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
ADMIN_SECRET          = os.getenv("ADMIN_SECRET", "change-me-in-production")

# DeepSeek models (current as of 2026):
#   deepseek-flash    → DeepSeek-V4.1-Flash  (fast, cheap, supports vision)
#   deepseek-v4-pro   → DeepSeek-V4-Pro      (stronger reasoning, no vision)
AI_MODEL        = os.getenv("AI_MODEL",        "deepseek-flash")
AI_VISION_MODEL = os.getenv("AI_VISION_MODEL", "deepseek-flash")  # only flash supports vision
AI_URL          = "https://api.deepseek.com/chat/completions"

stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="GhostAI Backend", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Startup / shutdown ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()

def _generate_raw_key() -> str:
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(32))
    return f"GHOST-{body[:8]}-{body[8:16]}-{body[16:24]}-{body[24:32]}"

async def _validate_key(license_key: str) -> dict:
    """Return the key row if active, raise 403 otherwise."""
    row = await database.fetch_one(
        keys_table.select().where(keys_table.c.key_hash == _hash_key(license_key))
    )
    if not row:
        raise HTTPException(status_code=403, detail="Invalid license key.")
    if not row["active"]:
        raise HTTPException(status_code=403, detail="License key inactive. Renew at ghostai.gg")
    return row

RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "60"))

async def _validate_hwid(row: dict, hwid: str) -> None:
    """
    On first use: lock the key to this HWID and save it.
    On subsequent uses: reject if HWID doesn't match.
    hwid is optional — if the client doesn't send one, skip the check.
    """
    if not hwid:
        return  # old client versions without HWID — tolerated

    stored = row["hwid"]

    if stored is None:
        # First time this key has been used — bind it to this machine
        await database.execute(
            keys_table.update()
            .where(keys_table.c.key_hash == row["key_hash"])
            .values(hwid=hwid)
        )
        return

    if stored != hwid:
        raise HTTPException(
            status_code=403,
            detail="License is locked to a different machine. Contact support."
        )

async def _check_rate_limit(row: dict) -> None:
    """Rough hourly rate limit using request_count + last_used window."""
    # We track a simple rolling window via last_used timestamp.
    # For a real app use Redis; this sqlite-friendly version is good enough.
    last = row["last_used"]
    count = row["request_count"]
    if last is None:
        return
    now = datetime.now(timezone.utc)
    # Make last_used timezone-aware if it isn't (sqlite returns naive)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = (now - last).total_seconds()
    if elapsed < 3600 and count >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {RATE_LIMIT_PER_HOUR} requests/hour."
        )
    # Reset counter each hour
    if elapsed >= 3600:
        await database.execute(
            keys_table.update()
            .where(keys_table.c.key_hash == row["key_hash"])
            .values(request_count=0)
        )

async def _bump_usage(key_hash: str):
    await database.execute(
        keys_table.update()
        .where(keys_table.c.key_hash == key_hash)
        .values(
            last_used=datetime.now(timezone.utc),
            request_count=keys_table.c.request_count + 1,
        )
    )

def _require_admin(x_admin_secret: str = Header(...)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Bad admin secret.")


# ── Request models ────────────────────────────────────────────────────────────
class AIRequest(BaseModel):
    license_key: str
    prompt: str
    hwid: Optional[str] = None   # hardware fingerprint — locked on first use
    system_prompt: Optional[str] = "You are a concise assistant. Give short, direct answers."
    max_tokens: Optional[int] = 1000
    model: Optional[str] = None

class VisionRequest(BaseModel):
    license_key: str
    image_b64: str          # JPEG base64
    hwid: Optional[str] = None   # hardware fingerprint — locked on first use
    max_tokens: Optional[int] = 1000
    model: Optional[str] = None

class AdminGenerateKey(BaseModel):
    email: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None


# ── AI proxy endpoints ────────────────────────────────────────────────────────
@app.post("/ai")
async def ai_proxy(req: AIRequest):
    row = await _validate_key(req.license_key)
    key_hash = _hash_key(req.license_key)
    await _validate_hwid(row, req.hwid or "")
    await _check_rate_limit(row)

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": req.model or AI_MODEL,
        "max_tokens": req.max_tokens,
        # Disable thinking mode — faster and cheaper for puzzle answers
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": req.system_prompt},
            {"role": "user",   "content": req.prompt},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(AI_URL, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        answer = data["choices"][0]["message"]["content"].strip()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI request timed out.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI error: {str(e)[:120]}")

    await _bump_usage(key_hash)
    return {"answer": answer}


@app.post("/ai/vision")
async def ai_vision_proxy(req: VisionRequest):
    row = await _validate_key(req.license_key)
    key_hash = _hash_key(req.license_key)
    await _validate_hwid(row, req.hwid or "")
    await _check_rate_limit(row)

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": req.model or AI_VISION_MODEL,
        "max_tokens": req.max_tokens,
        "thinking": {"type": "disabled"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a concise assistant. Read the screenshot and give a "
                    "direct, helpful plain-text answer."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{req.image_b64}"},
                    },
                    {"type": "text", "text": "What does this show? Help me with it."},
                ],
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(AI_URL, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        answer = data["choices"][0]["message"]["content"].strip()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI request timed out.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI error: {str(e)[:120]}")

    await _bump_usage(key_hash)
    return {"answer": answer}


# ── Stripe webhook ────────────────────────────────────────────────────────────
@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Bad Stripe signature.")

    etype = event["type"]

    # ── New subscription paid → generate and email key ────────────────────────
    if etype in ("checkout.session.completed", "invoice.paid"):
        obj = event["data"]["object"]
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription") or obj.get("id")
        email = obj.get("customer_email") or obj.get("customer_details", {}).get("email")

        # Check if key already exists for this subscription
        existing = await database.fetch_one(
            keys_table.select().where(
                keys_table.c.stripe_subscription_id == subscription_id
            )
        )
        if existing:
            # Reactivate on renewal
            await database.execute(
                keys_table.update()
                .where(keys_table.c.stripe_subscription_id == subscription_id)
                .values(active=True)
            )
        else:
            raw_key = _generate_raw_key()
            await database.execute(
                keys_table.insert().values(
                    key_hash=_hash_key(raw_key),
                    key_prefix=raw_key[:13],
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    email=email,
                    active=True,
                    created_at=datetime.now(timezone.utc),
                    request_count=0,
                )
            )
            # TODO: send raw_key to email here via SendGrid / Resend / etc.
            # For now, log it (remove in production!)
            print(f"[NEW KEY] email={email} key={raw_key}")

    # ── Subscription cancelled / payment failed → deactivate key ─────────────
    elif etype in (
        "customer.subscription.deleted",
        "invoice.payment_failed",
        "customer.subscription.paused",
    ):
        obj = event["data"]["object"]
        subscription_id = obj.get("id") or obj.get("subscription")
        if subscription_id:
            await database.execute(
                keys_table.update()
                .where(keys_table.c.stripe_subscription_id == subscription_id)
                .values(active=False)
            )

    return {"status": "ok"}


# ── Admin endpoints ───────────────────────────────────────────────────────────
@app.post("/admin/generate-key", dependencies=[Depends(_require_admin)])
async def admin_generate_key(body: AdminGenerateKey):
    raw_key = _generate_raw_key()
    await database.execute(
        keys_table.insert().values(
            key_hash=_hash_key(raw_key),
            key_prefix=raw_key[:13],
            stripe_customer_id=body.stripe_customer_id,
            stripe_subscription_id=body.stripe_subscription_id,
            email=body.email,
            active=True,
            created_at=datetime.now(timezone.utc),
            request_count=0,
            hwid=None,   # unbound until first use
        )
    )
    return {"key": raw_key, "email": body.email}


@app.get("/admin/keys", dependencies=[Depends(_require_admin)])
async def admin_list_keys():
    rows = await database.fetch_all(keys_table.select().order_by(keys_table.c.created_at.desc()))
    return [
        {
            "id": r["id"],
            "key_prefix": r["key_prefix"],
            "email": r["email"],
            "active": r["active"],
            "request_count": r["request_count"],
            "last_used": r["last_used"],
            "created_at": r["created_at"],
            "hwid_locked": r["hwid"] is not None,   # true = key is bound to a machine
        }
        for r in rows
    ]


@app.post("/admin/revoke/{key_id}", dependencies=[Depends(_require_admin)])
async def admin_revoke_key(key_id: int):
    await database.execute(
        keys_table.update().where(keys_table.c.id == key_id).values(active=False)
    )
    return {"status": "revoked"}


# ── Download endpoint ─────────────────────────────────────────────────────────
# PUT your actual release files somewhere publicly accessible (e.g. Fly.io volume,
# S3, R2, or GitHub Releases) and update the URLs below.
# The endpoint validates the license key BEFORE handing out the download URL,
# so only paying users can get the binaries.

DOWNLOAD_URLS = {
    "windows": os.getenv("DOWNLOAD_URL_WINDOWS", "https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/download/v1.0/Ghost_AI-setup.exe"),
    "linux":   os.getenv("DOWNLOAD_URL_LINUX",   "https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/download/v1.0/Ghost_AI-linux"),
    "mac":     os.getenv("DOWNLOAD_URL_MAC",      "https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/download/v1.0/Ghost_AI-mac.zip"),
}

class DownloadRequest(BaseModel):
    license_key: str
    platform: str   # "windows" | "linux" | "mac"

@app.post("/download")
async def download(req: DownloadRequest):
    """
    Validate the license key, then return a one-time download URL for the
    requested platform binary.  The frontend calls this after the user enters
    their key and picks their OS.
    """
    if req.platform not in DOWNLOAD_URLS:
        raise HTTPException(status_code=400, detail="Unknown platform. Use: windows, linux, mac")

    await _validate_key(req.license_key)   # raises 403 if invalid/inactive

    url = DOWNLOAD_URLS[req.platform]
    return {"url": url, "platform": req.platform}


@app.post("/admin/reset-hwid/{key_id}", dependencies=[Depends(_require_admin)])
async def admin_reset_hwid(key_id: int):
    """
    Call this when a paying user gets a new machine and their key stops working.
    Clears the stored HWID so the next request re-locks to the new machine.
    """
    await database.execute(
        keys_table.update().where(keys_table.c.id == key_id).values(hwid=None)
    )
    return {"status": "hwid_cleared"}
