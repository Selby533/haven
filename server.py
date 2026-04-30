from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Cookie, Header
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import os
import logging
import hashlib
import uuid
import random
import httpx
from urllib.parse import quote
from pathlib import Path
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
sb: Client = create_client(SUPABASE_URL.rstrip('/'), SUPABASE_KEY)

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ========= Constants =========
FREE_CARD_TTL_MINUTES = 20
PREMIUM_CARD_TTL_MINUTES = 35
VOTES_PER_TOKEN = 10
INITIAL_AD_TOKENS = 3
DIAMOND_BOOST_COST = 5
DIAMOND_BOOST_MINUTES = 10

# Solana/Phantom Constants
UPGRADE_COST_SOL = 0.012
MONTHLY_SERVICE_FEE_SOL = 0.01
DEFAULT_VOTE_COST_SOL = 0.001

SYSTEM_IMAGES = [
    "https://images.unsplash.com/photo-1723283126758-28f2a308bc47?crop=entropy&cs=srgb&fm=jpg&w=800&q=80",
    "https://images.unsplash.com/photo-1689154345830-861f74006b09?crop=entropy&cs=srgb&fm=jpg&w=800&q=80",
    "https://images.pexels.com/photos/29888428/pexels-photo-29888428.jpeg?auto=compress&cs=tinysrgb&w=800",
    "https://images.pexels.com/photos/25626583/pexels-photo-25626583.jpeg?auto=compress&cs=tinysrgb&w=800",
    "https://images.unsplash.com/photo-1639817754460-9af351966008?crop=entropy&cs=srgb&fm=jpg&w=800&q=80",
    "https://images.unsplash.com/photo-1557672172-298e090bd0f1?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1558865869-c93f6f8482af?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1579547945413-497e1b99dac0?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1618331835717-801e976710b2?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1550684848-fac1c5b4e853?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1604871000636-074fa5117945?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1614850523459-c2f4c699c52e?auto=format&fit=crop&w=800&q=80",
]

# ========= Helpers =========
def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def _maybe(res):
    if res is None:
        return None
    if hasattr(res, 'error') and res.error:
        logger.error(f"Supabase error: {res.error}")
        return None
    return getattr(res, "data", None)

# ========= Models =========
class CardCreate(BaseModel):
    image_url: str
    smart_link: Optional[str] = ""
    title: Optional[str] = ""
    use_diamond_boost: Optional[bool] = False
    card_type: Optional[str] = "smartlink"
    vote_cost_sol: Optional[float] = DEFAULT_VOTE_COST_SOL

class ConnectWalletRequest(BaseModel):
    wallet_address: str

class UpgradeRequest(BaseModel):
    tx_hash: str

class ServiceFeeRequest(BaseModel):
    tx_hash: str

class CryptoVoteRequest(BaseModel):
    card_id: str
    tx_hash: str
    amount_sol: float

class GoogleAuthPayload(BaseModel):
    id_token: str
    email: str
    name: str
    picture: str
    ref: Optional[str] = None

class PayfastInitiatePayload(BaseModel):
    return_url: str
    cancel_url: str

# ========= Auth =========
def get_current_user(
    request: Request,
    session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    res = sb.table("user_sessions").select("*").eq("session_token", token).maybe_single().execute()
    session = _maybe(res)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")

    expires_at = _parse_dt(session["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")

    user_res = sb.table("users").select("*").eq("user_id", session["user_id"]).maybe_single().execute()
    user = _maybe(user_res)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    check_service_fee(user)
    return user

# ========= Root Routes =========
@app.get("/")
def root():
    return {"message": "Stokvel API is running"}

@api_router.get("/")
def api_root():
    return {"message": "Stokvel API"}

@api_router.post("/auth/google")
def auth_google(payload: GoogleAuthPayload, response: Response):
    email = payload.email
    name = payload.name
    picture = payload.picture
    ref = payload.ref
    
    session_token = f"session_{uuid.uuid4().hex[:32]}"
    
    existing = _maybe(sb.table("users").select("*").eq("email", email).maybe_single().execute())
    now_iso = datetime.now(timezone.utc).isoformat()

    if existing:
        user_id = existing["user_id"]
        updates = {"name": name, "picture": picture}
        if not existing.get("referral_code"):
            updates["referral_code"] = uuid.uuid4().hex[:8]
        sb.table("users").update(updates).eq("user_id", user_id).execute()
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        referral_code = uuid.uuid4().hex[:8]
        referred_by = None
        if ref:
            ref_user = _maybe(sb.table("users").select("*").eq("referral_code", ref).maybe_single().execute())
            if ref_user and ref_user["user_id"] != user_id:
                referred_by = ref_user["user_id"]
                sb.table("users").update(
                    {"diamonds": (ref_user.get("diamonds") or 0) + 1}
                ).eq("user_id", ref_user["user_id"]).execute()
        sb.table("users").insert({
            "user_id": user_id,
            "email": email,
            "name": name,
            "picture": picture,
            "ad_tokens": INITIAL_AD_TOKENS,
            "sol_balance": 0.0,
            "is_upgraded": False,
            "is_premium": False,
            "wallet_address": None,
            "diamonds": 0,
            "premium_until": None,
            "upgrade_date": None,
            "last_service_fee_date": None,
            "votes_since_token": 0,
            "referral_code": referral_code,
            "referred_by": referred_by,
            "service_fee_paid": False,
            "created_at": now_iso,
        }).execute()

    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    sb.table("user_sessions").upsert({
        "session_token": session_token,
        "user_id": user_id,
        "expires_at": expires_at.isoformat(),
        "created_at": now_iso,
    }).execute()

    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
        max_age=7 * 24 * 60 * 60,
    )
    return {"ok": True, "user_id": user_id, "token": session_token}

@api_router.get("/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    check_service_fee(user)
    
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user["name"],
        "picture": user.get("picture", ""),
        "ad_tokens": user.get("ad_tokens", 0),
        "sol_balance": user.get("sol_balance", 0),
        "is_upgraded": user.get("is_upgraded", False),
        "is_premium": user.get("is_premium", False),
        "wallet_address": user.get("wallet_address"),
        "diamonds": user.get("diamonds", 0),
        "premium_until": user.get("premium_until"),
        "votes_since_token": user.get("votes_since_token", 0),
        "votes_per_token": VOTES_PER_TOKEN,
        "referral_code": user.get("referral_code"),
        "diamond_boost_cost": DIAMOND_BOOST_COST,
        "diamond_boost_minutes": DIAMOND_BOOST_MINUTES,
        "upgrade_cost_sol": UPGRADE_COST_SOL,
        "monthly_service_fee_sol": MONTHLY_SERVICE_FEE_SOL,
        "vote_cost_sol": DEFAULT_VOTE_COST_SOL,
        "service_fee_paid": user.get("service_fee_paid", False),
        "upgrade_date": user.get("upgrade_date"),
        "last_service_fee_date": user.get("last_service_fee_date"),
    }

@api_router.post("/auth/logout")
def auth_logout(
    response: Response,
    session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"),
    authorization: Optional[str] = Header(default=None),
):
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    if token:
        sb.table("user_sessions").delete().eq("session_token", token).execute()
    response.delete_cookie(key="session_token", path="/", samesite="lax", secure=False)
    return {"ok": True}

# ========= Service Fee Check =========
def check_service_fee(user: dict):
    if not user.get("is_upgraded"):
        return
    
    last_fee = user.get("last_service_fee_date")
    if last_fee:
        last_fee_dt = _parse_dt(last_fee)
        next_fee_due = last_fee_dt + timedelta(days=30)
        if datetime.now(timezone.utc) > next_fee_due:
            try:
                sb.table("users").update({
                    "service_fee_paid": False
                }).eq("user_id", user["user_id"]).execute()
                user["service_fee_paid"] = False
            except Exception as e:
                logger.error(f"Service fee check failed: {e}")
    else:
        now = datetime.now(timezone.utc)
        try:
            sb.table("users").update({
                "last_service_fee_date": now.isoformat(),
                "service_fee_paid": True
            }).eq("user_id", user["user_id"]).execute()
            user["last_service_fee_date"] = now.isoformat()
            user["service_fee_paid"] = True
        except Exception as e:
            logger.error(f"Setting initial service fee date failed: {e}")

# ========= Wallet & Upgrade =========
@api_router.post("/wallet/connect")
def connect_wallet(payload: ConnectWalletRequest, user: dict = Depends(get_current_user)):
    sb.table("users").update({
        "wallet_address": payload.wallet_address
    }).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "wallet_address": payload.wallet_address}

@api_router.post("/upgrade/verify")
def verify_upgrade(payload: UpgradeRequest, user: dict = Depends(get_current_user)):
    if not user.get("wallet_address"):
        raise HTTPException(status_code=400, detail="Connect wallet first")
    
    existing = _maybe(sb.table("sol_transactions").select("tx_id").eq("tx_hash", payload.tx_hash).maybe_single().execute())
    if existing:
        raise HTTPException(status_code=400, detail="Transaction already used")
    
    now = datetime.now(timezone.utc)
    sb.table("sol_transactions").insert({
        "tx_id": f"up_{uuid.uuid4().hex[:12]}",
        "from_user_id": user["user_id"],
        "to_user_id": None,
        "tx_type": "upgrade",
        "amount_sol": UPGRADE_COST_SOL,
        "tx_hash": payload.tx_hash,
        "status": "confirmed",
        "confirmed_at": now.isoformat()
    }).execute()
    
    sb.table("users").update({
        "is_upgraded": True,
        "upgrade_date": now.isoformat(),
        "last_service_fee_date": now.isoformat(),
        "service_fee_paid": True,
        "sol_balance": 0.0
    }).eq("user_id", user["user_id"]).execute()
    
    return {"ok": True, "is_upgraded": True}

@api_router.post("/service-fee/verify")
def verify_service_fee(payload: ServiceFeeRequest, user: dict = Depends(get_current_user)):
    if not user.get("is_upgraded"):
        raise HTTPException(status_code=400, detail="Not an upgraded user")
    
    existing = _maybe(sb.table("sol_transactions").select("tx_id").eq("tx_hash", payload.tx_hash).maybe_single().execute())
    if existing:
        raise HTTPException(status_code=400, detail="Transaction already used")
    
    now = datetime.now(timezone.utc)
    sb.table("sol_transactions").insert({
        "tx_id": f"fee_{uuid.uuid4().hex[:12]}",
        "from_user_id": user["user_id"],
        "to_user_id": None,
        "tx_type": "service_fee",
        "amount_sol": MONTHLY_SERVICE_FEE_SOL,
        "tx_hash": payload.tx_hash,
        "status": "confirmed",
        "confirmed_at": now.isoformat()
    }).execute()
    
    sb.table("users").update({
        "last_service_fee_date": now.isoformat(),
        "service_fee_paid": True
    }).eq("user_id", user["user_id"]).execute()
    
    return {
        "ok": True, 
        "next_fee_due": (now + timedelta(days=30)).isoformat(),
        "service_fee_paid": True
    }

# ========= Cards =========
def _card_public(doc: dict) -> dict:
    return {
        "card_id": doc["card_id"],
        "owner_id": doc["owner_id"],
        "owner_name": doc.get("owner_name", ""),
        "image_url": doc["image_url"],
        "smart_link": doc.get("smart_link", ""),
        "title": doc.get("title", ""),
        "votes": doc.get("votes", 0),
        "created_at": doc["created_at"],
        "expires_at": doc["expires_at"],
        "is_premium": doc.get("is_premium", False),
        "diamond_boosted": doc.get("diamond_boosted", False),
        "card_type": doc.get("card_type", "smartlink"),
        "vote_cost_sol": doc.get("vote_cost_sol", DEFAULT_VOTE_COST_SOL),
        "owner_wallet": doc.get("owner_wallet"),
    }

@api_router.post("/cards")
def create_card(payload: CardCreate, user: dict = Depends(get_current_user)):
    card_type = payload.card_type
    
    if card_type == "crypto":
        if not user.get("is_upgraded"):
            raise HTTPException(status_code=402, detail="Upgrade required for crypto cards")
        if not user.get("wallet_address"):
            raise HTTPException(status_code=400, detail="Connect wallet first")
        if not user.get("service_fee_paid"):
            raise HTTPException(status_code=402, detail="Service fee payment required")
        
        token_cost = 0
        smart_link = ""
        recipient_wallet = user.get("wallet_address")
    else:
        if user.get("ad_tokens", 0) < 1:
            raise HTTPException(status_code=402, detail="Not enough ad tokens")
        token_cost = 1
        
        if not payload.smart_link or not payload.smart_link.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="smart_link must be a valid URL")
        smart_link = payload.smart_link
        recipient_wallet = None
    
    if not payload.image_url:
        raise HTTPException(status_code=400, detail="image_url is required")

    base_ttl = PREMIUM_CARD_TTL_MINUTES if user.get("is_premium") else FREE_CARD_TTL_MINUTES
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=base_ttl)
    
    card = {
        "card_id": f"card_{uuid.uuid4().hex[:12]}",
        "owner_id": user["user_id"],
        "owner_name": user.get("name", ""),
        "image_url": payload.image_url,
        "smart_link": smart_link,
        "title": payload.title or "",
        "votes": 0,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "is_premium": bool(user.get("is_premium", False)),
        "diamond_boosted": False,
        "card_type": card_type,
        "vote_cost_sol": payload.vote_cost_sol if card_type == "crypto" else 0.0,
        "owner_wallet": recipient_wallet,
    }
    sb.table("cards").insert(card).execute()

    if token_cost > 0:
        new_tokens = user["ad_tokens"] - 1
        sb.table("users").update({"ad_tokens": new_tokens}).eq("user_id", user["user_id"]).execute()
    
    if payload.use_diamond_boost:
        new_diamonds = user.get("diamonds", 0) - DIAMOND_BOOST_COST
        sb.table("users").update({"diamonds": new_diamonds}).eq("user_id", user["user_id"]).execute()
    
    return _card_public(card)

@api_router.get("/cards/marketplace")
def get_marketplace(
    user: dict = Depends(get_current_user),
    filter_type: Optional[str] = None
):
    """Get marketplace. Free users see only smartlink. Upgraded users can filter."""
    now_iso = datetime.now(timezone.utc).isoformat()
    is_upgraded = user.get("is_upgraded", False)
    
    query = sb.table("cards").select("*").gt("expires_at", now_iso).neq("owner_id", user["user_id"])
    
    if not is_upgraded:
        # Free users only see SmartLink cards
        query = query.or_("card_type.eq.smartlink,card_type.is.null")
    elif filter_type and filter_type in ["smartlink", "crypto"]:
        # Upgraded users can filter
        if filter_type == "smartlink":
            query = query.or_("card_type.eq.smartlink,card_type.is.null")
        else:
            query = query.eq("card_type", "crypto")
    
    res = query.limit(500).execute()
    cards = res.data or []
    random.shuffle(cards)
    
    return [_card_public(c) for c in cards[:12]]

@api_router.get("/cards/mine")
def get_my_cards(user: dict = Depends(get_current_user)):
    res = sb.table("cards").select("*").eq("owner_id", user["user_id"]).order("created_at", desc=True).limit(500).execute()
    return [_card_public(c) for c in (res.data or [])]

@api_router.post("/cards/{card_id}/vote")
def vote_card(card_id: str, user: dict = Depends(get_current_user)):
    card = _maybe(sb.table("cards").select("*").eq("card_id", card_id).maybe_single().execute())
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card["owner_id"] == user["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot vote on your own card")
    
    expires_dt = _parse_dt(card["expires_at"])
    if expires_dt < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Card has expired")

    if card.get("card_type") == "crypto":
        raise HTTPException(status_code=400, detail="Use crypto-vote for crypto cards")

    sb.table("votes").insert({
        "vote_id": f"vote_{uuid.uuid4().hex[:12]}",
        "voter_id": user["user_id"],
        "card_id": card_id,
        "owner_id": card["owner_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    sb.table("cards").update({"votes": card.get("votes", 0) + 1}).eq("card_id", card_id).execute()

    new_progress = user.get("votes_since_token", 0) + 1
    tokens_earned = 0
    if new_progress >= VOTES_PER_TOKEN:
        tokens_earned = new_progress // VOTES_PER_TOKEN
        new_progress = new_progress % VOTES_PER_TOKEN
    
    new_tokens = user.get("ad_tokens", 0) + tokens_earned
    sb.table("users").update({
        "votes_since_token": new_progress,
        "ad_tokens": new_tokens
    }).eq("user_id", user["user_id"]).execute()

    return {
        "ok": True,
        "smart_link": card["smart_link"],
        "ad_tokens": new_tokens,
        "votes_since_token": new_progress,
        "tokens_earned": tokens_earned,
    }

@api_router.post("/cards/{card_id}/crypto-vote")
def crypto_vote_card(card_id: str, payload: CryptoVoteRequest, user: dict = Depends(get_current_user)):
    card = _maybe(sb.table("cards").select("*").eq("card_id", card_id).maybe_single().execute())
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card["owner_id"] == user["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot vote on your own card")
    
    if card.get("card_type") != "crypto":
        raise HTTPException(status_code=400, detail="Use /vote for SmartLink cards")
    
    expires_dt = _parse_dt(card["expires_at"])
    if expires_dt < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Card has expired")

    owner = _maybe(sb.table("users").select("*").eq("user_id", card["owner_id"]).maybe_single().execute())
    if not owner or not owner.get("service_fee_paid"):
        raise HTTPException(status_code=400, detail="Card owner's service fee is not current")
    
    existing = _maybe(sb.table("sol_transactions").select("tx_id").eq("tx_hash", payload.tx_hash).maybe_single().execute())
    if existing:
        raise HTTPException(status_code=400, detail="Transaction already used")

    vote_cost = card.get("vote_cost_sol", DEFAULT_VOTE_COST_SOL)
    now = datetime.now(timezone.utc)
    
    sb.table("sol_transactions").insert({
        "tx_id": f"cv_{uuid.uuid4().hex[:12]}",
        "from_user_id": user["user_id"],
        "to_user_id": card["owner_id"],
        "tx_type": "vote_reward",
        "amount_sol": vote_cost,
        "tx_hash": payload.tx_hash,
        "status": "confirmed",
        "confirmed_at": now.isoformat()
    }).execute()
    
    new_votes = card.get("votes", 0) + 1
    sb.table("cards").update({"votes": new_votes}).eq("card_id", card_id).execute()
    
    owner_sol = owner.get("sol_balance", 0) if owner else 0
    sb.table("users").update({
        "sol_balance": float(owner_sol) + vote_cost
    }).eq("user_id", card["owner_id"]).execute()

    return {"ok": True, "votes": new_votes, "amount_sol": vote_cost}

# ========= Referral =========
@api_router.get("/referral/me")
def referral_me(user: dict = Depends(get_current_user)):
    return {
        "referral_code": user.get("referral_code"),
        "diamonds": user.get("diamonds", 0),
        "diamond_boost_cost": DIAMOND_BOOST_COST,
        "diamond_boost_minutes": DIAMOND_BOOST_MINUTES,
    }

# ========= Image Library =========
@api_router.get("/images/library")
def image_library(user: dict = Depends(get_current_user)):
    return {"images": SYSTEM_IMAGES}

# ========= PayFast =========
def _payfast_signature(params: dict, passphrase: str = "") -> str:
    filtered = {k: v for k, v in params.items() if v not in (None, "")}
    pairs = []
    for k in sorted(filtered.keys()):
        v = str(filtered[k]).strip()
        pairs.append(f"{k}={quote(v, safe='')}")
    query = "&".join(pairs)
    if passphrase:
        query += f"&passphrase={quote(passphrase, safe='')}"
    return hashlib.md5(query.encode("utf-8")).hexdigest()

@api_router.post("/payments/payfast/initiate")
def payfast_initiate(payload: PayfastInitiatePayload, user: dict = Depends(get_current_user)):
    merchant_id = os.environ.get("PAYFAST_MERCHANT_ID", "")
    merchant_key = os.environ.get("PAYFAST_MERCHANT_KEY", "")
    passphrase = os.environ.get("PAYFAST_PASSPHRASE", "")
    sandbox = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"

    m_payment_id = f"stokvel_{user['user_id']}_{uuid.uuid4().hex[:8]}"
    params = {
        "merchant_id": merchant_id,
        "merchant_key": merchant_key,
        "return_url": payload.return_url,
        "cancel_url": payload.cancel_url,
        "m_payment_id": m_payment_id,
        "amount": "5.00",
        "item_name": "Stokvel Premium (Monthly)",
        "email_address": user["email"],
        "subscription_type": "1",
        "billing_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "recurring_amount": "5.00",
        "frequency": "3",
        "cycles": "0",
    }
    signature = _payfast_signature(params, passphrase)
    params["signature"] = signature

    sb.table("subscriptions").insert({
        "m_payment_id": m_payment_id,
        "user_id": user["user_id"],
        "kind": "subscription",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    base = "https://sandbox.payfast.co.za/eng/process" if sandbox else "https://www.payfast.co.za/eng/process"
    query = "&".join([f"{k}={quote(str(v), safe='')}" for k, v in params.items()])
    return {"redirect_url": f"{base}?{query}", "m_payment_id": m_payment_id}

@api_router.post("/payments/payfast/activate-sandbox")
def payfast_activate_sandbox(user: dict = Depends(get_current_user)):
    sandbox = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"
    if not sandbox:
        raise HTTPException(status_code=400, detail="Only available in sandbox mode")
    premium_until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    sb.table("users").update({"is_premium": True, "premium_until": premium_until}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "is_premium": True, "premium_until": premium_until}

@api_router.post("/payments/payfast/boost/initiate")
def payfast_boost_initiate(payload: PayfastInitiatePayload, user: dict = Depends(get_current_user)):
    merchant_id = os.environ.get("PAYFAST_MERCHANT_ID", "")
    merchant_key = os.environ.get("PAYFAST_MERCHANT_KEY", "")
    passphrase = os.environ.get("PAYFAST_PASSPHRASE", "")
    sandbox = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"

    m_payment_id = f"boost_{user['user_id']}_{uuid.uuid4().hex[:8]}"
    params = {
        "merchant_id": merchant_id,
        "merchant_key": merchant_key,
        "return_url": payload.return_url,
        "cancel_url": payload.cancel_url,
        "m_payment_id": m_payment_id,
        "amount": "2.50",
        "item_name": "Stokvel Boost Pack (3 tokens)",
        "email_address": user["email"],
    }
    signature = _payfast_signature(params, passphrase)
    params["signature"] = signature

    sb.table("subscriptions").insert({
        "m_payment_id": m_payment_id,
        "user_id": user["user_id"],
        "kind": "boost",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    base = "https://sandbox.payfast.co.za/eng/process" if sandbox else "https://www.payfast.co.za/eng/process"
    query = "&".join([f"{k}={quote(str(v), safe='')}" for k, v in params.items()])
    return {"redirect_url": f"{base}?{query}", "m_payment_id": m_payment_id}

@api_router.post("/payments/payfast/boost/activate-sandbox")
def payfast_boost_activate_sandbox(user: dict = Depends(get_current_user)):
    sandbox = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"
    if not sandbox:
        raise HTTPException(status_code=400, detail="Only available in sandbox mode")
    new_tokens = user.get("ad_tokens", 0) + 3
    sb.table("users").update({"ad_tokens": new_tokens}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "tokens": new_tokens, "credited": 3}

# ========= App wiring =========
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://haven-83b20.web.app",
        "https://haven-83b20.firebaseapp.com",
        "https://stokvel-cafbf.web.app",
        "https://stokvel-cafbf.firebaseapp.com",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
        "capacitor://localhost",
        "http://localhost"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)