from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Cookie, Header
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import os, logging, uuid, random, math, httpx, io, base64, time
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta
from PIL import Image
import httpx as httpx_lib

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']

sb: Client = create_client(SUPABASE_URL.rstrip('/'), SUPABASE_KEY)
http_client = httpx_lib.Client(http2=False)
sb.postgrest.session = http_client

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://havenpositive.online","https://www.havenpositive.online","https://haven-83b20.web.app","https://haven-83b20.firebaseapp.com","https://haven-hmwq.onrender.com","http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter(prefix="/api")

# ---------- Simple in‑memory rate limiter ----------
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10     # requests per window
rate_limit_store: Dict[str, list] = {}

def check_rate_limit(key: str, max_req: int = RATE_LIMIT_MAX, window: int = RATE_LIMIT_WINDOW):
    now = time.time()
    timestamps = rate_limit_store.get(key, [])
    timestamps = [t for t in timestamps if now - t < window]
    if len(timestamps) >= max_req:
        raise HTTPException(429, "Too many requests. Please slow down.")
    timestamps.append(now)
    rate_limit_store[key] = timestamps

# ---------- Constants ----------
EARTH_RADIUS_KM = 6371
MAX_GPS_AGE_HOURS = 24
STORAGE_BUCKET = "avatars"
VERIFY_COST = 10
GOLD_COST = 39
GOLD_DAYS = 30
PREMIUM_COST = 199
PREMIUM_DAYS = 180
MAX_IMAGE_BASE64_SIZE = 5 * 1024 * 1024  # 5 MB

PROFANITY_LIST = {"fuck","shit","bitch","asshole","bastard","dick","pussy","cunt","whore"}
ETHNICITY_LIST = [
    "Asian", "Black / African", "Caucasian / White", "Hispanic / Latino",
    "Middle Eastern", "Native American", "Pacific Islander",
    "South Asian", "Southeast Asian", "Mixed / Other"
]

def contains_profanity(text: str) -> bool:
    if not text: return False
    words = text.lower().split()
    return any(word.strip(".,!?") in PROFANITY_LIST for word in words)

# ---------- Helpers ----------
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

def reverse_geocode(lat: float, lon: float) -> tuple:
    try:
        resp = httpx.get(f"https://nominatim.openstreetmap.org/reverse", params={"lat": lat, "lon": lon, "format": "json"}, headers={"User-Agent": "HavenApp/1.0"}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("address"):
                country = data["address"].get("country")
                city = data["address"].get("city") or data["address"].get("town") or data["address"].get("village")
                return country, city
    except Exception as e:
        logger.error(f"Reverse geocoding failed: {e}")
    return None, None

async def get_location_from_ip(ip: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://ip-api.com/json/{ip}?fields=status,lat,lon,country,city")
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    return {'latitude': data.get('lat'), 'longitude': data.get('lon'), 'country': data.get('country'), 'city': data.get('city'), 'source': 'ip'}
    except Exception as e:
        logger.error(f"IP geolocation failed: {e}")
    return None

# ---------- Image Helpers (WebP) ----------
def compress_image(base64_str: str, max_size_kb: int = 300) -> bytes:
    if len(base64_str) > MAX_IMAGE_BASE64_SIZE:
        raise HTTPException(400, "Image too large (max 5 MB)")
    if "," in base64_str: base64_str = base64_str.split(",", 1)[1]
    img_data = base64.b64decode(base64_str)
    img = Image.open(io.BytesIO(img_data))
    if img.mode in ("RGBA", "P"): img = img.convert("RGB")
    w, h = img.size
    if w > 1200 or h > 1200: img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    quality = 85
    while True:
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality)
        size_kb = buf.tell() / 1024
        if size_kb <= max_size_kb or quality <= 20: break
        quality -= 5
    return buf.getvalue()

def upload_image_to_supabase(file_bytes: bytes, user_id: str, filename: str) -> str:
    path = f"{user_id}/{filename}"
    sb.storage.from_(STORAGE_BUCKET).upload(path=path, file=file_bytes, file_options={"content-type": "image/webp", "cache-control": "public, max-age=31536000, immutable"})
    return f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{path}"

def process_image_field(image_value: str, user_id: str, filename_prefix: str) -> str:
    if not image_value: return image_value
    if image_value.startswith("data:image") or (len(image_value) > 1000 and "base64" in image_value):
        if len(image_value) > MAX_IMAGE_BASE64_SIZE:
            raise HTTPException(400, "Image too large (max 5 MB)")
        try:
            compressed = compress_image(image_value)
            filename = f"{filename_prefix}_{uuid.uuid4().hex[:8]}.jpg"
            return upload_image_to_supabase(compressed, user_id, filename)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Image compression/upload failed: {e}")
            return image_value
    return image_value

# ---------- Realtime notification helper ----------
def notify_user(user_id: str, ntype: str, message: str, from_user_id: str = "system"):
    nid = f"notif_{uuid.uuid4().hex[:12]}"
    sb.table("notifications").insert({"notification_id": nid, "user_id": user_id, "from_user_id": from_user_id, "type": ntype, "message": message, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
    try:
        channel = sb.channel(f"user-{user_id}")
        channel.send({"type": "broadcast", "event": "new_notification", "payload": {"type": ntype, "message": message}})
    except Exception as e:
        logger.error(f"Realtime broadcast failed: {e}")

# ---------- Premium helpers ----------
def is_premium(user: dict) -> bool:
    tier = user.get("premium_tier")
    if not tier: return False
    expires = user.get("premium_expires_at")
    if not expires: return True
    return _parse_dt(expires) > datetime.now(timezone.utc)

def require_token(user: dict, cost: int = 1):
    if is_premium(user): return
    if user.get("tokens", 0) < cost:
        raise HTTPException(status_code=402, detail=f"You need {cost} token(s). Earn them in the balloon game or upgrade to premium.")
    sb.table("users").update({"tokens": user["tokens"] - cost}).eq("user_id", user["user_id"]).execute()
    user["tokens"] -= cost

# ---------- Models ----------
class LocationUpdatePayload(BaseModel):
    latitude: float; longitude: float; accuracy: Optional[float] = None

class GoogleAuthPayload(BaseModel):
    id_token: str; email: str; name: str; picture: str; ref: Optional[str] = None

class ProfileSetupPayload(BaseModel):
    date_of_birth: str; gender: str; health_status: str
    sexual_orientation: Optional[str] = ""
    positive_since: Optional[str] = ""
    height: Optional[str] = ""
    ethnicity: Optional[str] = ""
    religion: Optional[str] = ""
    display_name: Optional[str] = ""; bio: Optional[str] = ""
    interests: Optional[str] = ""; looking_for: Optional[str] = ""
    education: Optional[str] = ""; kids: Optional[str] = ""
    want_kids: Optional[str] = ""; smoke: Optional[str] = ""
    drink: Optional[str] = ""; employment: Optional[str] = ""
    profile_image: Optional[str] = ""; gallery_images: Optional[List[str]] = []
    pref_gender: Optional[str] = ""; pref_min_age: Optional[int] = 18
    pref_max_age: Optional[int] = 99; pref_country: Optional[str] = ""
    pref_max_distance: Optional[int] = 50; pref_health_status: Optional[str] = ""
    pref_sexual_orientation: Optional[str] = ""
    profile_hidden: Optional[bool] = False
    hide_from_min_age: Optional[int] = None; hide_from_max_age: Optional[int] = None
    hide_from_health_statuses: Optional[str] = ""
    visible_to: Optional[str] = "all"
    lock_all_images: Optional[bool] = False

class ProfileUpdatePayload(BaseModel):
    date_of_birth: Optional[str] = None; gender: Optional[str] = None; health_status: Optional[str] = None
    sexual_orientation: Optional[str] = None
    positive_since: Optional[str] = None
    height: Optional[str] = None
    ethnicity: Optional[str] = None
    religion: Optional[str] = None
    display_name: Optional[str] = None; bio: Optional[str] = None; interests: Optional[str] = None
    looking_for: Optional[str] = None; education: Optional[str] = None; kids: Optional[str] = None
    want_kids: Optional[str] = None; smoke: Optional[str] = None; drink: Optional[str] = None
    employment: Optional[str] = None; profile_image: Optional[str] = None
    gallery_images: Optional[List[str]] = None
    pref_gender: Optional[str] = None; pref_min_age: Optional[int] = None
    pref_max_age: Optional[int] = None; pref_country: Optional[str] = None
    pref_max_distance: Optional[int] = None; pref_health_status: Optional[str] = None
    pref_sexual_orientation: Optional[str] = None
    profile_hidden: Optional[bool] = None
    hide_from_min_age: Optional[int] = None; hide_from_max_age: Optional[int] = None
    hide_from_health_statuses: Optional[str] = None
    visible_to: Optional[str] = None
    lock_all_images: Optional[bool] = None

class CreateStoryPayload(BaseModel):
    content: str; category: str; title: Optional[str] = ""
class CreateCommentPayload(BaseModel):
    content: str; parent_id: Optional[str] = None
class SwipePayload(BaseModel):
    swiped_id: str; direction: str; swipe_type: Optional[str] = 'dating'
class MatchMessagePayload(BaseModel):
    content: str
class ReportPayload(BaseModel):
    reported_user_id: str; reason: Optional[str] = ""
class BlockPayload(BaseModel):
    blocked_user_id: str

# ---------- Auth ----------
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
    if _parse_dt(session["expires_at"]) < datetime.now(timezone.utc): raise HTTPException(status_code=401)
    user = _maybe(sb.table("users").select("*").eq("user_id", session["user_id"]).maybe_single().execute())
    if not user: raise HTTPException(status_code=401, detail="User not found")
    if user.get("deleted"):
        raise HTTPException(status_code=401, detail="Account deleted")

    # Auto‑renew / expiry check
    old_tier = user.get("premium_tier")
    if old_tier and not is_premium(user):
        if user.get("auto_renew"):
            cost = GOLD_COST if old_tier == "gold" else PREMIUM_COST
            days = GOLD_DAYS if old_tier == "gold" else PREMIUM_DAYS
            if user.get("diamonds", 0) >= cost:
                new_diamonds = user["diamonds"] - cost
                new_expires = datetime.now(timezone.utc) + timedelta(days=days)
                sb.table("users").update({"diamonds": new_diamonds, "premium_expires_at": new_expires.isoformat()}).eq("user_id", user["user_id"]).execute()
                user["diamonds"] = new_diamonds
                user["premium_expires_at"] = new_expires.isoformat()
            else:
                sb.table("users").update({"premium_tier": None, "premium_expires_at": None, "auto_renew": False}).eq("user_id", user["user_id"]).execute()
                user["premium_tier"] = None
                user["premium_expires_at"] = None
                user["auto_renew"] = False
                notify_user(user["user_id"], "auto_renew_failed", "Auto‑renewal failed – insufficient diamonds. Premium expired.")
        else:
            sb.table("users").update({"premium_tier": None, "premium_expires_at": None}).eq("user_id", user["user_id"]).execute()
            user["premium_tier"] = None
            user["premium_expires_at"] = None

    sb.table("users").update({"last_active": datetime.now(timezone.utc).isoformat()}).eq("user_id", user["user_id"]).execute()
    return user

@app.get("/")
def root():
    return {"message": "Haven API is running"}

@api_router.get("/")
def api_root():
    return {"message": "Haven API"}

@api_router.post("/auth/google")
def auth_google(payload: GoogleAuthPayload, request: Request, response: Response):
    check_rate_limit(f"auth_{request.client.host}", max_req=5)
    email, name, picture = payload.email, payload.name, payload.picture
    session_token = f"session_{uuid.uuid4().hex[:32]}"
    existing = _maybe(sb.table("users").select("*").eq("email", email).eq("deleted", False).maybe_single().execute())
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing:
        user_id = existing["user_id"]
        sb.table("users").update({"name": name, "picture": picture, "last_active": now_iso, "deleted": False}).eq("user_id", user_id).execute()
    else:
        # Check if there's a deleted account with this email
        deleted_user = _maybe(sb.table("users").select("*").eq("email", email).eq("deleted", True).maybe_single().execute())
        if deleted_user:
            if deleted_user.get("banned"):
                raise HTTPException(403, "Your account has been banned.")
            user_id = deleted_user["user_id"]
            sb.table("users").update({"name": name, "picture": picture, "last_active": now_iso, "deleted": False}).eq("user_id", user_id).execute()
        else:
            user_id = f"user_{uuid.uuid4().hex[:12]}"
            sb.table("users").insert({
                "user_id": user_id, "email": email, "name": name, "picture": picture,
                "created_at": now_iso, "last_active": now_iso,
                "tokens": 100, "diamonds": 5, "verified": False
            }).execute()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    sb.table("user_sessions").upsert({"session_token": session_token, "user_id": user_id, "expires_at": expires_at.isoformat(), "created_at": now_iso}).execute()
    response.set_cookie(key="session_token", value=session_token, httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/", max_age=7*24*60*60)
    return {"ok": True, "user_id": user_id, "token": session_token}

@api_router.get("/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    onboarding_complete = profile.get("onboarding_complete", False) if profile else False
    has_gps = profile and profile.get("gps_latitude") is not None
    gps_stale = False
    if has_gps and profile.get("gps_verified_at"):
        gps_age = datetime.now(timezone.utc) - _parse_dt(profile["gps_verified_at"])
        gps_stale = gps_age > timedelta(hours=MAX_GPS_AGE_HOURS)
    unread = 0
    notif_cnt = sb.table("notifications").select("notification_id", count="exact").eq("user_id", user["user_id"]).eq("read", False).execute()
    unread = notif_cnt.count if hasattr(notif_cnt, 'count') else 0
    return {
        "user_id": user["user_id"], "email": user["email"], "name": user["name"],
        "picture": user.get("picture", ""),
        "onboarding_complete": onboarding_complete,
        "unread_notifications": unread,
        "has_gps": has_gps, "gps_stale": gps_stale,
        "needs_location": not has_gps or gps_stale,
        "tokens": user.get("tokens", 0),
        "diamonds": user.get("diamonds", 0),
        "verified": user.get("verified", False),
        "premium_tier": user.get("premium_tier"),
        "premium_expires_at": user.get("premium_expires_at"),
        "auto_renew": user.get("auto_renew", False),
        "privacy_accepted": user.get("privacy_accepted_at") is not None,
    }

@api_router.post("/accept-privacy")
def accept_privacy(user: dict = Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    sb.table("users").update({"privacy_accepted_at": now}).eq("user_id", user["user_id"]).execute()
    return {"ok": True}

@api_router.post("/auth/logout")
def auth_logout(response: Response, session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"), authorization: Optional[str] = Header(default=None)):
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "): token = authorization.split(" ", 1)[1]
    if token: sb.table("user_sessions").delete().eq("session_token", token).execute()
    response.delete_cookie(key="session_token", path="/", samesite="lax", secure=False)
    return {"ok": True}

@api_router.delete("/auth/me")
def delete_account(user: dict = Depends(get_current_user)):
    sb.table("users").update({"deleted": True}).eq("user_id", user["user_id"]).execute()
    sb.table("user_sessions").delete().eq("user_id", user["user_id"]).execute()
    sb.table("user_profiles").update({
        "display_name": None, "bio": None, "date_of_birth": None,
        "profile_image": None, "gallery_images": None,
        "gps_latitude": None, "gps_longitude": None, "gps_verified_at": None,
    }).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "message": "Account deleted"}

# ---------- Lookup endpoints ----------
@api_router.get("/lookup/ethnicities")
def get_ethnicities():
    return ETHNICITY_LIST

# ---------- Stats ----------
@api_router.get("/stats/online")
def online_users():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)
    res = sb.table("users").select("user_id", count="exact").gte("last_active", cutoff.isoformat()).execute()
    count = res.count if hasattr(res, 'count') else 0
    return {"online": count}

# ---------- Economy ----------
@api_router.post("/purchase/verify")
def purchase_verify(user: dict = Depends(get_current_user)):
    if user.get("verified"): raise HTTPException(400, "Already verified")
    if user.get("diamonds", 0) < VERIFY_COST: raise HTTPException(402, "Not enough diamonds")
    new_diamonds = user["diamonds"] - VERIFY_COST
    sb.table("users").update({"diamonds": new_diamonds, "verified": True}).eq("user_id", user["user_id"]).execute()
    sb.table("diamond_purchases").insert({"purchase_id": f"pur_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"], "item": "verify", "diamond_cost": VERIFY_COST}).execute()
    return {"ok": True, "verified": True, "diamonds": new_diamonds}

@api_router.post("/purchase/premium")
def purchase_premium(tier: str = "gold", user: dict = Depends(get_current_user)):
    if not user.get("verified"):
        raise HTTPException(400, "You must be verified before purchasing premium.")
    if is_premium(user):
        raise HTTPException(400, "You already have an active premium subscription. Please wait for it to expire.")
    if tier not in ["gold", "platinum"]:
        raise HTTPException(400, "Invalid tier")
    cost = GOLD_COST if tier == "gold" else PREMIUM_COST
    days = GOLD_DAYS if tier == "gold" else PREMIUM_DAYS
    if user.get("diamonds", 0) < cost: raise HTTPException(402, "Not enough diamonds")
    new_diamonds = user["diamonds"] - cost
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    sb.table("users").update({"diamonds": new_diamonds, "premium_tier": tier, "premium_expires_at": expires.isoformat()}).eq("user_id", user["user_id"]).execute()
    sb.table("diamond_purchases").insert({"purchase_id": f"pur_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"], "item": f"{tier}_premium", "diamond_cost": cost}).execute()
    return {"ok": True, "premium_tier": tier, "diamonds": new_diamonds, "expires_at": expires.isoformat()}

@api_router.put("/premium/auto-renew")
def toggle_auto_renew(user: dict = Depends(get_current_user)):
    current = user.get("auto_renew", False)
    new_val = not current
    sb.table("users").update({"auto_renew": new_val}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "auto_renew": new_val}

# ---------- PayPal Diamond Purchase ----------
PAYPAL_API_BASE = "https://api-m.paypal.com"

@api_router.post("/diamonds/create-order")
async def create_diamond_order(
    package_id: str,
    request: Request,
    user: dict = Depends(get_current_user)
):
    packages = {
        "52":  {"diamonds": 52,  "amount": 3.00,  "label": "$3"},
        "120": {"diamonds": 120, "amount": 7.00,  "label": "$7"},
        "310": {"diamonds": 310, "amount": 16.00, "label": "$16"},
        "770": {"diamonds": 770, "amount": 32.00, "label": "$32"},
    }
    if package_id not in packages:
        raise HTTPException(400, "Invalid package")

    pkg = packages[package_id]
    PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(500, "PayPal credentials not configured")

    auth_response = await httpx.AsyncClient().post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        headers={"Accept": "application/json"},
    )
    if auth_response.status_code != 200:
        raise HTTPException(500, "PayPal authentication failed")

    access_token = auth_response.json()["access_token"]

    order_data = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": "USD",
                "value": str(pkg["amount"]),
            },
            "description": f"{pkg['diamonds']} Diamonds for {pkg['label']}",
            "custom_id": user["user_id"] + ":" + package_id,
        }],
        "application_context": {
            "brand_name": "Haven",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
            "return_url": "https://havenpositive.online/shop",
            "cancel_url": "https://havenpositive.online/shop",
        }
    }

    order_response = await httpx.AsyncClient().post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        json=order_data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    if order_response.status_code != 201:
        raise HTTPException(500, "PayPal order creation failed")

    return order_response.json()

@api_router.post("/diamonds/capture-order")
async def capture_diamond_order(
    order_id: str,
    package_id: str,
    user: dict = Depends(get_current_user)
):
    PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(500, "PayPal credentials not configured")

    auth_response = await httpx.AsyncClient().post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        headers={"Accept": "application/json"},
    )
    if auth_response.status_code != 200:
        raise HTTPException(500, "PayPal authentication failed")

    access_token = auth_response.json()["access_token"]

    capture_response = await httpx.AsyncClient().post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    if capture_response.status_code not in (200, 201):
        raise HTTPException(500, "Payment capture failed")

    packages = {
        "52":  52,
        "120": 120,
        "310": 310,
        "770": 770,
    }
    diamonds = packages.get(package_id)
    if not diamonds:
        raise HTTPException(400, "Invalid package")

    new_diamonds = user.get("diamonds", 0) + diamonds
    sb.table("users").update({"diamonds": new_diamonds}).eq("user_id", user["user_id"]).execute()

    sb.table("diamond_purchases").insert({
        "purchase_id": f"diam_{uuid.uuid4().hex[:12]}",
        "user_id": user["user_id"],
        "item": f"{diamonds}_diamonds_paypal",
        "diamond_cost": 0,
        "purchased_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    return {"ok": True, "diamonds": new_diamonds}

# ---------- Earn tokens (game) ----------
@api_router.post("/earn-tokens")
def earn_tokens(user: dict = Depends(get_current_user)):
    if is_premium(user):
        raise HTTPException(400, "Premium members don't earn tokens")
    last_earn = user.get("last_token_earned")
    if last_earn:
        last = _parse_dt(last_earn)
        if datetime.now(timezone.utc) - last < timedelta(minutes=1):
            raise HTTPException(400, "You can earn tokens again in a minute")
    new_tokens = user.get("tokens", 0) + 15
    sb.table("users").update({"tokens": new_tokens, "last_token_earned": datetime.now(timezone.utc).isoformat()}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "tokens_awarded": 15, "total_tokens": new_tokens}

# ---------- Earn tokens from ad (button) ----------
@api_router.post("/earn-tokens-ad")
def earn_tokens_ad(user: dict = Depends(get_current_user)):
    if is_premium(user):
        raise HTTPException(400, "Premium members don't earn tokens")
    last_earn = user.get("last_ad_token_earned")
    if last_earn:
        last = _parse_dt(last_earn)
        if datetime.now(timezone.utc) - last < timedelta(seconds=30):
            raise HTTPException(400, "You can earn ad tokens again in 30 seconds")
    new_tokens = user.get("tokens", 0) + 2
    sb.table("users").update({"tokens": new_tokens, "last_ad_token_earned": datetime.now(timezone.utc).isoformat()}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "tokens_awarded": 2, "total_tokens": new_tokens}

# ---------- Location API ----------
@api_router.post("/location/update")
async def update_location(payload: LocationUpdatePayload, user: dict = Depends(get_current_user)):
    if not (-90 <= payload.latitude <= 90) or not (-180 <= payload.longitude <= 180):
        raise HTTPException(400, "Invalid coordinates")
    if payload.accuracy and payload.accuracy > 500:
        raise HTTPException(400, "Location accuracy too low ( >500m). Please enable GPS and try again.")
    now = datetime.now(timezone.utc)
    country, city = reverse_geocode(payload.latitude, payload.longitude)
    profile_data = {
        "gps_latitude": payload.latitude, "gps_longitude": payload.longitude,
        "gps_verified_at": now.isoformat(), "gps_accuracy": payload.accuracy,
        "location_source": "gps", "latitude": payload.latitude, "longitude": payload.longitude,
        "country": country, "city": city or "", "updated_at": now.isoformat(),
    }
    existing = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", user["user_id"]).maybe_single().execute())
    if existing:
        sb.table("user_profiles").update(profile_data).eq("user_id", user["user_id"]).execute()
    else:
        profile_data["user_id"] = user["user_id"]; profile_data["created_at"] = now.isoformat()
        sb.table("user_profiles").insert(profile_data).execute()
    return {"ok": True, "message": "GPS location updated", "latitude": payload.latitude, "longitude": payload.longitude, "country": country, "city": city}

@api_router.get("/location/ip-fallback")
async def ip_fallback(request: Request, user: dict = Depends(get_current_user)):
    client_ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    location = await get_location_from_ip(client_ip)
    if location:
        now = datetime.now(timezone.utc)
        profile_data = {
            "gps_latitude": location['latitude'], "gps_longitude": location['longitude'],
            "gps_verified_at": now.isoformat(), "location_source": "ip",
            "latitude": location['latitude'], "longitude": location['longitude'],
            "country": location.get('country', ''), "city": location.get('city', ''),
            "updated_at": now.isoformat(),
        }
        existing = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", user["user_id"]).maybe_single().execute())
        if existing:
            sb.table("user_profiles").update(profile_data).eq("user_id", user["user_id"]).execute()
        else:
            profile_data["user_id"] = user["user_id"]; sb.table("user_profiles").insert(profile_data).execute()
        return {"ok": True, "latitude": location['latitude'], "longitude": location['longitude'], "country": location.get('country'), "city": location.get('city'), "source": "ip"}
    return {"ok": False, "message": "Could not determine location from IP"}

@api_router.get("/location/status")
def get_location_status(user: dict = Depends(get_current_user)):
    profile = _maybe(sb.table("user_profiles").select("gps_latitude,gps_longitude,gps_verified_at,location_source").eq("user_id", user["user_id"]).maybe_single().execute())
    if not profile or profile.get("gps_latitude") is None:
        return {"has_location": False, "needs_location": True, "message": "No GPS location set"}
    gps_age = datetime.now(timezone.utc) - _parse_dt(profile["gps_verified_at"])
    is_stale = gps_age > timedelta(hours=MAX_GPS_AGE_HOURS)
    return {"has_location": True, "needs_location": is_stale, "is_stale": is_stale, "location_source": profile.get("location_source", "unknown"), "last_updated": profile.get("gps_verified_at"), "message": "GPS location expired - please refresh" if is_stale else "GPS location valid"}

# ---------- Profile ----------
def get_profile(user: dict) -> dict:
    # If we only have a user_id, fetch the full user row first
    if "email" not in user or "last_active" not in user:
        full_user = _maybe(sb.table("users").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
        if full_user:
            user = full_user

    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    if not profile:
        return {
            "user_id": user["user_id"], "email": user.get("email",""), "name": user.get("name",""),
            "date_of_birth": None, "gender": None, "country": None, "city": None,
            "health_status": None, "latitude": None, "longitude": None,
            "sexual_orientation": "", "positive_since": "", "height": "", "ethnicity": "", "religion": "",
            "display_name": user.get("name",""), "bio": "", "interests": "", "looking_for": "",
            "education": "", "kids": "", "want_kids": "", "smoke": "", "drink": "", "employment": "",
            "profile_image": user.get("picture",""), "gallery_images": [],
            "onboarding_complete": False,
            "pref_gender": "", "pref_min_age": 18, "pref_max_age": 99,
            "pref_country": "", "pref_max_distance": 50, "pref_health_status": "",
            "profile_hidden": False, "hide_from_min_age": None, "hide_from_max_age": None,
            "hide_from_health_statuses": "",
            "gps_latitude": None, "gps_longitude": None, "gps_verified_at": None,
            "location_source": "none",
            "last_active": user.get("last_active"),
            "verified": False, "premium_tier": None,
            "visible_to": "all", "pref_sexual_orientation": "", "lock_all_images": False
        }
    lat = profile.get("gps_latitude"); lon = profile.get("gps_longitude")
    country = profile.get("country") if lat is not None else None
    city = profile.get("city") if lat is not None else None
    return {
        "user_id": profile["user_id"], "email": user.get("email",""), "name": user.get("name",""),
        "date_of_birth": profile.get("date_of_birth"), "gender": profile.get("gender"),
        "country": country, "city": city,
        "health_status": profile.get("health_status"),
        "latitude": lat, "longitude": lon,
        "sexual_orientation": profile.get("sexual_orientation",""),
        "positive_since": profile.get("positive_since",""),
        "height": profile.get("height",""),
        "ethnicity": profile.get("ethnicity",""),
        "religion": profile.get("religion",""),
        "display_name": profile.get("display_name", user.get("name","")),
        "bio": profile.get("bio",""), "interests": profile.get("interests",""),
        "looking_for": profile.get("looking_for",""),
        "education": profile.get("education",""), "kids": profile.get("kids",""),
        "want_kids": profile.get("want_kids",""), "smoke": profile.get("smoke",""),
        "drink": profile.get("drink",""), "employment": profile.get("employment",""),
        "profile_image": profile.get("profile_image") or user.get("picture",""),
        "gallery_images": profile.get("gallery_images") or [],
        "onboarding_complete": profile.get("onboarding_complete", False),
        "pref_gender": profile.get("pref_gender",""), "pref_min_age": profile.get("pref_min_age",18),
        "pref_max_age": profile.get("pref_max_age",99), "pref_country": profile.get("pref_country",""),
        "pref_max_distance": profile.get("pref_max_distance",50),
        "pref_health_status": profile.get("pref_health_status",""),
        "pref_sexual_orientation": profile.get("pref_sexual_orientation",""),
        "profile_hidden": profile.get("profile_hidden", False),
        "hide_from_min_age": profile.get("hide_from_min_age"),
        "hide_from_max_age": profile.get("hide_from_max_age"),
        "hide_from_health_statuses": profile.get("hide_from_health_statuses",""),
        "visible_to": profile.get("visible_to", "all"),
        "lock_all_images": profile.get("lock_all_images", False),
        "gps_latitude": lat, "gps_longitude": lon,
        "gps_verified_at": profile.get("gps_verified_at"),
        "location_source": profile.get("location_source", "none"),
        "last_active": user.get("last_active"),        # now properly fetched
        "verified": user.get("verified", False),
        "premium_tier": user.get("premium_tier")
    }



@api_router.post("/profile/setup")
def setup_profile(payload: ProfileSetupPayload, user: dict = Depends(get_current_user)):
    if payload.ethnicity and payload.ethnicity not in ETHNICITY_LIST:
        raise HTTPException(400, f"Ethnicity must be one of: {', '.join(ETHNICITY_LIST)}")
    if not user.get("verified"):
        payload.visible_to = "all"
        payload.lock_all_images = False
    if payload.visible_to == "verified_only" and not user.get("verified"):
        raise HTTPException(400, "Only verified users can restrict visibility to verified members")
    if payload.lock_all_images and not user.get("verified"):
        raise HTTPException(400, "Only verified users can lock all images")
    existing_profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    lat = existing_profile.get("gps_latitude") if existing_profile else None
    lon = existing_profile.get("gps_longitude") if existing_profile else None
    country = existing_profile.get("country") if existing_profile and lat else None
    city = existing_profile.get("city") if existing_profile and lat else None
    profile_image = process_image_field(payload.profile_image, user["user_id"], "profile")
    gallery = []
    for i, img in enumerate(payload.gallery_images or []):
        gallery.append(process_image_field(img, user["user_id"], f"gallery_{i}"))
    profile_data = {
        "user_id": user["user_id"], "date_of_birth": payload.date_of_birth, "gender": payload.gender,
        "country": country, "city": city, "health_status": payload.health_status,
        "sexual_orientation": payload.sexual_orientation or "",
        "positive_since": payload.positive_since or "",
        "height": payload.height or "",
        "ethnicity": payload.ethnicity or "",
        "religion": payload.religion or "",
        "latitude": lat, "longitude": lon,
        "display_name": payload.display_name or user.get("name",""),
        "bio": payload.bio or "", "interests": payload.interests or "",
        "looking_for": payload.looking_for or "",
        "education": payload.education or "", "kids": payload.kids or "",
        "want_kids": payload.want_kids or "", "smoke": payload.smoke or "",
        "drink": payload.drink or "", "employment": payload.employment or "",
        "profile_image": profile_image, "gallery_images": gallery,
        "pref_gender": payload.pref_gender or "", "pref_min_age": payload.pref_min_age,
        "pref_max_age": payload.pref_max_age, "pref_country": payload.pref_country or "",
        "pref_max_distance": payload.pref_max_distance,
        "pref_health_status": payload.pref_health_status or "",
        "pref_sexual_orientation": payload.pref_sexual_orientation or "",
        "profile_hidden": payload.profile_hidden if user.get("verified") else False,
        "hide_from_min_age": payload.hide_from_min_age if user.get("verified") else None,
        "hide_from_max_age": payload.hide_from_max_age if user.get("verified") else None,
        "hide_from_health_statuses": payload.hide_from_health_statuses if user.get("verified") else "",
        "visible_to": payload.visible_to if user.get("verified") else "all",
        "lock_all_images": payload.lock_all_images if user.get("verified") else False,
        "onboarding_complete": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing_profile:
        if existing_profile.get("gps_latitude"):
            profile_data["gps_latitude"] = existing_profile["gps_latitude"]
            profile_data["gps_longitude"] = existing_profile["gps_longitude"]
            profile_data["gps_verified_at"] = existing_profile["gps_verified_at"]
            profile_data["location_source"] = existing_profile.get("location_source", "none")
        sb.table("user_profiles").update(profile_data).eq("user_id", user["user_id"]).execute()
    else:
        profile_data["created_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("user_profiles").insert(profile_data).execute()
    return {"ok": True, "profile": get_profile(user)}

@api_router.put("/profile")
def update_profile(payload: ProfileUpdatePayload, user: dict = Depends(get_current_user)):
    updates = {}
    all_fields = [
        "date_of_birth", "gender", "health_status",
        "sexual_orientation", "positive_since", "height", "ethnicity", "religion",
        "display_name", "bio", "interests", "looking_for",
        "education", "kids", "want_kids", "smoke", "drink", "employment",
        "pref_gender", "pref_min_age", "pref_max_age", "pref_country",
        "pref_max_distance", "pref_health_status", "pref_sexual_orientation",
        "hide_from_min_age", "hide_from_max_age", "hide_from_health_statuses",
        "visible_to", "lock_all_images"
    ]
    for field in all_fields:
        value = getattr(payload, field, None)
        if value is not None:
            if field == "ethnicity" and value not in ETHNICITY_LIST:
                raise HTTPException(400, f"Ethnicity must be one of: {', '.join(ETHNICITY_LIST)}")
            if field == "visible_to" and value == "verified_only" and not user.get("verified"):
                raise HTTPException(400, "Only verified users can restrict visibility to verified members")
            if field == "lock_all_images" and value and not user.get("verified"):
                raise HTTPException(400, "Only verified users can lock all images")
            updates[field] = value
    if payload.profile_hidden is not None:
        if not is_premium(user):
            updates["profile_hidden"] = False
        else:
            updates["profile_hidden"] = payload.profile_hidden
    if payload.gallery_images is not None:
        if not is_premium(user) and len(payload.gallery_images) > 5:
            raise HTTPException(400, "Free users can only upload up to 5 images")
        new_gallery = []
        for i, img in enumerate(payload.gallery_images):
            new_gallery.append(process_image_field(img, user["user_id"], f"gallery_{i}"))
        updates["gallery_images"] = new_gallery
    if payload.profile_image is not None:
        updates["profile_image"] = process_image_field(payload.profile_image, user["user_id"], "profile")
    if not updates: return {"ok": True, "profile": get_profile(user)}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", user["user_id"]).maybe_single().execute())
    if existing:
        sb.table("user_profiles").update(updates).eq("user_id", user["user_id"]).execute()
    else:
        updates["user_id"] = user["user_id"]; updates["onboarding_complete"] = False
        updates["created_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("user_profiles").insert(updates).execute()
    return {"ok": True, "profile": get_profile(user)}

@api_router.get("/profile")
def get_my_profile(user: dict = Depends(get_current_user)):
    return get_profile(user)

# ---------- Discovery (optimised) ----------
@api_router.get("/discover/profiles")
def get_discover_profiles(
    user: dict = Depends(get_current_user),
    page: Optional[int] = None,
    limit: Optional[int] = None,
    gender: Optional[str] = None,
    health_status: Optional[str] = None,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    country: Optional[str] = None,
    max_distance: Optional[int] = None,
    sexual_orientation: Optional[str] = None,
):
    viewer_profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    if not viewer_profile: return []
    my_lat = viewer_profile.get("gps_latitude") or viewer_profile.get("latitude")
    my_lon = viewer_profile.get("gps_longitude") or viewer_profile.get("longitude")
    if my_lat is None or my_lon is None: return []

    pref_gender = gender if gender is not None else viewer_profile.get("pref_gender", "")
    pref_health = health_status if health_status is not None else viewer_profile.get("pref_health_status", "")
    pref_min_age = min_age if min_age is not None else viewer_profile.get("pref_min_age", 18)
    pref_max_age = max_age if max_age is not None else viewer_profile.get("pref_max_age", 99)
    pref_country = country if country is not None else viewer_profile.get("pref_country", "")
    pref_max_distance = max_distance if max_distance is not None else viewer_profile.get("pref_max_distance", 50)
    pref_sexual_orientation = sexual_orientation if sexual_orientation is not None else viewer_profile.get("pref_sexual_orientation", "")

    today = datetime.now(timezone.utc).date()
    min_birth_date = today.replace(year=today.year - pref_max_age)
    max_birth_date = today.replace(year=today.year - pref_min_age)

    matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").execute()
    matched_ids = set()
    for m in (matches.data or []):
        partner = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        matched_ids.add(partner)

    query = sb.table("user_profiles").select("*", count="exact") \
        .neq("user_id", user["user_id"]) \
        .eq("onboarding_complete", True) \
        .not_.is_("gps_latitude", None) \
        .eq("profile_hidden", False)

    if not user.get("verified"):
        query = query.or_("visible_to.eq.all,visible_to.is.null")

    if pref_gender:
        query = query.eq("gender", pref_gender)
    if pref_health:
        query = query.eq("health_status", pref_health)
    if pref_country:
        query = query.eq("country", pref_country)
    if pref_sexual_orientation:
        query = query.eq("sexual_orientation", pref_sexual_orientation)
    if pref_min_age and pref_max_age:
        query = query.gte("date_of_birth", min_birth_date.isoformat()) \
                     .lte("date_of_birth", max_birth_date.isoformat())

    for mid in matched_ids:
        query = query.neq("user_id", mid)

    if page is not None and limit is not None:
        start = (page - 1) * limit
        end = start + limit - 1
        query = query.range(start, end)
    else:
        query = query.limit(50)

    profiles = query.execute().data or []
    if not profiles:
        return []

    user_ids = [p["user_id"] for p in profiles]
    users_data = sb.table("users").select("user_id,verified,premium_tier,last_active").in_("user_id", user_ids).execute().data or []
    user_status = {u["user_id"]: u for u in users_data}

    filtered = []
    for p in profiles:
        if p.get("profile_hidden"): continue
        p_lat = p.get("gps_latitude"); p_lon = p.get("gps_longitude")
        distance = None
        if p_lat is not None and p_lon is not None:
            distance = haversine(my_lat, my_lon, p_lat, p_lon)
            if pref_max_distance and distance > pref_max_distance: continue
        p["distance_km"] = round(distance, 1) if distance is not None else None
        status = user_status.get(p["user_id"], {})
        p["verified"] = status.get("verified", False)
        p["premium_tier"] = status.get("premium_tier")
        filtered.append(p)

    if not is_premium(user):
        require_token(user, len(filtered))

    return filtered

@api_router.post("/discover/swipe")
def swipe_profile(payload: SwipePayload, user: dict = Depends(get_current_user)):
    if not is_premium(user):
        if user.get("tokens", 0) < 1:
            raise HTTPException(402, "You need a token to swipe. Earn tokens or upgrade to premium.")
        sb.table("users").update({"tokens": user["tokens"] - 1}).eq("user_id", user["user_id"]).execute()
        user["tokens"] -= 1

    if payload.direction not in ["like","pass"]: raise HTTPException(400)
    target = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", payload.swiped_id).maybe_single().execute())
    if not target: raise HTTPException(404)
    existing = _maybe(sb.table("profile_swipes").select("*").eq("swiper_id", user["user_id"]).eq("swiped_id", payload.swiped_id).eq("swipe_type", payload.swipe_type).maybe_single().execute())
    if not existing:
        try:
            sb.table("profile_swipes").insert({
                "swipe_id": f"swp_{uuid.uuid4().hex[:12]}", "swiper_id": user["user_id"],
                "swiped_id": payload.swiped_id, "direction": payload.direction, "swipe_type": payload.swipe_type
            }).execute()
        except Exception: pass
    matched = False; match_id = None
    if payload.direction == "like":
        from_profile = get_profile(user)
        if payload.swipe_type == "dating":
            existing_req = _maybe(sb.table("dating_requests").select("*").eq("from_user_id", user["user_id"]).eq("to_user_id", payload.swiped_id).maybe_single().execute())
            if not existing_req:
                sb.table("dating_requests").insert({"request_id": f"dr_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": payload.swiped_id, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
                notify_user(payload.swiped_id, "dating_request", f"{from_profile.get('display_name', 'Someone')} sent you a dating request", user["user_id"])
        elif payload.swipe_type == "friendship":
            existing_req = _maybe(sb.table("friend_requests").select("*").eq("from_user_id", user["user_id"]).eq("to_user_id", payload.swiped_id).maybe_single().execute())
            if not existing_req:
                sb.table("friend_requests").insert({"request_id": f"fr_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": payload.swiped_id, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
                notify_user(payload.swiped_id, "friend_request", f"{from_profile.get('display_name', 'Someone')} sent you a friend request", user["user_id"])
        other = _maybe(sb.table("profile_swipes").select("*").eq("swiper_id", payload.swiped_id).eq("swiped_id", user["user_id"]).eq("direction","like").eq("swipe_type", payload.swipe_type).maybe_single().execute())
        if other:
            uid1, uid2 = sorted([user["user_id"], payload.swiped_id])
            exist_match = _maybe(sb.table("profile_matches").select("*").eq("user1_id", uid1).eq("user2_id", uid2).eq("swipe_type", payload.swipe_type).maybe_single().execute())
            if not exist_match:
                match_id = f"match_{uuid.uuid4().hex[:12]}"
                sb.table("profile_matches").insert({"match_id": match_id, "user1_id": uid1, "user2_id": uid2, "swipe_type": payload.swipe_type, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
                matched = True
                notify_user(payload.swiped_id, "match_new", f"You matched with {from_profile.get('display_name', 'Someone')}!", user["user_id"])
                notify_user(user["user_id"], "match_new", f"You matched with {from_profile.get('display_name', 'Someone')}!", payload.swiped_id)
            else: match_id = exist_match["match_id"]
    return {"ok": True, "matched": matched, "match_id": match_id, "direction": payload.direction}

# ---------- Random Chat token charge ----------
@api_router.post("/chat/start")
def chat_start(user: dict = Depends(get_current_user)):
    require_token(user, 1)
    return {"ok": True}

# ---------- Matches / Messages ----------
@api_router.get("/discover/matches")
def get_matches(swipe_type: Optional[str] = 'dating', user: dict = Depends(get_current_user)):
    matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").eq("swipe_type", swipe_type).order("created_at", desc=True).execute()
    result = []
    for m in (matches.data or []):
        partner_id = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", partner_id).maybe_single().execute())
        if profile:
            unread_res = sb.table("match_messages").select("message_id", count="exact").eq("match_id", m["match_id"]).eq("read", False).eq("sender_id", partner_id).execute()
            unread = unread_res.count if hasattr(unread_res, 'count') else 0
            result.append({
                "match_id": m["match_id"], "user_id": partner_id,
                "display_name": profile.get("display_name",""),
                "profile_image": profile.get("profile_image",""),
                "bio": profile.get("bio",""), "country": profile.get("country",""), "city": profile.get("city",""),
                "health_status": profile.get("health_status"),
                "created_at": m["created_at"], "unread_count": unread
            })
    return result

@api_router.get("/discover/matches/{match_id}/messages")
def get_match_messages(match_id: str, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(404)
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(403)
    msgs = sb.table("match_messages").select("*").eq("match_id", match_id).order("created_at").execute().data or []
    for msg in msgs:
        if msg["sender_id"] != user["user_id"] and not msg.get("read"):
            sb.table("match_messages").update({"read": True}).eq("message_id", msg["message_id"]).execute()
    return msgs

@api_router.post("/discover/matches/{match_id}/messages")
def send_match_message(match_id: str, payload: MatchMessagePayload, user: dict = Depends(get_current_user)):
    if contains_profanity(payload.content):
        raise HTTPException(400, "Message contains inappropriate language")
    if not is_premium(user):
        count_res = sb.table("match_messages").select("message_id", count="exact").eq("match_id", match_id).eq("sender_id", user["user_id"]).execute()
        msg_count = count_res.count if hasattr(count_res, 'count') else 0
        if msg_count >= 2:
            if user.get("tokens", 0) < 1:
                raise HTTPException(402, "You need a token to send more messages. Earn tokens or upgrade.")
            sb.table("users").update({"tokens": user["tokens"] - 1}).eq("user_id", user["user_id"]).execute()
            user["tokens"] -= 1
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(404)
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(403)
    msg = {"message_id": f"msg_{uuid.uuid4().hex[:12]}", "match_id": match_id, "sender_id": user["user_id"], "content": payload.content, "read": False, "created_at": datetime.now(timezone.utc).isoformat()}
    sb.table("match_messages").insert(msg).execute()
    other_id = match["user2_id"] if match["user1_id"] == user["user_id"] else match["user1_id"]
    from_profile = get_profile(user)
    notify_user(other_id, "match_message", f"New message from {from_profile.get('display_name', 'Someone')}", user["user_id"])
    return {"ok": True, "message": msg}

@api_router.delete("/discover/matches/{match_id}")
def unmatch(match_id: str, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(404, "Match not found")
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(403, "Not your match")
    sb.table("match_messages").delete().eq("match_id", match_id).execute()
    sb.table("profile_matches").delete().eq("match_id", match_id).execute()
    uid1, uid2 = match["user1_id"], match["user2_id"]
    sb.table("dating_requests").delete().or_(f"from_user_id.eq.{uid1},to_user_id.eq.{uid1}").or_(f"from_user_id.eq.{uid2},to_user_id.eq.{uid2}").execute()
    sb.table("friend_requests").delete().or_(f"from_user_id.eq.{uid1},to_user_id.eq.{uid1}").or_(f"from_user_id.eq.{uid2},to_user_id.eq.{uid2}").execute()
    sb.table("profile_swipes").delete().or_(f"swiper_id.eq.{uid1},swiped_id.eq.{uid1}").or_(f"swiper_id.eq.{uid2},swiped_id.eq.{uid2}").execute()
    return {"ok": True}

@api_router.get("/discover/matches/{match_id}/profile")
def get_match_profile(match_id: str, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(404, "Match not found")
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(403, "Not your match")
    partner_id = match["user2_id"] if match["user1_id"] == user["user_id"] else match["user1_id"]
    return get_profile({"user_id": partner_id})

@api_router.get("/unread-counts")
def get_unread_counts(user: dict = Depends(get_current_user)):
    unread_res = sb.table("match_messages").select("match_id").neq("sender_id", user["user_id"]).eq("read", False).execute()
    match_ids = set()
    for row in (unread_res.data or []): match_ids.add(row["match_id"])
    dating_count = 0; friend_count = 0
    if match_ids:
        for mid in match_ids:
            match = _maybe(sb.table("profile_matches").select("swipe_type").eq("match_id", mid).maybe_single().execute())
            if match:
                if match.get("swipe_type") == "friendship": friend_count += 1
                else: dating_count += 1
    return {"dating_unread": dating_count, "friend_unread": friend_count}

@api_router.get("/requests")
def get_requests(user: dict = Depends(get_current_user)):
    dating = sb.table("dating_requests").select("*").eq("to_user_id", user["user_id"]).eq("status", "pending").execute().data or []
    friend = sb.table("friend_requests").select("*").eq("to_user_id", user["user_id"]).eq("status", "pending").execute().data or []
    result = []
    for req in dating:
        from_profile = _maybe(sb.table("user_profiles").select("display_name,profile_image,country").eq("user_id", req["from_user_id"]).maybe_single().execute())
        if from_profile:
            result.append({
                "request_id": req["request_id"], "type": "dating",
                "from_user_id": req["from_user_id"], "from_name": from_profile.get("display_name","Someone"),
                "from_image": from_profile.get("profile_image",""), "from_country": from_profile.get("country",""),
                "created_at": req["created_at"], "status": req["status"],
            })
    for req in friend:
        from_profile = _maybe(sb.table("user_profiles").select("display_name,profile_image,country").eq("user_id", req["from_user_id"]).maybe_single().execute())
        if from_profile:
            result.append({
                "request_id": req["request_id"], "type": "friend",
                "from_user_id": req["from_user_id"], "from_name": from_profile.get("display_name","Someone"),
                "from_image": from_profile.get("profile_image",""), "from_country": from_profile.get("country",""),
                "created_at": req["created_at"], "status": req["status"],
            })
    return result

@api_router.post("/requests/{request_id}/respond")
def respond_request(request_id: str, action: str, user: dict = Depends(get_current_user)):
    if action not in ["accept","reject"]: raise HTTPException(400, "Action must be 'accept' or 'reject'")
    req = _maybe(sb.table("dating_requests").select("*").eq("request_id", request_id).eq("to_user_id", user["user_id"]).maybe_single().execute())
    table = "dating_requests"
    if not req:
        req = _maybe(sb.table("friend_requests").select("*").eq("request_id", request_id).eq("to_user_id", user["user_id"]).maybe_single().execute())
        table = "friend_requests"
    if not req: raise HTTPException(404, "Request not found")
    new_status = "accepted" if action == "accept" else "rejected"
    if req["status"] != "pending": raise HTTPException(400, "Request already handled")
    sb.table(table).update({"status": new_status, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("request_id", request_id).execute()
    if action == "accept":
        swipe_type = "dating" if table == "dating_requests" else "friendship"
        uid1, uid2 = sorted([user["user_id"], req["from_user_id"]])
        exist_match = _maybe(sb.table("profile_matches").select("*").eq("user1_id", uid1).eq("user2_id", uid2).eq("swipe_type", swipe_type).maybe_single().execute())
        if not exist_match:
            match_id = f"match_{uuid.uuid4().hex[:12]}"
            sb.table("profile_matches").insert({"match_id": match_id, "user1_id": uid1, "user2_id": uid2, "swipe_type": swipe_type, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
    from_profile = get_profile(user)
    notif_type = "dating_accepted" if table == "dating_requests" else "friend_accepted"
    notify_user(req["from_user_id"], notif_type, f"{from_profile.get('display_name','Someone')} {action}ed your request", user["user_id"])
    return {"ok": True, "status": new_status}

@api_router.get("/notifications")
def get_notifications(user: dict = Depends(get_current_user)):
    notifs = sb.table("notifications").select("*").eq("user_id", user["user_id"]).order("created_at", desc=True).limit(50).execute().data or []
    for n in notifs:
        from_profile = _maybe(sb.table("user_profiles").select("display_name,profile_image").eq("user_id", n["from_user_id"]).maybe_single().execute())
        n["from_name"] = from_profile.get("display_name","Someone") if from_profile else "Someone"
        n["from_image"] = from_profile.get("profile_image","") if from_profile else ""
    return notifs

@api_router.post("/notifications/read")
def mark_notifications_read(user: dict = Depends(get_current_user)):
    sb.table("notifications").update({"read": True}).eq("user_id", user["user_id"]).eq("read", False).execute()
    return {"ok": True}

# ---------- Report / Block ----------
@api_router.post("/report")
def report_user(payload: ReportPayload, user: dict = Depends(get_current_user)):
    existing = _maybe(sb.table("user_reports").select("report_id").eq("reporter_id", user["user_id"]).eq("reported_user_id", payload.reported_user_id).maybe_single().execute())
    if existing: raise HTTPException(400, "Already reported")
    sb.table("user_reports").insert({"report_id": f"rep_{uuid.uuid4().hex[:12]}", "reporter_id": user["user_id"], "reported_user_id": payload.reported_user_id, "reason": payload.reason or ""}).execute()
    return {"ok": True}

@api_router.post("/block")
def block_user(payload: BlockPayload, user: dict = Depends(get_current_user)):
    existing = _maybe(sb.table("user_blocks").select("block_id").eq("blocker_id", user["user_id"]).eq("blocked_user_id", payload.blocked_user_id).maybe_single().execute())
    if existing: raise HTTPException(400, "Already blocked")
    sb.table("user_blocks").insert({"block_id": f"blk_{uuid.uuid4().hex[:12]}", "blocker_id": user["user_id"], "blocked_user_id": payload.blocked_user_id}).execute()
    sb.table("profile_matches").delete().or_(f"and(user1_id.eq.{user['user_id']},user2_id.eq.{payload.blocked_user_id}),and(user1_id.eq.{payload.blocked_user_id},user2_id.eq.{user['user_id']})").execute()
    return {"ok": True}

# ---------- Stories ----------
@api_router.post("/stories")
def create_story(payload: CreateStoryPayload, user: dict = Depends(get_current_user)):
    if not user.get("verified"):
        raise HTTPException(403, "Only verified users can post stories")
    if contains_profanity(payload.content): raise HTTPException(400, "Story contains inappropriate language")
    if payload.category not in ["HIV","HPV","HSV","Other STD"]: raise HTTPException(400)
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    author_avatar = profile.get("profile_image") if profile else user.get("picture","")
    story = {"story_id": f"story_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"],
             "author_name": profile.get("display_name", user.get("name","")),
             "author_avatar": author_avatar, "content": payload.content, "category": payload.category,
             "title": payload.title or "", "likes": 0, "comment_count": 0,
             "created_at": datetime.now(timezone.utc).isoformat()}
    sb.table("stories").insert(story).execute()
    return {"ok": True, "story": story}

@api_router.get("/stories")
def get_stories(category: Optional[str] = None, user: dict = Depends(get_current_user)):
    query = sb.table("stories").select("*").order("created_at", desc=True).limit(100)
    if category: query = query.eq("category", category)
    stories = (query.execute()).data or []
    for s in stories:
        like = _maybe(sb.table("story_likes").select("like_id").eq("user_id", user["user_id"]).eq("story_id", s["story_id"]).maybe_single().execute())
        s["liked_by_user"] = like is not None
    return stories

@api_router.get("/stories/{story_id}")
def get_story(story_id: str, user: dict = Depends(get_current_user)):
    story = _maybe(sb.table("stories").select("*").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(404)
    like = _maybe(sb.table("story_likes").select("like_id").eq("user_id", user["user_id"]).eq("story_id", story_id).maybe_single().execute())
    story["liked_by_user"] = like is not None
    comments = sb.table("story_comments").select("*").eq("story_id", story_id).order("created_at").execute().data or []
    story["comments"] = build_comment_tree(comments)
    return story

@api_router.post("/stories/{story_id}/like")
def like_story(story_id: str, user: dict = Depends(get_current_user)):
    existing = _maybe(sb.table("story_likes").select("*").eq("user_id", user["user_id"]).eq("story_id", story_id).maybe_single().execute())
    story = _maybe(sb.table("stories").select("likes").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(404)
    current = story.get("likes",0)
    if existing:
        sb.table("story_likes").delete().eq("like_id", existing["like_id"]).execute()
        sb.table("stories").update({"likes": max(current-1,0)}).eq("story_id", story_id).execute()
        return {"ok": True, "liked": False}
    sb.table("story_likes").insert({"like_id": f"like_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"], "story_id": story_id}).execute()
    sb.table("stories").update({"likes": current+1}).eq("story_id", story_id).execute()
    return {"ok": True, "liked": True}

@api_router.post("/stories/{story_id}/comments")
def create_comment(story_id: str, payload: CreateCommentPayload, user: dict = Depends(get_current_user)):
    if contains_profanity(payload.content): raise HTTPException(400, "Comment contains inappropriate language")
    story = _maybe(sb.table("stories").select("story_id,comment_count").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(404)
    if payload.parent_id:
        parent = _maybe(sb.table("story_comments").select("comment_id").eq("comment_id", payload.parent_id).maybe_single().execute())
        if not parent: raise HTTPException(404)
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    author_avatar = profile.get("profile_image") if profile else user.get("picture","")
    comment = {"comment_id": f"cmt_{uuid.uuid4().hex[:12]}", "story_id": story_id, "user_id": user["user_id"],
               "author_name": profile.get("display_name", user.get("name","")),
               "author_avatar": author_avatar, "content": payload.content, "parent_id": payload.parent_id,
               "likes": 0, "reply_count": 0, "created_at": datetime.now(timezone.utc).isoformat()}
    sb.table("story_comments").insert(comment).execute()
    sb.table("stories").update({"comment_count": story.get("comment_count",0)+1}).eq("story_id", story_id).execute()
    if payload.parent_id:
        pc = _maybe(sb.table("story_comments").select("reply_count").eq("comment_id", payload.parent_id).maybe_single().execute())
        if pc: sb.table("story_comments").update({"reply_count": pc.get("reply_count",0)+1}).eq("comment_id", payload.parent_id).execute()
    return {"ok": True, "comment": comment}

@api_router.put("/stories/{story_id}/comments/{comment_id}")
def edit_comment(story_id: str, comment_id: str, payload: CreateCommentPayload, user: dict = Depends(get_current_user)):
    comment = _maybe(sb.table("story_comments").select("*").eq("comment_id", comment_id).eq("story_id", story_id).maybe_single().execute())
    if not comment: raise HTTPException(404, "Comment not found")
    if comment["user_id"] != user["user_id"]: raise HTTPException(403, "Not your comment")
    if contains_profanity(payload.content): raise HTTPException(400, "Comment contains inappropriate language")
    sb.table("story_comments").update({"content": payload.content}).eq("comment_id", comment_id).execute()
    return {"ok": True}

@api_router.delete("/stories/{story_id}/comments/{comment_id}")
def delete_comment(story_id: str, comment_id: str, user: dict = Depends(get_current_user)):
    comment = _maybe(sb.table("story_comments").select("*").eq("comment_id", comment_id).eq("story_id", story_id).maybe_single().execute())
    if not comment: raise HTTPException(404, "Comment not found")
    if comment["user_id"] != user["user_id"]: raise HTTPException(403, "Not your comment")
    sb.table("story_comments").delete().eq("comment_id", comment_id).execute()
    story = _maybe(sb.table("stories").select("comment_count").eq("story_id", story_id).maybe_single().execute())
    if story:
        new_count = max(0, story.get("comment_count", 0) - 1)
        sb.table("stories").update({"comment_count": new_count}).eq("story_id", story_id).execute()
    return {"ok": True}

@api_router.put("/stories/{story_id}")
def edit_story(story_id: str, payload: CreateStoryPayload, user: dict = Depends(get_current_user)):
    story = _maybe(sb.table("stories").select("*").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(404, "Story not found")
    if story["user_id"] != user["user_id"]: raise HTTPException(403, "Not your story")
    updates = {}
    if payload.title is not None: updates["title"] = payload.title
    if payload.content is not None: updates["content"] = payload.content
    if payload.category is not None: updates["category"] = payload.category
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("stories").update(updates).eq("story_id", story_id).execute()
    return {"ok": True}

@api_router.delete("/stories/{story_id}")
def delete_story(story_id: str, user: dict = Depends(get_current_user)):
    story = _maybe(sb.table("stories").select("*").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(404, "Story not found")
    if story["user_id"] != user["user_id"]: raise HTTPException(403, "Not your story")
    sb.table("story_comments").delete().eq("story_id", story_id).execute()
    sb.table("story_likes").delete().eq("story_id", story_id).execute()
    sb.table("stories").delete().eq("story_id", story_id).execute()
    return {"ok": True}

def build_comment_tree(comments):
    cmap = {c["comment_id"]: {**c, "replies": []} for c in comments}
    roots = []
    for c in comments:
        node = cmap[c["comment_id"]]
        if c.get("parent_id") and c["parent_id"] in cmap: cmap[c["parent_id"]]["replies"].append(node)
        else: roots.append(node)
    return roots

# ---------- Flexer Board ----------
@api_router.post("/flexer/join")
def flexer_join(amount: int = 6, user: dict = Depends(get_current_user)):
    if amount < 6: raise HTTPException(400, "Minimum 6 diamonds")
    if user.get("diamonds",0) < amount: raise HTTPException(402)
    now = datetime.now(timezone.utc)
    existing = _maybe(sb.table("flexer_cards").select("*").eq("user_id",user["user_id"]).gt("expires_at",now.isoformat()).maybe_single().execute())
    new_diamonds = user["diamonds"] - amount
    if existing:
        new_total = existing["diamonds_committed"] + amount
        new_expiry = max(_parse_dt(existing["expires_at"]), now) + timedelta(days=30)
        sb.table("flexer_cards").update({"diamonds_committed":new_total,"expires_at":new_expiry.isoformat(),"last_renewed_at":now.isoformat()}).eq("card_id",existing["card_id"]).execute()
    else:
        card_id = f"flex_{uuid.uuid4().hex[:12]}"
        expires = now + timedelta(days=30)
        sb.table("flexer_cards").insert({"card_id":card_id,"user_id":user["user_id"],"diamonds_committed":amount,"created_at":now.isoformat(),"expires_at":expires.isoformat(),"last_renewed_at":now.isoformat()}).execute()
    sb.table("users").update({"diamonds":new_diamonds}).eq("user_id",user["user_id"]).execute()
    return {"ok":True}

@api_router.post("/flexer/increment")
def flexer_increment(amount:int, user:dict=Depends(get_current_user)):
    if amount<1: raise HTTPException(400)
    if user.get("diamonds",0)<amount: raise HTTPException(402)
    existing = _maybe(sb.table("flexer_cards").select("*").eq("user_id",user["user_id"]).gt("expires_at",datetime.now(timezone.utc).isoformat()).maybe_single().execute())
    if not existing: raise HTTPException(400)
    if (datetime.now(timezone.utc)-_parse_dt(existing["created_at"]))>timedelta(days=365): raise HTTPException(400)
    new_total = existing["diamonds_committed"]+amount
    new_diamonds = user["diamonds"]-amount
    sb.table("flexer_cards").update({"diamonds_committed":new_total}).eq("card_id",existing["card_id"]).execute()
    sb.table("users").update({"diamonds":new_diamonds}).eq("user_id",user["user_id"]).execute()
    return {"ok":True}

@api_router.get("/flexer/board")
def flexer_board(user:dict=Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    cards = sb.table("flexer_cards").select("*").gt("expires_at",now).order("diamonds_committed",desc=True).limit(100).execute().data or []
    result=[]
    for c in cards:
        profile = _maybe(sb.table("user_profiles").select("display_name,profile_image,date_of_birth,country,city,health_status").eq("user_id",c["user_id"]).maybe_single().execute())
        if profile:
            age=None
            if profile.get("date_of_birth"):
                try:
                    dob=datetime.fromisoformat(str(profile["date_of_birth"])).date()
                    today=datetime.now(timezone.utc).date()
                    age=today.year-dob.year-((today.month,today.day)<(dob.month,dob.day))
                except: pass
            result.append({"card_id":c["card_id"],"user_id":c["user_id"],"display_name":profile.get("display_name","Someone"),"profile_image":profile.get("profile_image",""),"age":age,"country":profile.get("country",""),"city":profile.get("city",""),"health_status":profile.get("health_status"),"diamonds_committed":c["diamonds_committed"],"expires_at":c["expires_at"]})
    return result

# ---------- Countries/Cities ----------
@api_router.get("/location/countries")
def get_countries():
    try:
        resp = httpx.get("https://restcountries.com/v3.1/all?fields=name,cca2", timeout=5)
        if resp.status_code == 200: return [{"code":c["cca2"],"name":c["name"]["common"]} for c in resp.json()]
    except: pass
    return [{"code":"ZA","name":"South Africa"},{"code":"US","name":"United States"},{"code":"GB","name":"United Kingdom"},{"code":"CA","name":"Canada"},{"code":"AU","name":"Australia"},{"code":"IN","name":"India"}]

@api_router.get("/location/cities")
def get_cities(country:str):
    try:
        resp = httpx.post("https://countriesnow.space/api/v0.1/countries/cities", json={"country":country}, timeout=5)
        if resp.status_code==200 and not resp.json().get("error"): return [{"name":c} for c in resp.json().get("data",[])]
    except: pass
    fallback = {"South Africa":["Johannesburg","Cape Town","Durban"],"United States":["New York","Los Angeles","Chicago"]}
    return [{"name":c} for c in fallback.get(country,[])]


# ---------- Admin ----------
@api_router.get("/admin/check")
def admin_check(user: dict = Depends(get_current_user)):
    return {"is_admin": user.get("is_admin", False)}

# ---------- Reports ----------
@api_router.get("/admin/reports")
def admin_get_reports(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    reports = sb.table("user_reports").select("*").order("created_at", desc=True).execute().data or []
    result = []
    for r in reports:
        reporter = _maybe(sb.table("users").select("email,name").eq("user_id", r["reporter_id"]).maybe_single().execute())
        reported = _maybe(sb.table("users").select("email,name").eq("user_id", r["reported_user_id"]).maybe_single().execute())
        result.append({
            "report_id": r["report_id"],
            "reporter_name": reporter.get("name") or reporter.get("email","Unknown") if reporter else "Unknown",
            "reporter_email": reporter.get("email","Unknown") if reporter else "Unknown",
            "reported_name": reported.get("name") or reported.get("email","Unknown") if reported else "Unknown",
            "reported_email": reported.get("email","Unknown") if reported else "Unknown",
            "reported_user_id": r["reported_user_id"],
            "reason": r.get("reason",""),
            "created_at": r["created_at"],
        })
    return result

@api_router.post("/admin/reports/{report_id}/resolve")
def admin_resolve_report(report_id: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    sb.table("user_reports").delete().eq("report_id", report_id).execute()
    return {"ok": True}

# ---------- Ban / Unban ----------
@api_router.post("/admin/ban")
def admin_ban_user(payload: dict, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    target_id = payload.get("user_id")
    reason = payload.get("reason", "")
    duration_days = payload.get("duration_days")  # None = permanent

    if not target_id:
        raise HTTPException(400, "Missing user_id")

    update_data = {"deleted": True, "banned": True, "banned_reason": reason}
    if duration_days:
        update_data["banned_until"] = (datetime.now(timezone.utc) + timedelta(days=duration_days)).isoformat()
    else:
        update_data["banned_until"] = None

    sb.table("users").update(update_data).eq("user_id", target_id).execute()
    sb.table("user_sessions").delete().eq("user_id", target_id).execute()
    return {"ok": True}

@api_router.post("/admin/unban")
def admin_unban_user(payload: dict, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    target_id = payload.get("user_id")
    if not target_id:
        raise HTTPException(400, "Missing user_id")
    sb.table("users").update({"deleted": False, "banned": False, "banned_reason": None, "banned_until": None}).eq("user_id", target_id).execute()
    return {"ok": True}

# ---------- Stories Moderation ----------
@api_router.get("/admin/stories")
def admin_get_stories(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    stories = sb.table("stories").select("*").order("created_at", desc=True).limit(200).execute().data or []
    return stories

@api_router.delete("/admin/stories/{story_id}")
def admin_delete_story(story_id: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    sb.table("story_comments").delete().eq("story_id", story_id).execute()
    sb.table("story_likes").delete().eq("story_id", story_id).execute()
    sb.table("stories").delete().eq("story_id", story_id).execute()
    return {"ok": True}

# ---------- User Lookup ----------
@api_router.get("/admin/users")
def admin_search_users(query: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    # Search by email or name (using display_name or name)
    res = sb.table("users").select("user_id,email,name,verified,premium_tier,deleted,banned,tokens,diamonds,last_active,created_at").or_(f"email.ilike.%{query}%,name.ilike.%{query}%").limit(20).execute()
    users = res.data or []
    # Also search profiles for display_name
    profiles = sb.table("user_profiles").select("user_id,display_name").or_(f"display_name.ilike.%{query}%").limit(20).execute().data or []
    # Merge
    profile_map = {p["user_id"]: p["display_name"] for p in profiles}
    result = []
    for u in users:
        u["display_name"] = profile_map.get(u["user_id"]) or u.get("name","")
        result.append(u)
    return result

# ---------- Stats ----------
@api_router.get("/admin/stats")
def admin_stats(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    total_users = sb.table("users").select("user_id", count="exact").eq("deleted", False).execute().count
    # New today
    today = datetime.now(timezone.utc).date()
    new_today = sb.table("users").select("user_id", count="exact").gte("created_at", today.isoformat()).execute().count
    # Active (last 24h)
    active = sb.table("users").select("user_id", count="exact").gte("last_active", (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()).execute().count
    # Revenue (sum of diamond purchases) – we store amount paid in description but not a numeric column. We'll approximate by counting purchases.
    purchases = sb.table("diamond_purchases").select("item").execute().data or []
    # Estimate revenue: each purchase corresponds to a package value. We'll just return purchase count.
    total_purchases = len(purchases)
    return {
        "total_users": total_users,
        "new_today": new_today,
        "active_last_24h": active,
        "diamond_purchases": total_purchases,
    }

# ---------- Announcement ----------
@api_router.post("/admin/announce")
def admin_announce(payload: dict, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    message = payload.get("message", "")
    if not message:
        raise HTTPException(400, "Message required")

    # Fetch only non‑deleted users (batch them to avoid huge memory usage)
    users = sb.table("users").select("user_id").eq("deleted", False).execute().data or []
    sent_count = 0
    errors = 0
    for u in users:
        try:
            # Use the admin's own user_id as the sender, not "system"
            notify_user(u["user_id"], "announcement", message, user["user_id"])
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to notify {u['user_id']}: {e}")
            errors += 1

    return {"ok": True, "sent_to": sent_count, "errors": errors}
# ---------- Manual Verification ----------
@api_router.post("/admin/verify-user")
def admin_verify_user(payload: dict, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    target_id = payload.get("user_id")
    verified = payload.get("verified", True)
    if not target_id:
        raise HTTPException(400, "Missing user_id")
    sb.table("users").update({"verified": verified}).eq("user_id", target_id).execute()
    return {"ok": True}

# ---------- Image Queue (recently updated profiles) ----------
@api_router.get("/admin/image-queue")
def admin_image_queue(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    # Fetch profiles with images, ordered by updated_at desc
    profiles = sb.table("user_profiles").select("*").not_.is_("profile_image", None).order("updated_at", desc=True).limit(50).execute().data or []
    users = sb.table("users").select("user_id,verified,premium_tier").execute().data or []
    user_map = {u["user_id"]: u for u in users}
    result = []
    for p in profiles:
        u = user_map.get(p["user_id"], {})
        result.append({
            "user_id": p["user_id"],
            "display_name": p.get("display_name") or u.get("display_name") or "Anonymous",
            "profile_image": p.get("profile_image"),
            "gallery_images": p.get("gallery_images") or [],
            "updated_at": p.get("updated_at"),
            "verified": u.get("verified", False),
        })
    return result

# ---------- Admin image deletion ----------
@api_router.delete("/admin/images/{user_id}/{image_type}/{image_index}")
def admin_delete_image(
    user_id: str,
    image_type: str,  # "profile" or "gallery"
    image_index: int,
    admin_user: dict = Depends(get_current_user)
):
    if not admin_user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    if image_type not in ("profile", "gallery"):
        raise HTTPException(400, "Image type must be 'profile' or 'gallery'")

    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user_id).maybe_single().execute())
    if not profile:
        raise HTTPException(404, "User not found")

    if image_type == "profile":
        image_url = profile.get("profile_image")
        if not image_url:
            raise HTTPException(400, "No profile image")
        path = extract_path_from_url(image_url)
        if path:
            try:
                sb.storage.from_(STORAGE_BUCKET).remove([path])
            except Exception as e:
                logger.warning(f"Could not delete profile image {path}: {e}")
        sb.table("user_profiles").update({"profile_image": None}).eq("user_id", user_id).execute()
    else:  # gallery
        gallery = profile.get("gallery_images") or []
        if image_index < 0 or image_index >= len(gallery):
            raise HTTPException(400, "Invalid image index")
        image_url = gallery[image_index]
        path = extract_path_from_url(image_url)
        if path:
            try:
                sb.storage.from_(STORAGE_BUCKET).remove([path])
            except Exception as e:
                logger.warning(f"Could not delete gallery image {path}: {e}")
        del gallery[image_index]
        sb.table("user_profiles").update({"gallery_images": gallery}).eq("user_id", user_id).execute()

    return {"ok": True}

def extract_path_from_url(url: str) -> Optional[str]:
    """Extract bucket path from Supabase storage URL."""
    if not url:
        return None
    prefix = f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/"
    if url.startswith(prefix):
        return url[len(prefix):]
    return None



# ---------- Photo & Story reports ----------
@api_router.post("/report-content")
def report_content(payload: dict, user: dict = Depends(get_current_user)):
    reported_user_id = payload.get("reported_user_id")
    reason = payload.get("reason", "")
    image_index = payload.get("image_index")
    story_id = payload.get("story_id")

    if not reported_user_id or not reason:
        raise HTTPException(400, "Missing reported_user_id or reason")

    # Create the report – only use columns that exist in user_reports
    sb.table("user_reports").insert({
        "reporter_id": user["user_id"],
        "reported_user_id": reported_user_id,
        "reason": f"{reason}" + (f" (image {image_index})" if image_index is not None else "") + (f" (story {story_id})" if story_id else ""),
        "created_at": datetime.utcnow().isoformat()
    }).execute()

    # Notify the reported user
    notify_user(
        reported_user_id,
        "warning",
        f"Your content has been reported for: {reason}. Our team will review it.",
        user["user_id"]
    )

    return {"ok": True}

# ---------- Admin: get detailed reports ----------
@api_router.get("/admin/reports-detailed")
def admin_get_detailed_reports(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")

    reports = sb.table("user_reports").select("*").order("created_at", desc=True).execute().data or []
    result = []
    for r in reports:
        meta = r.get("metadata") or {}
        # fetch the reported user's profile and story/image details
        reported_user = _maybe(sb.table("users").select("email,name,verified,premium_tier").eq("user_id", r["reported_user_id"]).maybe_single().execute())
        reporter = _maybe(sb.table("users").select("email,name").eq("user_id", r["reporter_id"]).maybe_single().execute())

        entry = {
            "report_id": r["report_id"],
            "reporter_name": reporter.get("name") or reporter.get("email","Unknown") if reporter else "Unknown",
            "reporter_email": reporter.get("email","Unknown") if reporter else "Unknown",
            "reported_name": reported_user.get("name") or reported_user.get("email","Unknown") if reported_user else "Unknown",
            "reported_email": reported_user.get("email","Unknown") if reported_user else "Unknown",
            "reported_user_id": r["reported_user_id"],
            "reason": r["reason"],
            "created_at": r["created_at"],
            "type": meta.get("type", "user"),
            "image_index": meta.get("image_index"),
            "story_id": meta.get("story_id"),
        }

        # If story report, fetch story content
        if entry["story_id"]:
            story = _maybe(sb.table("stories").select("*").eq("story_id", entry["story_id"]).maybe_single().execute())
            if story:
                entry["story_content"] = story.get("content","")
                entry["story_author"] = story.get("author_name","")
            else:
                entry["story_content"] = "[deleted]"

        # If photo report, fetch the image URL(s)
        if entry["image_index"] is not None:
            profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", r["reported_user_id"]).maybe_single().execute())
            if profile:
                if entry["image_index"] == 0:
                    entry["image_url"] = profile.get("profile_image")
                else:
                    gallery = profile.get("gallery_images") or []
                    idx = entry["image_index"]
                    if 0 <= idx < len(gallery):
                        entry["image_url"] = gallery[idx]

        result.append(entry)

    return result

# ---------- Admin: delete reported photo ----------
@api_router.delete("/admin/reports/{report_id}/delete-photo")
def admin_delete_reported_photo(report_id: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")

    report = _maybe(sb.table("user_reports").select("*").eq("report_id", report_id).maybe_single().execute())
    if not report:
        raise HTTPException(404, "Report not found")

    meta = report.get("metadata") or {}
    image_index = meta.get("image_index")
    reported_user_id = report["reported_user_id"]

    if image_index is None:
        raise HTTPException(400, "Not a photo report")

    # Delete the image
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", reported_user_id).maybe_single().execute())
    if not profile:
        raise HTTPException(404, "User profile not found")

    if image_index == 0:
        # profile image
        image_url = profile.get("profile_image")
        if image_url:
            path = extract_path_from_url(image_url)
            if path:
                try:
                    sb.storage.from_(STORAGE_BUCKET).remove([path])
                except Exception as e:
                    logger.warning(f"Could not delete profile image {path}: {e}")
        sb.table("user_profiles").update({"profile_image": None}).eq("user_id", reported_user_id).execute()
    else:
        gallery = profile.get("gallery_images") or []
        if 0 <= image_index < len(gallery):
            image_url = gallery[image_index]
            path = extract_path_from_url(image_url)
            if path:
                try:
                    sb.storage.from_(STORAGE_BUCKET).remove([path])
                except Exception as e:
                    logger.warning(f"Could not delete gallery image {path}: {e}")
            del gallery[image_index]
            sb.table("user_profiles").update({"gallery_images": gallery}).eq("user_id", reported_user_id).execute()

    # Send warning notification to the owner
    notify_user(reported_user_id, "warning", f"Your photo was removed for violating our guidelines ({report['reason']}). Repeated violations may lead to account suspension.", user["user_id"])

    # Resolve the report
    sb.table("user_reports").delete().eq("report_id", report_id).execute()
    return {"ok": True}

# ---------- Admin: delete reported story ----------
@api_router.delete("/admin/reports/{report_id}/delete-story")
def admin_delete_reported_story(report_id: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")

    report = _maybe(sb.table("user_reports").select("*").eq("report_id", report_id).maybe_single().execute())
    if not report:
        raise HTTPException(404, "Report not found")

    story_id = (report.get("metadata") or {}).get("story_id")
    if not story_id:
        raise HTTPException(400, "Not a story report")

    # Delete the story
    sb.table("story_comments").delete().eq("story_id", story_id).execute()
    sb.table("story_likes").delete().eq("story_id", story_id).execute()
    sb.table("stories").delete().eq("story_id", story_id).execute()

    # Warn the story author
    notify_user(report["reported_user_id"], "warning", f"Your story was removed for violating our guidelines ({report['reason']}). Repeated violations may lead to account suspension.", user["user_id"])

    # Resolve the report
    sb.table("user_reports").delete().eq("report_id", report_id).execute()
    return {"ok": True}

# ---------- Admin: suspend user ----------
@api_router.post("/admin/suspend")
def admin_suspend_user(payload: dict, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    target_id = payload.get("user_id")
    reason = payload.get("reason", "Violation of guidelines")
    if not target_id:
        raise HTTPException(400, "Missing user_id")
    sb.table("users").update({
        "deleted": True,
        "banned": True,
        "banned_reason": reason
    }).eq("user_id", target_id).execute()
    sb.table("user_sessions").delete().eq("user_id", target_id).execute()
    notify_user(target_id, "warning",
        f"Your account has been suspended for: {reason}. If you believe this is a mistake, please contact support.",
        user["user_id"])
    return {"ok": True}

app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT",8000)))
    
   