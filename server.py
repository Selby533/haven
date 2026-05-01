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
import math
import httpx
from urllib.parse import quote
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, List
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
UPGRADE_COST_SOL = 0.012
MONTHLY_SERVICE_FEE_SOL = 0.01
DEFAULT_VOTE_COST_SOL = 0.001
EARTH_RADIUS_KM = 6371

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
    if value is None: return None
    if isinstance(value, datetime): dt = value
    else:
        s = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt

def _maybe(res):
    if res is None: return None
    if hasattr(res, 'error') and res.error:
        logger.error(f"Supabase error: {res.error}")
        return None
    return getattr(res, "data", None)

def haversine(lat1, lon1, lat2, lon2):
    """Calculate distance between two coordinates in km"""
    if None in [lat1, lon1, lat2, lon2]: return None
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return round(EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

# ========= Models =========
class CardCreate(BaseModel):
    image_url: str
    smart_link: Optional[str] = ""
    title: Optional[str] = ""
    use_diamond_boost: Optional[bool] = False
    card_type: Optional[str] = "smartlink"
    vote_cost_sol: Optional[float] = DEFAULT_VOTE_COST_SOL

class ConnectWalletRequest(BaseModel): wallet_address: str
class UpgradeRequest(BaseModel): tx_hash: str
class ServiceFeeRequest(BaseModel): tx_hash: str

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

class ProfileSetupPayload(BaseModel):
    date_of_birth: str
    gender: str
    country: str
    city: str
    health_status: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    display_name: Optional[str] = ""
    bio: Optional[str] = ""
    interests: Optional[str] = ""
    looking_for: Optional[str] = ""
    profile_image: Optional[str] = ""
    gallery_images: Optional[List[str]] = []

class ProfileUpdatePayload(BaseModel):
    date_of_birth: Optional[str] = None
    gender: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    health_status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    interests: Optional[str] = None
    looking_for: Optional[str] = None
    profile_image: Optional[str] = None
    gallery_images: Optional[List[str]] = None

class CreateStoryPayload(BaseModel):
    content: str
    category: str
    title: Optional[str] = ""

class CreateCommentPayload(BaseModel):
    content: str
    parent_id: Optional[str] = None

class SwipePayload(BaseModel):
    swiped_id: str
    direction: str
    swipe_type: Optional[str] = 'dating'

class MatchMessagePayload(BaseModel):
    content: str

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
    if not session: raise HTTPException(status_code=401, detail="Invalid session")
    expires_at = _parse_dt(session["expires_at"])
    if expires_at < datetime.now(timezone.utc): raise HTTPException(status_code=401, detail="Session expired")
    user_res = sb.table("users").select("*").eq("user_id", session["user_id"]).maybe_single().execute()
    user = _maybe(user_res)
    if not user: raise HTTPException(status_code=401, detail="User not found")
    check_service_fee(user)
    return user

# ========= Root Routes =========
@app.get("/")
def root(): return {"message": "Stokvel API is running"}

@api_router.get("/")
def api_root(): return {"message": "Stokvel API"}

@api_router.post("/auth/google")
def auth_google(payload: GoogleAuthPayload, response: Response):
    email, name, picture, ref = payload.email, payload.name, payload.picture, payload.ref
    session_token = f"session_{uuid.uuid4().hex[:32]}"
    existing = _maybe(sb.table("users").select("*").eq("email", email).maybe_single().execute())
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing:
        user_id = existing["user_id"]
        updates = {"name": name, "picture": picture}
        if not existing.get("referral_code"): updates["referral_code"] = uuid.uuid4().hex[:8]
        sb.table("users").update(updates).eq("user_id", user_id).execute()
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        referral_code = uuid.uuid4().hex[:8]
        referred_by = None
        if ref:
            ref_user = _maybe(sb.table("users").select("*").eq("referral_code", ref).maybe_single().execute())
            if ref_user and ref_user["user_id"] != user_id:
                referred_by = ref_user["user_id"]
                sb.table("users").update({"diamonds": (ref_user.get("diamonds") or 0) + 1}).eq("user_id", ref_user["user_id"]).execute()
        sb.table("users").insert({
            "user_id": user_id, "email": email, "name": name, "picture": picture,
            "ad_tokens": INITIAL_AD_TOKENS, "sol_balance": 0.0, "is_upgraded": False,
            "is_premium": False, "wallet_address": None, "diamonds": 0,
            "premium_until": None, "upgrade_date": None, "last_service_fee_date": None,
            "votes_since_token": 0, "referral_code": referral_code, "referred_by": referred_by,
            "service_fee_paid": False, "created_at": now_iso,
        }).execute()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    sb.table("user_sessions").upsert({"session_token": session_token, "user_id": user_id, "expires_at": expires_at.isoformat(), "created_at": now_iso}).execute()
    response.set_cookie(key="session_token", value=session_token, httponly=True, secure=False, samesite="lax", path="/", max_age=7*24*60*60)
    return {"ok": True, "user_id": user_id, "token": session_token}

@api_router.get("/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    check_service_fee(user)
    profile = _maybe(sb.table("user_profiles").select("onboarding_complete").eq("user_id", user["user_id"]).maybe_single().execute())
    onboarding_complete = profile.get("onboarding_complete", False) if profile else False
    return {
        "user_id": user["user_id"], "email": user["email"], "name": user["name"],
        "picture": user.get("picture", ""), "ad_tokens": user.get("ad_tokens", 0),
        "sol_balance": user.get("sol_balance", 0), "is_upgraded": user.get("is_upgraded", False),
        "is_premium": user.get("is_premium", False), "wallet_address": user.get("wallet_address"),
        "diamonds": user.get("diamonds", 0), "premium_until": user.get("premium_until"),
        "votes_since_token": user.get("votes_since_token", 0), "votes_per_token": VOTES_PER_TOKEN,
        "referral_code": user.get("referral_code"),
        "diamond_boost_cost": DIAMOND_BOOST_COST, "diamond_boost_minutes": DIAMOND_BOOST_MINUTES,
        "upgrade_cost_sol": UPGRADE_COST_SOL, "monthly_service_fee_sol": MONTHLY_SERVICE_FEE_SOL,
        "vote_cost_sol": DEFAULT_VOTE_COST_SOL, "service_fee_paid": user.get("service_fee_paid", False),
        "upgrade_date": user.get("upgrade_date"), "last_service_fee_date": user.get("last_service_fee_date"),
        "onboarding_complete": onboarding_complete,
    }

@api_router.post("/auth/logout")
def auth_logout(response: Response, session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"), authorization: Optional[str] = Header(default=None)):
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "): token = authorization.split(" ", 1)[1]
    if token: sb.table("user_sessions").delete().eq("session_token", token).execute()
    response.delete_cookie(key="session_token", path="/", samesite="lax", secure=False)
    return {"ok": True}

def check_service_fee(user: dict):
    if not user.get("is_upgraded"): return
    last_fee = user.get("last_service_fee_date")
    if last_fee:
        last_fee_dt = _parse_dt(last_fee)
        if datetime.now(timezone.utc) > last_fee_dt + timedelta(days=30):
            sb.table("users").update({"service_fee_paid": False}).eq("user_id", user["user_id"]).execute()
            user["service_fee_paid"] = False
    else:
        now = datetime.now(timezone.utc)
        sb.table("users").update({"last_service_fee_date": now.isoformat(), "service_fee_paid": True}).eq("user_id", user["user_id"]).execute()
        user["last_service_fee_date"] = now.isoformat()
        user["service_fee_paid"] = True

# ========= Wallet & Upgrade =========
@api_router.post("/wallet/connect")
def connect_wallet(payload: ConnectWalletRequest, user: dict = Depends(get_current_user)):
    sb.table("users").update({"wallet_address": payload.wallet_address}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "wallet_address": payload.wallet_address}

@api_router.post("/upgrade/verify")
def verify_upgrade(payload: UpgradeRequest, user: dict = Depends(get_current_user)):
    if not user.get("wallet_address"): raise HTTPException(status_code=400, detail="Connect wallet first")
    existing = _maybe(sb.table("sol_transactions").select("tx_id").eq("tx_hash", payload.tx_hash).maybe_single().execute())
    if existing: raise HTTPException(status_code=400, detail="Transaction already used")
    now = datetime.now(timezone.utc)
    sb.table("sol_transactions").insert({"tx_id": f"up_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": None, "tx_type": "upgrade", "amount_sol": UPGRADE_COST_SOL, "tx_hash": payload.tx_hash, "status": "confirmed", "confirmed_at": now.isoformat()}).execute()
    sb.table("users").update({"is_upgraded": True, "upgrade_date": now.isoformat(), "last_service_fee_date": now.isoformat(), "service_fee_paid": True, "sol_balance": 0.0}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "is_upgraded": True}

@api_router.post("/service-fee/verify")
def verify_service_fee(payload: ServiceFeeRequest, user: dict = Depends(get_current_user)):
    if not user.get("is_upgraded"): raise HTTPException(status_code=400, detail="Not an upgraded user")
    existing = _maybe(sb.table("sol_transactions").select("tx_id").eq("tx_hash", payload.tx_hash).maybe_single().execute())
    if existing: raise HTTPException(status_code=400, detail="Transaction already used")
    now = datetime.now(timezone.utc)
    sb.table("sol_transactions").insert({"tx_id": f"fee_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": None, "tx_type": "service_fee", "amount_sol": MONTHLY_SERVICE_FEE_SOL, "tx_hash": payload.tx_hash, "status": "confirmed", "confirmed_at": now.isoformat()}).execute()
    sb.table("users").update({"last_service_fee_date": now.isoformat(), "service_fee_paid": True}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "next_fee_due": (now + timedelta(days=30)).isoformat(), "service_fee_paid": True}

# ========= Cards =========
def _card_public(doc: dict) -> dict:
    return {"card_id": doc["card_id"], "owner_id": doc["owner_id"], "owner_name": doc.get("owner_name", ""), "image_url": doc["image_url"], "smart_link": doc.get("smart_link", ""), "title": doc.get("title", ""), "votes": doc.get("votes", 0), "created_at": doc["created_at"], "expires_at": doc["expires_at"], "is_premium": doc.get("is_premium", False), "diamond_boosted": doc.get("diamond_boosted", False), "card_type": doc.get("card_type", "smartlink"), "vote_cost_sol": doc.get("vote_cost_sol", DEFAULT_VOTE_COST_SOL), "owner_wallet": doc.get("owner_wallet")}

@api_router.post("/cards")
def create_card(payload: CardCreate, user: dict = Depends(get_current_user)):
    card_type = payload.card_type
    if card_type == "crypto":
        if not user.get("is_upgraded"): raise HTTPException(status_code=402, detail="Upgrade required")
        if not user.get("wallet_address"): raise HTTPException(status_code=400, detail="Connect wallet first")
        if not user.get("service_fee_paid"): raise HTTPException(status_code=402, detail="Service fee payment required")
        token_cost, smart_link, recipient_wallet = 0, "", user.get("wallet_address")
    else:
        if user.get("ad_tokens", 0) < 1: raise HTTPException(status_code=402, detail="Not enough ad tokens")
        token_cost = 1
        if not payload.smart_link or not payload.smart_link.startswith(("http://", "https://")): raise HTTPException(status_code=400, detail="smart_link must be a valid URL")
        smart_link, recipient_wallet = payload.smart_link, None
    if not payload.image_url: raise HTTPException(status_code=400, detail="image_url is required")
    base_ttl = PREMIUM_CARD_TTL_MINUTES if user.get("is_premium") else FREE_CARD_TTL_MINUTES
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=base_ttl)
    card = {"card_id": f"card_{uuid.uuid4().hex[:12]}", "owner_id": user["user_id"], "owner_name": user.get("name", ""), "image_url": payload.image_url, "smart_link": smart_link, "title": payload.title or "", "votes": 0, "created_at": now.isoformat(), "expires_at": expires.isoformat(), "is_premium": bool(user.get("is_premium", False)), "diamond_boosted": False, "card_type": card_type, "vote_cost_sol": payload.vote_cost_sol if card_type == "crypto" else 0.0, "owner_wallet": recipient_wallet}
    sb.table("cards").insert(card).execute()
    if token_cost > 0: sb.table("users").update({"ad_tokens": user["ad_tokens"] - 1}).eq("user_id", user["user_id"]).execute()
    if payload.use_diamond_boost: sb.table("users").update({"diamonds": user.get("diamonds", 0) - DIAMOND_BOOST_COST}).eq("user_id", user["user_id"]).execute()
    return _card_public(card)

@api_router.get("/cards/marketplace")
def get_marketplace(user: dict = Depends(get_current_user), filter_type: Optional[str] = None):
    now_iso = datetime.now(timezone.utc).isoformat()
    is_upgraded = user.get("is_upgraded", False)
    query = sb.table("cards").select("*").gt("expires_at", now_iso).neq("owner_id", user["user_id"])
    if not is_upgraded: query = query.or_("card_type.eq.smartlink,card_type.is.null")
    elif filter_type and filter_type in ["smartlink", "crypto"]:
        query = query.eq("card_type", filter_type)
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
    if not card: raise HTTPException(status_code=404, detail="Card not found")
    if card["owner_id"] == user["user_id"]: raise HTTPException(status_code=400, detail="Cannot vote on your own card")
    if _parse_dt(card["expires_at"]) < datetime.now(timezone.utc): raise HTTPException(status_code=400, detail="Card has expired")
    if card.get("card_type") == "crypto": raise HTTPException(status_code=400, detail="Use crypto-vote for crypto cards")
    sb.table("votes").insert({"vote_id": f"vote_{uuid.uuid4().hex[:12]}", "voter_id": user["user_id"], "card_id": card_id, "owner_id": card["owner_id"], "created_at": datetime.now(timezone.utc).isoformat()}).execute()
    sb.table("cards").update({"votes": card.get("votes", 0) + 1}).eq("card_id", card_id).execute()
    new_progress = user.get("votes_since_token", 0) + 1
    tokens_earned = 0
    if new_progress >= VOTES_PER_TOKEN:
        tokens_earned = new_progress // VOTES_PER_TOKEN
        new_progress = new_progress % VOTES_PER_TOKEN
    new_tokens = user.get("ad_tokens", 0) + tokens_earned
    sb.table("users").update({"votes_since_token": new_progress, "ad_tokens": new_tokens}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "smart_link": card["smart_link"], "ad_tokens": new_tokens, "votes_since_token": new_progress, "tokens_earned": tokens_earned}

@api_router.post("/cards/{card_id}/crypto-vote")
def crypto_vote_card(card_id: str, payload: CryptoVoteRequest, user: dict = Depends(get_current_user)):
    card = _maybe(sb.table("cards").select("*").eq("card_id", card_id).maybe_single().execute())
    if not card: raise HTTPException(status_code=404, detail="Card not found")
    if card["owner_id"] == user["user_id"]: raise HTTPException(status_code=400, detail="Cannot vote on your own card")
    if card.get("card_type") != "crypto": raise HTTPException(status_code=400, detail="Use /vote for SmartLink cards")
    if _parse_dt(card["expires_at"]) < datetime.now(timezone.utc): raise HTTPException(status_code=400, detail="Card has expired")
    owner = _maybe(sb.table("users").select("*").eq("user_id", card["owner_id"]).maybe_single().execute())
    if not owner or not owner.get("service_fee_paid"): raise HTTPException(status_code=400, detail="Card owner's service fee is not current")
    existing = _maybe(sb.table("sol_transactions").select("tx_id").eq("tx_hash", payload.tx_hash).maybe_single().execute())
    if existing: raise HTTPException(status_code=400, detail="Transaction already used")
    vote_cost = card.get("vote_cost_sol", DEFAULT_VOTE_COST_SOL)
    now = datetime.now(timezone.utc)
    sb.table("sol_transactions").insert({"tx_id": f"cv_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": card["owner_id"], "tx_type": "vote_reward", "amount_sol": vote_cost, "tx_hash": payload.tx_hash, "status": "confirmed", "confirmed_at": now.isoformat()}).execute()
    new_votes = card.get("votes", 0) + 1
    sb.table("cards").update({"votes": new_votes}).eq("card_id", card_id).execute()
    owner_sol = owner.get("sol_balance", 0) if owner else 0
    sb.table("users").update({"sol_balance": float(owner_sol) + vote_cost}).eq("user_id", card["owner_id"]).execute()
    return {"ok": True, "votes": new_votes, "amount_sol": vote_cost}

# ========= Referral =========
@api_router.get("/referral/me")
def referral_me(user: dict = Depends(get_current_user)):
    return {"referral_code": user.get("referral_code"), "diamonds": user.get("diamonds", 0), "diamond_boost_cost": DIAMOND_BOOST_COST, "diamond_boost_minutes": DIAMOND_BOOST_MINUTES}

@api_router.get("/images/library")
def image_library(user: dict = Depends(get_current_user)):
    return {"images": SYSTEM_IMAGES}

# ========= Profile =========
def get_profile(user: dict) -> dict:
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    if not profile:
        return {"user_id": user["user_id"], "email": user.get("email", ""), "name": user.get("name", ""), "date_of_birth": None, "gender": None, "country": None, "city": None, "health_status": None, "latitude": None, "longitude": None, "display_name": user.get("name", ""), "bio": "", "interests": "", "looking_for": "", "profile_image": user.get("picture", ""), "gallery_images": [], "onboarding_complete": False}
    return {"user_id": profile["user_id"], "email": user.get("email", ""), "name": user.get("name", ""), "date_of_birth": profile.get("date_of_birth"), "gender": profile.get("gender"), "country": profile.get("country"), "city": profile.get("city"), "health_status": profile.get("health_status"), "latitude": profile.get("latitude"), "longitude": profile.get("longitude"), "display_name": profile.get("display_name", user.get("name", "")), "bio": profile.get("bio", ""), "interests": profile.get("interests", ""), "looking_for": profile.get("looking_for", ""), "profile_image": profile.get("profile_image", user.get("picture", "")), "gallery_images": profile.get("gallery_images", []), "onboarding_complete": profile.get("onboarding_complete", False)}

@api_router.post("/profile/setup")
def setup_profile(payload: ProfileSetupPayload, user: dict = Depends(get_current_user)):
    existing = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    profile_data = {"user_id": user["user_id"], "date_of_birth": payload.date_of_birth, "gender": payload.gender, "country": payload.country, "city": payload.city, "health_status": payload.health_status, "latitude": payload.latitude, "longitude": payload.longitude, "display_name": payload.display_name or user.get("name", ""), "bio": payload.bio or "", "interests": payload.interests or "", "looking_for": payload.looking_for or "", "profile_image": payload.profile_image or user.get("picture", ""), "gallery_images": payload.gallery_images or [], "onboarding_complete": True, "updated_at": datetime.now(timezone.utc).isoformat()}
    if existing: sb.table("user_profiles").update(profile_data).eq("user_id", user["user_id"]).execute()
    else:
        profile_data["created_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("user_profiles").insert(profile_data).execute()
    return {"ok": True, "profile": get_profile(user)}

@api_router.put("/profile")
def update_profile(payload: ProfileUpdatePayload, user: dict = Depends(get_current_user)):
    updates = {}
    for field in ["date_of_birth", "gender", "country", "city", "health_status", "latitude", "longitude", "display_name", "bio", "interests", "looking_for", "profile_image", "gallery_images"]:
        value = getattr(payload, field, None)
        if value is not None: updates[field] = value
    if not updates: return {"ok": True, "profile": get_profile(user)}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", user["user_id"]).maybe_single().execute())
    if existing: sb.table("user_profiles").update(updates).eq("user_id", user["user_id"]).execute()
    else:
        updates["user_id"] = user["user_id"]; updates["onboarding_complete"] = False; updates["created_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("user_profiles").insert(updates).execute()
    return {"ok": True, "profile": get_profile(user)}

@api_router.get("/profile")
def get_my_profile(user: dict = Depends(get_current_user)):
    return get_profile(user)

# ========= Discovery =========
@api_router.get("/discover/profiles")
def get_discover_profiles(
    user: dict = Depends(get_current_user),
    gender: Optional[str] = None,
    health_status: Optional[str] = None,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    country: Optional[str] = None,
    max_distance: Optional[float] = None,
):
    """Get ALL profiles with optional filters, excluding already matched users"""
    
    # Get user's profile for distance calc
    my_profile = _maybe(sb.table("user_profiles").select("latitude,longitude").eq("user_id", user["user_id"]).maybe_single().execute())
    my_lat = my_profile.get("latitude") if my_profile else None
    my_lon = my_profile.get("longitude") if my_profile else None
    
    # Get IDs of already matched users (both dating and friendship)
    matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").execute()
    matched_ids = set()
    for m in (matches.data or []):
        partner = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        matched_ids.add(partner)
    
    # Build query - exclude current user and matched users
    query = sb.table("user_profiles").select("*").neq("user_id", user["user_id"]).eq("onboarding_complete", True)
    
    # Exclude matched users one by one
    for mid in matched_ids:
        query = query.neq("user_id", mid)
    
    # Apply filters
    if gender:
        query = query.eq("gender", gender)
    if health_status:
        query = query.eq("health_status", health_status)
    if country:
        query = query.eq("country", country)
    
    res = query.limit(200).execute()
    profiles = res.data or []
    
    # Calculate age from date_of_birth and filter
    today = datetime.now(timezone.utc).date()
    filtered = []
    for p in profiles:
        if p.get("date_of_birth"):
            try:
                dob = datetime.fromisoformat(str(p["date_of_birth"])).date()
                age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            except:
                age = None
        else:
            age = None
        
        # Age filter
        if min_age is not None and (age is None or age < min_age): continue
        if max_age is not None and (age is None or age > max_age): continue
        
        # Distance filter
        if max_distance is not None and my_lat and my_lon:
            plat = p.get("latitude")
            plon = p.get("longitude")
            if plat and plon:
                dist = haversine(my_lat, my_lon, plat, plon)
                if dist > max_distance: continue
                p["distance_km"] = dist
            elif max_distance:
                continue  # Exclude profiles without coordinates when distance filter applied
        
        p["age"] = age
        # Add email from users table
        user_data = _maybe(sb.table("users").select("email").eq("user_id", p["user_id"]).maybe_single().execute())
        p["email"] = user_data.get("email", "") if user_data else ""
        filtered.append(p)
    
    random.shuffle(filtered)
    return filtered[:50]

@api_router.get("/discover/matches")
def get_matches(swipe_type: Optional[str] = 'dating', user: dict = Depends(get_current_user)):
    matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").eq("swipe_type", swipe_type).order("created_at", desc=True).execute()
    result = []
    for m in (matches.data or []):
        partner_id = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        partner_profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", partner_id).maybe_single().execute())
        partner_user = _maybe(sb.table("users").select("email,picture").eq("user_id", partner_id).maybe_single().execute())
        last_msg = _maybe(sb.table("match_messages").select("*").eq("match_id", m["match_id"]).order("created_at", desc=True).limit(1).execute())
        last_message = last_msg[0] if last_msg else None
        if partner_profile:
            result.append({"match_id": m["match_id"], "user_id": partner_id, "display_name": partner_profile.get("display_name", ""), "profile_image": partner_profile.get("profile_image", partner_user.get("picture", "") if partner_user else ""), "bio": partner_profile.get("bio", ""), "country": partner_profile.get("country", ""), "city": partner_profile.get("city", ""), "health_status": partner_profile.get("health_status"), "last_message": last_message.get("content") if last_message else None, "last_message_time": last_message.get("created_at") if last_message else None, "created_at": m["created_at"]})
    return result

@api_router.get("/discover/matches/{match_id}/messages")
def get_match_messages(match_id: str, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(status_code=404, detail="Match not found")
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(status_code=403, detail="Not your match")
    messages = sb.table("match_messages").select("*").eq("match_id", match_id).order("created_at").execute()
    for msg in (messages.data or []):
        if msg["sender_id"] != user["user_id"] and not msg.get("read"):
            sb.table("match_messages").update({"read": True}).eq("message_id", msg["message_id"]).execute()
    return messages.data or []

@api_router.post("/discover/matches/{match_id}/messages")
def send_match_message(match_id: str, payload: MatchMessagePayload, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(status_code=404, detail="Match not found")
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(status_code=403, detail="Not your match")
    message = {"message_id": f"msg_{uuid.uuid4().hex[:12]}", "match_id": match_id, "sender_id": user["user_id"], "content": payload.content, "read": False, "created_at": datetime.now(timezone.utc).isoformat()}
    sb.table("match_messages").insert(message).execute()
    return {"ok": True, "message": message}

@api_router.post("/discover/swipe")
def swipe_profile(payload: SwipePayload, user: dict = Depends(get_current_user)):
    if payload.direction not in ["like", "pass"]: raise HTTPException(status_code=400, detail="Direction must be 'like' or 'pass'")
    target = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", payload.swiped_id).maybe_single().execute())
    if not target: raise HTTPException(status_code=404, detail="Profile not found")
    existing = _maybe(sb.table("profile_swipes").select("*").eq("swiper_id", user["user_id"]).eq("swiped_id", payload.swiped_id).eq("swipe_type", payload.swipe_type).maybe_single().execute())
    if not existing:
        sb.table("profile_swipes").insert({"swipe_id": f"swp_{uuid.uuid4().hex[:12]}", "swiper_id": user["user_id"], "swiped_id": payload.swiped_id, "direction": payload.direction, "swipe_type": payload.swipe_type, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
    matched = False; match_id = None
    if payload.direction == "like":
        other_swipe = _maybe(sb.table("profile_swipes").select("*").eq("swiper_id", payload.swiped_id).eq("swiped_id", user["user_id"]).eq("direction", "like").eq("swipe_type", payload.swipe_type).maybe_single().execute())
        if other_swipe:
            uid1, uid2 = sorted([user["user_id"], payload.swiped_id])
            existing_match = _maybe(sb.table("profile_matches").select("*").eq("user1_id", uid1).eq("user2_id", uid2).eq("swipe_type", payload.swipe_type).maybe_single().execute())
            if not existing_match:
                match_id = f"match_{uuid.uuid4().hex[:12]}"
                sb.table("profile_matches").insert({"match_id": match_id, "user1_id": uid1, "user2_id": uid2, "swipe_type": payload.swipe_type, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
                matched = True
            else:
                match_id = existing_match["match_id"]
    return {"ok": True, "matched": matched, "match_id": match_id, "direction": payload.direction}

# ========= Location API (Countries & Cities) =========
@api_router.get("/location/countries")
def get_countries():
    """Get list of all countries"""
    return [
        {"code": "AF", "name": "Afghanistan"}, {"code": "AL", "name": "Albania"}, {"code": "DZ", "name": "Algeria"},
        {"code": "AR", "name": "Argentina"}, {"code": "AU", "name": "Australia"}, {"code": "AT", "name": "Austria"},
        {"code": "BD", "name": "Bangladesh"}, {"code": "BE", "name": "Belgium"}, {"code": "BR", "name": "Brazil"},
        {"code": "CA", "name": "Canada"}, {"code": "CL", "name": "Chile"}, {"code": "CN", "name": "China"},
        {"code": "CO", "name": "Colombia"}, {"code": "EG", "name": "Egypt"}, {"code": "ET", "name": "Ethiopia"},
        {"code": "FR", "name": "France"}, {"code": "DE", "name": "Germany"}, {"code": "GH", "name": "Ghana"},
        {"code": "GR", "name": "Greece"}, {"code": "IN", "name": "India"}, {"code": "ID", "name": "Indonesia"},
        {"code": "IR", "name": "Iran"}, {"code": "IQ", "name": "Iraq"}, {"code": "IE", "name": "Ireland"},
        {"code": "IL", "name": "Israel"}, {"code": "IT", "name": "Italy"}, {"code": "JP", "name": "Japan"},
        {"code": "JO", "name": "Jordan"}, {"code": "KE", "name": "Kenya"}, {"code": "KW", "name": "Kuwait"},
        {"code": "LB", "name": "Lebanon"}, {"code": "LY", "name": "Libya"}, {"code": "MY", "name": "Malaysia"},
        {"code": "MX", "name": "Mexico"}, {"code": "MA", "name": "Morocco"}, {"code": "NL", "name": "Netherlands"},
        {"code": "NZ", "name": "New Zealand"}, {"code": "NG", "name": "Nigeria"}, {"code": "NO", "name": "Norway"},
        {"code": "PK", "name": "Pakistan"}, {"code": "PE", "name": "Peru"}, {"code": "PH", "name": "Philippines"},
        {"code": "PL", "name": "Poland"}, {"code": "PT", "name": "Portugal"}, {"code": "QA", "name": "Qatar"},
        {"code": "RO", "name": "Romania"}, {"code": "RU", "name": "Russia"}, {"code": "SA", "name": "Saudi Arabia"},
        {"code": "SG", "name": "Singapore"}, {"code": "ZA", "name": "South Africa"}, {"code": "KR", "name": "South Korea"},
        {"code": "ES", "name": "Spain"}, {"code": "SE", "name": "Sweden"}, {"code": "CH", "name": "Switzerland"},
        {"code": "TZ", "name": "Tanzania"}, {"code": "TH", "name": "Thailand"}, {"code": "TR", "name": "Turkey"},
        {"code": "UG", "name": "Uganda"}, {"code": "UA", "name": "Ukraine"}, {"code": "AE", "name": "United Arab Emirates"},
        {"code": "GB", "name": "United Kingdom"}, {"code": "US", "name": "United States"}, {"code": "VN", "name": "Vietnam"},
        {"code": "ZW", "name": "Zimbabwe"}
    ]

@api_router.get("/location/cities")
def get_cities(country: str):
    """Get cities for a specific country using API"""
    try:
        # Using countriesnow.space API for real city data
        resp = httpx.post("https://countriesnow.space/api/v0.1/countries/cities", json={"country": country}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("error"):
                cities = data.get("data", [])
                return [{"name": c} for c in cities]
    except:
        pass
    # Fallback for common countries
    fallback = {
        "South Africa": ["Johannesburg", "Cape Town", "Durban", "Pretoria", "Port Elizabeth", "Bloemfontein"],
        "United States": ["New York", "Los Angeles", "Chicago", "Houston", "Miami"],
        "United Kingdom": ["London", "Manchester", "Birmingham", "Liverpool", "Edinburgh"],
        "Canada": ["Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa"],
    }
    return [{"name": c} for c in fallback.get(country, [])]

# ========= Stories =========
@api_router.post("/stories")
def create_story(payload: CreateStoryPayload, user: dict = Depends(get_current_user)):
    if payload.category not in ["HIV", "HPV", "HSV"]: raise HTTPException(status_code=400)
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    story = {"story_id": f"story_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"], "author_name": (profile or {}).get("display_name", user.get("name", "")), "author_avatar": (profile or {}).get("profile_image", user.get("picture", "")), "content": payload.content, "category": payload.category, "title": payload.title or "", "likes": 0, "comment_count": 0, "created_at": datetime.now(timezone.utc).isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()}
    sb.table("stories").insert(story).execute()
    return {"ok": True, "story": story}

@api_router.get("/stories")
def get_stories(category: Optional[str] = None, user: dict = Depends(get_current_user)):
    query = sb.table("stories").select("*").order("created_at", desc=True).limit(100)
    if category and category in ["HIV", "HPV", "HSV"]: query = query.eq("category", category)
    stories = (query.execute()).data or []
    for s in stories:
        like = _maybe(sb.table("story_likes").select("like_id").eq("user_id", user["user_id"]).eq("story_id", s["story_id"]).maybe_single().execute())
        s["liked_by_user"] = like is not None
    return stories

@api_router.get("/stories/{story_id}")
def get_story(story_id: str, user: dict = Depends(get_current_user)):
    story = _maybe(sb.table("stories").select("*").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(status_code=404)
    like = _maybe(sb.table("story_likes").select("like_id").eq("user_id", user["user_id"]).eq("story_id", story_id).maybe_single().execute())
    story["liked_by_user"] = like is not None
    comments = sb.table("story_comments").select("*").eq("story_id", story_id).order("created_at").execute()
    story["comments"] = build_comment_tree(comments.data or [])
    return story

@api_router.post("/stories/{story_id}/like")
def like_story(story_id: str, user: dict = Depends(get_current_user)):
    existing = _maybe(sb.table("story_likes").select("*").eq("user_id", user["user_id"]).eq("story_id", story_id).maybe_single().execute())
    story = _maybe(sb.table("stories").select("likes").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(status_code=404)
    current = story.get("likes", 0)
    if existing:
        sb.table("story_likes").delete().eq("like_id", existing["like_id"]).execute()
        sb.table("stories").update({"likes": max(current - 1, 0)}).eq("story_id", story_id).execute()
        return {"ok": True, "liked": False}
    sb.table("story_likes").insert({"like_id": f"like_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"], "story_id": story_id}).execute()
    sb.table("stories").update({"likes": current + 1}).eq("story_id", story_id).execute()
    return {"ok": True, "liked": True}

@api_router.post("/stories/{story_id}/comments")
def create_comment(story_id: str, payload: CreateCommentPayload, user: dict = Depends(get_current_user)):
    story = _maybe(sb.table("stories").select("story_id,comment_count").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(status_code=404)
    if payload.parent_id:
        parent = _maybe(sb.table("story_comments").select("comment_id").eq("comment_id", payload.parent_id).maybe_single().execute())
        if not parent: raise HTTPException(status_code=404)
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    comment = {"comment_id": f"cmt_{uuid.uuid4().hex[:12]}", "story_id": story_id, "user_id": user["user_id"], "author_name": (profile or {}).get("display_name", user.get("name", "")), "author_avatar": (profile or {}).get("profile_image", user.get("picture", "")), "content": payload.content, "parent_id": payload.parent_id, "likes": 0, "reply_count": 0, "created_at": datetime.now(timezone.utc).isoformat()}
    sb.table("story_comments").insert(comment).execute()
    sb.table("stories").update({"comment_count": story.get("comment_count", 0) + 1}).eq("story_id", story_id).execute()
    if payload.parent_id:
        pc = _maybe(sb.table("story_comments").select("reply_count").eq("comment_id", payload.parent_id).maybe_single().execute())
        if pc: sb.table("story_comments").update({"reply_count": pc.get("reply_count", 0) + 1}).eq("comment_id", payload.parent_id).execute()
    return {"ok": True, "comment": comment}

def build_comment_tree(comments):
    cmap = {c["comment_id"]: {**c, "replies": []} for c in comments}
    roots = []
    for c in comments:
        node = cmap[c["comment_id"]]
        if c.get("parent_id") and c["parent_id"] in cmap: cmap[c["parent_id"]]["replies"].append(node)
        else: roots.append(node)
    return roots

# ========= PayFast =========
def _payfast_signature(params, passphrase=""):
    filtered = {k: v for k, v in params.items() if v not in (None, "")}
    pairs = [f"{k}={quote(str(filtered[k]).strip(), safe='')}" for k in sorted(filtered.keys())]
    query = "&".join(pairs)
    if passphrase: query += f"&passphrase={quote(passphrase, safe='')}"
    return hashlib.md5(query.encode("utf-8")).hexdigest()

@api_router.post("/payments/payfast/initiate")
def payfast_initiate(payload: PayfastInitiatePayload, user: dict = Depends(get_current_user)):
    mid, mkey, pp = os.environ.get("PAYFAST_MERCHANT_ID", ""), os.environ.get("PAYFAST_MERCHANT_KEY", ""), os.environ.get("PAYFAST_PASSPHRASE", "")
    sandbox = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"
    pid = f"stokvel_{user['user_id']}_{uuid.uuid4().hex[:8]}"
    params = {"merchant_id": mid, "merchant_key": mkey, "return_url": payload.return_url, "cancel_url": payload.cancel_url, "m_payment_id": pid, "amount": "5.00", "item_name": "Stokvel Premium", "email_address": user["email"], "subscription_type": "1", "billing_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "recurring_amount": "5.00", "frequency": "3", "cycles": "0"}
    params["signature"] = _payfast_signature(params, pp)
    sb.table("subscriptions").insert({"m_payment_id": pid, "user_id": user["user_id"], "kind": "subscription", "status": "pending"}).execute()
    base = "https://sandbox.payfast.co.za/eng/process" if sandbox else "https://www.payfast.co.za/eng/process"
    return {"redirect_url": f"{base}?{'&'.join([f'{k}={quote(str(v), safe='')}' for k,v in params.items()])}", "m_payment_id": pid}

@api_router.post("/payments/payfast/activate-sandbox")
def payfast_activate_sandbox(user: dict = Depends(get_current_user)):
    if os.environ.get("PAYFAST_SANDBOX", "true").lower() != "true": raise HTTPException(status_code=400)
    sb.table("users").update({"is_premium": True, "premium_until": (datetime.now(timezone.utc)+timedelta(days=30)).isoformat()}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "is_premium": True}

@api_router.post("/payments/payfast/boost/initiate")
def payfast_boost_initiate(payload: PayfastInitiatePayload, user: dict = Depends(get_current_user)):
    mid, mkey, pp = os.environ.get("PAYFAST_MERCHANT_ID", ""), os.environ.get("PAYFAST_MERCHANT_KEY", ""), os.environ.get("PAYFAST_PASSPHRASE", "")
    sandbox = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"
    pid = f"boost_{user['user_id']}_{uuid.uuid4().hex[:8]}"
    params = {"merchant_id": mid, "merchant_key": mkey, "return_url": payload.return_url, "cancel_url": payload.cancel_url, "m_payment_id": pid, "amount": "2.50", "item_name": "Stokvel Boost Pack", "email_address": user["email"]}
    params["signature"] = _payfast_signature(params, pp)
    sb.table("subscriptions").insert({"m_payment_id": pid, "user_id": user["user_id"], "kind": "boost", "status": "pending"}).execute()
    base = "https://sandbox.payfast.co.za/eng/process" if sandbox else "https://www.payfast.co.za/eng/process"
    return {"redirect_url": f"{base}?{'&'.join([f'{k}={quote(str(v), safe='')}' for k,v in params.items()])}", "m_payment_id": pid}

@api_router.post("/payments/payfast/boost/activate-sandbox")
def payfast_boost_activate_sandbox(user: dict = Depends(get_current_user)):
    if os.environ.get("PAYFAST_SANDBOX", "true").lower() != "true": raise HTTPException(status_code=400)
    sb.table("users").update({"ad_tokens": user.get("ad_tokens", 0) + 3}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "tokens": user.get("ad_tokens", 0) + 3}

# ========= App wiring =========
app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["https://haven-83b20.web.app","https://haven-83b20.firebaseapp.com","https://stokvel-cafbf.web.app","https://stokvel-cafbf.firebaseapp.com","http://localhost:3000","http://localhost:5173","http://localhost:8000","capacitor://localhost","http://localhost"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))