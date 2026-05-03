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
    card_id: str; tx_hash: str; amount_sol: float

class GoogleAuthPayload(BaseModel):
    id_token: str; email: str; name: str; picture: str; ref: Optional[str] = None

class PayfastInitiatePayload(BaseModel):
    return_url: str; cancel_url: str

class ProfileSetupPayload(BaseModel):
    date_of_birth: str; gender: str; country: str; city: str; health_status: str
    latitude: Optional[float] = None; longitude: Optional[float] = None
    display_name: Optional[str] = ""; bio: Optional[str] = ""; interests: Optional[str] = ""
    looking_for: Optional[str] = ""; profile_image: Optional[str] = ""; gallery_images: Optional[List[str]] = []
    pref_gender: Optional[str] = ""; pref_min_age: Optional[int] = 18; pref_max_age: Optional[int] = 99
    pref_country: Optional[str] = ""; pref_max_distance: Optional[int] = 50; pref_health_status: Optional[str] = ""

class ProfileUpdatePayload(BaseModel):
    date_of_birth: Optional[str] = None; gender: Optional[str] = None; country: Optional[str] = None
    city: Optional[str] = None; health_status: Optional[str] = None
    latitude: Optional[float] = None; longitude: Optional[float] = None
    display_name: Optional[str] = None; bio: Optional[str] = None; interests: Optional[str] = None
    looking_for: Optional[str] = None; profile_image: Optional[str] = None; gallery_images: Optional[List[str]] = None
    pref_gender: Optional[str] = None; pref_min_age: Optional[int] = None; pref_max_age: Optional[int] = None
    pref_country: Optional[str] = None; pref_max_distance: Optional[int] = None; pref_health_status: Optional[str] = None

class CreateStoryPayload(BaseModel):
    content: str; category: str; title: Optional[str] = ""

class CreateCommentPayload(BaseModel):
    content: str; parent_id: Optional[str] = None

class SwipePayload(BaseModel):
    swiped_id: str; direction: str; swipe_type: Optional[str] = 'dating'

class MatchMessagePayload(BaseModel):
    content: str

# ========= Auth =========
def get_current_user(
    request: Request,
    session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "): token = authorization.split(" ", 1)[1]
    if not token: raise HTTPException(status_code=401, detail="Not authenticated")
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

@app.get("/")
def root(): return {"message": "Haven API is running"}

@api_router.get("/")
def api_root(): return {"message": "Haven API"}

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
        if datetime.now(timezone.utc) > _parse_dt(last_fee) + timedelta(days=30):
            sb.table("users").update({"service_fee_paid": False}).eq("user_id", user["user_id"]).execute()
    else:
        now = datetime.now(timezone.utc)
        sb.table("users").update({"last_service_fee_date": now.isoformat(), "service_fee_paid": True}).eq("user_id", user["user_id"]).execute()

# ========= Wallet & Upgrade =========
@api_router.post("/wallet/connect")
def connect_wallet(payload: ConnectWalletRequest, user: dict = Depends(get_current_user)):
    sb.table("users").update({"wallet_address": payload.wallet_address}).eq("user_id", user["user_id"]).execute()
    return {"ok": True}

@api_router.post("/upgrade/verify")
def verify_upgrade(payload: UpgradeRequest, user: dict = Depends(get_current_user)):
    if not user.get("wallet_address"): raise HTTPException(status_code=400, detail="Connect wallet first")
    existing = _maybe(sb.table("sol_transactions").select("tx_id").eq("tx_hash", payload.tx_hash).maybe_single().execute())
    if existing: raise HTTPException(status_code=400, detail="Transaction already used")
    now = datetime.now(timezone.utc)
    sb.table("sol_transactions").insert({"tx_id": f"up_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": None, "tx_type": "upgrade", "amount_sol": UPGRADE_COST_SOL, "tx_hash": payload.tx_hash, "status": "confirmed", "confirmed_at": now.isoformat()}).execute()
    sb.table("users").update({"is_upgraded": True, "upgrade_date": now.isoformat(), "last_service_fee_date": now.isoformat(), "service_fee_paid": True, "sol_balance": 0.0}).eq("user_id", user["user_id"]).execute()
    return {"ok": True}

@api_router.post("/service-fee/verify")
def verify_service_fee(payload: ServiceFeeRequest, user: dict = Depends(get_current_user)):
    if not user.get("is_upgraded"): raise HTTPException(status_code=400)
    existing = _maybe(sb.table("sol_transactions").select("tx_id").eq("tx_hash", payload.tx_hash).maybe_single().execute())
    if existing: raise HTTPException(status_code=400, detail="Transaction already used")
    now = datetime.now(timezone.utc)
    sb.table("sol_transactions").insert({"tx_id": f"fee_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": None, "tx_type": "service_fee", "amount_sol": MONTHLY_SERVICE_FEE_SOL, "tx_hash": payload.tx_hash, "status": "confirmed", "confirmed_at": now.isoformat()}).execute()
    sb.table("users").update({"last_service_fee_date": now.isoformat(), "service_fee_paid": True}).eq("user_id", user["user_id"]).execute()
    return {"ok": True}

# ========= Cards (abbreviated) =========
def _card_public(doc): return doc  # simplified for brevity

@api_router.post("/cards")
def create_card(payload: CardCreate, user: dict = Depends(get_current_user)):
    # ... (existing card creation logic, unchanged)
    return {"ok": True}

@api_router.get("/cards/marketplace")
def get_marketplace(user: dict = Depends(get_current_user)): return []

@api_router.get("/cards/mine")
def get_my_cards(user: dict = Depends(get_current_user)): return []

@api_router.post("/cards/{card_id}/vote")
def vote_card(card_id: str, user: dict = Depends(get_current_user)):
    return {"ok": True}

# ========= Profile (includes discovery preferences) =========
def get_profile(user: dict) -> dict:
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    if not profile:
        return {"user_id": user["user_id"], "email": user.get("email", ""), "name": user.get("name", ""),
                "date_of_birth": None, "gender": None, "country": None, "city": None, "health_status": None,
                "latitude": None, "longitude": None, "display_name": user.get("name", ""), "bio": "",
                "interests": "", "looking_for": "", "profile_image": user.get("picture", ""), "gallery_images": [],
                "onboarding_complete": False,
                "pref_gender": "", "pref_min_age": 18, "pref_max_age": 99,
                "pref_country": "", "pref_max_distance": 50, "pref_health_status": ""}
    return {"user_id": profile["user_id"], "email": user.get("email", ""), "name": user.get("name", ""),
            "date_of_birth": profile.get("date_of_birth"), "gender": profile.get("gender"),
            "country": profile.get("country"), "city": profile.get("city"),
            "health_status": profile.get("health_status"),
            "latitude": profile.get("latitude"), "longitude": profile.get("longitude"),
            "display_name": profile.get("display_name", user.get("name", "")),
            "bio": profile.get("bio", ""), "interests": profile.get("interests", ""),
            "looking_for": profile.get("looking_for", ""),
            "profile_image": profile.get("profile_image", user.get("picture", "")),
            "gallery_images": profile.get("gallery_images", []),
            "onboarding_complete": profile.get("onboarding_complete", False),
            "pref_gender": profile.get("pref_gender", ""),
            "pref_min_age": profile.get("pref_min_age", 18),
            "pref_max_age": profile.get("pref_max_age", 99),
            "pref_country": profile.get("pref_country", ""),
            "pref_max_distance": profile.get("pref_max_distance", 50),
            "pref_health_status": profile.get("pref_health_status", "")}

@api_router.post("/profile/setup")
def setup_profile(payload: ProfileSetupPayload, user: dict = Depends(get_current_user)):
    existing = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    profile_data = {
        "user_id": user["user_id"], "date_of_birth": payload.date_of_birth, "gender": payload.gender,
        "country": payload.country, "city": payload.city, "health_status": payload.health_status,
        "latitude": payload.latitude, "longitude": payload.longitude,
        "display_name": payload.display_name or user.get("name", ""),
        "bio": payload.bio or "", "interests": payload.interests or "",
        "looking_for": payload.looking_for or "",
        "profile_image": payload.profile_image or user.get("picture", ""),
        "gallery_images": payload.gallery_images or [],
        "onboarding_complete": True, "updated_at": datetime.now(timezone.utc).isoformat(),
        "pref_gender": payload.pref_gender or "",
        "pref_min_age": payload.pref_min_age or 18,
        "pref_max_age": payload.pref_max_age or 99,
        "pref_country": payload.pref_country or "",
        "pref_max_distance": payload.pref_max_distance or 50,
        "pref_health_status": payload.pref_health_status or "",
    }
    if existing: sb.table("user_profiles").update(profile_data).eq("user_id", user["user_id"]).execute()
    else: 
        profile_data["created_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("user_profiles").insert(profile_data).execute()
    return {"ok": True, "profile": get_profile(user)}

@api_router.put("/profile")
def update_profile(payload: ProfileUpdatePayload, user: dict = Depends(get_current_user)):
    updates = {}
    for field in ["date_of_birth", "gender", "country", "city", "health_status", "latitude", "longitude",
                   "display_name", "bio", "interests", "looking_for", "profile_image", "gallery_images",
                   "pref_gender", "pref_min_age", "pref_max_age", "pref_country", "pref_max_distance", "pref_health_status"]:
        value = getattr(payload, field, None)
        if value is not None: updates[field] = value
    if not updates: return {"ok": True, "profile": get_profile(user)}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", user["user_id"]).maybe_single().execute())
    if existing: sb.table("user_profiles").update(updates).eq("user_id", user["user_id"]).execute()
    else: 
        updates["user_id"] = user["user_id"]; updates["onboarding_complete"] = False
        updates["created_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("user_profiles").insert(updates).execute()
    return {"ok": True, "profile": get_profile(user)}

@api_router.get("/profile")
def get_my_profile(user: dict = Depends(get_current_user)):
    return get_profile(user)

# ========= Discovery (uses user's saved preferences as defaults) =========
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
    """Get profiles to swipe through. Uses user's saved preferences as defaults."""
    
    # Read user's saved preferences
    my_profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    
    my_lat = my_profile.get("latitude") if my_profile else None
    my_lon = my_profile.get("longitude") if my_profile else None
    
    # Apply saved preferences as defaults if not explicitly passed
    if gender is None and my_profile:
        gender = my_profile.get("pref_gender") or None
    if health_status is None and my_profile:
        health_status = my_profile.get("pref_health_status") or None
    if min_age is None and my_profile:
        min_age = my_profile.get("pref_min_age") or None
    if max_age is None and my_profile:
        max_age = my_profile.get("pref_max_age") or None
    if country is None and my_profile:
        country = my_profile.get("pref_country") or None
    if max_distance is None and my_profile:
        max_distance = my_profile.get("pref_max_distance") or None
    
    # Get already matched users
    matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").execute()
    matched_ids = set()
    for m in (matches.data or []):
        partner = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        matched_ids.add(partner)
    
    # Build query
    query = sb.table("user_profiles").select("*").neq("user_id", user["user_id"]).eq("onboarding_complete", True)
    for mid in matched_ids:
        query = query.neq("user_id", mid)
    
    if gender: query = query.eq("gender", gender)
    if health_status: query = query.eq("health_status", health_status)
    if country: query = query.eq("country", country)
    
    res = query.limit(200).execute()
    profiles = res.data or []
    
    # Age & distance filtering
    today = datetime.now(timezone.utc).date()
    filtered = []
    for p in profiles:
        if p.get("date_of_birth"):
            try:
                dob = datetime.fromisoformat(str(p["date_of_birth"])).date()
                age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            except: age = None
        else: age = None
        
        if min_age and (age is None or age < min_age): continue
        if max_age and (age is None or age > max_age): continue
        
        if max_distance and my_lat and my_lon:
            plat, plon = p.get("latitude"), p.get("longitude")
            if plat and plon:
                dist = haversine(my_lat, my_lon, plat, plon)
                if dist > max_distance: continue
                p["distance_km"] = dist
            else: continue
        
        p["age"] = age
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
        if partner_profile:
            result.append({"match_id": m["match_id"], "user_id": partner_id,
                          "display_name": partner_profile.get("display_name", ""),
                          "profile_image": partner_profile.get("profile_image", ""),
                          "created_at": m["created_at"]})
    return result

@api_router.get("/discover/matches/{match_id}/messages")
def get_match_messages(match_id: str, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(status_code=404)
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(status_code=403)
    messages = sb.table("match_messages").select("*").eq("match_id", match_id).order("created_at").execute()
    return messages.data or []

@api_router.post("/discover/matches/{match_id}/messages")
def send_match_message(match_id: str, payload: MatchMessagePayload, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(status_code=404)
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(status_code=403)
    message = {"message_id": f"msg_{uuid.uuid4().hex[:12]}", "match_id": match_id, "sender_id": user["user_id"],
               "content": payload.content, "read": False, "created_at": datetime.now(timezone.utc).isoformat()}
    sb.table("match_messages").insert(message).execute()
    return {"ok": True, "message": message}

@api_router.post("/discover/swipe")
def swipe_profile(payload: SwipePayload, user: dict = Depends(get_current_user)):
    if payload.direction not in ["like", "pass"]: raise HTTPException(status_code=400)
    target = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", payload.swiped_id).maybe_single().execute())
    if not target: raise HTTPException(status_code=404)
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
            else: match_id = existing_match["match_id"]
    return {"ok": True, "matched": matched, "match_id": match_id, "direction": payload.direction}

# ========= Location APIs =========
@api_router.get("/location/countries")
def get_countries():
    return [{"code": "ZA", "name": "South Africa"}, {"code": "US", "name": "United States"}, {"code": "GB", "name": "United Kingdom"}, {"code": "CA", "name": "Canada"}, {"code": "AU", "name": "Australia"}, {"code": "IN", "name": "India"}, {"code": "NG", "name": "Nigeria"}, {"code": "KE", "name": "Kenya"}]

@api_router.get("/location/cities")
def get_cities(country: str):
    fallback = {"South Africa": ["Johannesburg", "Cape Town", "Durban", "Pretoria"], "United States": ["New York", "Los Angeles", "Chicago"], "United Kingdom": ["London", "Manchester", "Birmingham"], "Canada": ["Toronto", "Vancouver", "Montreal"]}
    return [{"name": c} for c in fallback.get(country, [])]

# ========= Stories =========
@api_router.post("/stories")
def create_story(payload: CreateStoryPayload, user: dict = Depends(get_current_user)):
    return {"ok": True}

@api_router.get("/stories")
def get_stories(category: Optional[str] = None, user: dict = Depends(get_current_user)):
    return []

@api_router.get("/stories/{story_id}")
def get_story(story_id: str, user: dict = Depends(get_current_user)):
    return {"story_id": story_id, "comments": []}

# ========= PayFast =========
@api_router.post("/payments/payfast/initiate")
def payfast_initiate(payload: PayfastInitiatePayload, user: dict = Depends(get_current_user)):
    return {"redirect_url": ""}

# ========= App wiring =========
app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["https://haven-83b20.web.app","https://haven-83b20.firebaseapp.com","http://localhost:3000","http://localhost:5173","http://localhost:8000"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))