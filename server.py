from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Cookie, Header
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import os, logging, uuid, random, math, httpx, io, base64, time, secrets, json
from pathlib import Path
from pydantic import BaseModel, validator
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta
from PIL import Image

# ---------- Enable HEIC/HEIF support for Pillow ----------
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

import bcrypt
import threading
from contextlib import asynccontextmanager
try:
    import redis
except ImportError:
    redis = None

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']

# ---------- HTTP clients ----------
http_client_sync = httpx.Client(http2=False)
_async_client: Optional[httpx.AsyncClient] = None

def get_async_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None:
        _async_client = httpx.AsyncClient(timeout=10.0)
    return _async_client

# ---------- Redis rate limiter (fallback to in‑memory) ----------
_redis_client: Optional[redis.Redis] = None
try:
    _redis_client = redis.from_url(os.environ.get("REDIS_URL", ""))
    _redis_client.ping()
except Exception:
    _redis_client = None

_rate_limit_lock = threading.Lock()
_in_memory_store: Dict[str, list] = {}

def check_rate_limit(key: str, max_req: int = 10, window: int = 60):
    now = time.time()
    if _redis_client:
        current = _redis_client.get(key)
        if current and int(current) >= max_req:
            raise HTTPException(429, "Too many requests. Please slow down.")
        pipe = _redis_client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        pipe.execute()
    else:
        with _rate_limit_lock:
            timestamps = _in_memory_store.get(key, [])
            timestamps = [t for t in timestamps if now - t < window]
            if len(timestamps) >= max_req:
                raise HTTPException(429, "Too many requests. Please slow down.")
            timestamps.append(now)
            _in_memory_store[key] = timestamps

# ==================== LIFESPAN ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    get_async_client()
    _cache_countries()
    ensure_public_chats()
    yield
    if _async_client:
        await _async_client.aclose()
    http_client_sync.close()

app = FastAPI(lifespan=lifespan)

sb: Client = create_client(SUPABASE_URL.rstrip('/'), SUPABASE_KEY)
sb.postgrest.session = http_client_sync

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

# ---------- Constants ----------
EARTH_RADIUS_KM = 6371
MAX_GPS_AGE_HOURS = 24
STORAGE_BUCKET = "avatars"
VERIFY_COST = 10
GOLD_COST = 39
GOLD_DAYS = 30
PREMIUM_COST = 199
PREMIUM_DAYS = 180
MAX_IMAGE_BASE64_SIZE = 5 * 1024 * 1024

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

def spend_diamonds(user_id: str, amount: int, item: str, log_purchase: bool = True):
    for attempt in range(3):
        res = sb.table("users").select("diamonds").eq("user_id", user_id).single().execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="User not found")
        current = res.data["diamonds"]
        if current < amount:
            raise HTTPException(status_code=402, detail=f"Not enough diamonds (have {current}, need {amount})")
        new_balance = current - amount
        upd = sb.table("users").update({"diamonds": new_balance}).eq("user_id", user_id).eq("diamonds", current).execute()
        if upd.data:
            if log_purchase:
                sb.table("diamond_purchases").insert({
                    "purchase_id": f"pur_{uuid.uuid4().hex[:12]}",
                    "user_id": user_id,
                    "item": item,
                    "diamond_cost": amount,
                    "purchased_at": datetime.now(timezone.utc).isoformat()
                }).execute()
            return new_balance
    raise HTTPException(409, "Balance conflict, please retry")

def spend_tokens(user_id: str, amount: int):
    for attempt in range(3):
        res = sb.table("users").select("tokens").eq("user_id", user_id).single().execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="User not found")
        current = res.data["tokens"]
        if current < amount:
            raise HTTPException(status_code=402, detail=f"Not enough tokens (have {current}, need {amount})")
        new_balance = current - amount
        upd = sb.table("users").update({"tokens": new_balance}).eq("user_id", user_id).eq("tokens", current).execute()
        if upd.data:
            return new_balance
    raise HTTPException(409, "Balance conflict, please retry")

def increment_diamonds(user_id: str, amount: int) -> int:
    try:
        result = sb.rpc('increment_diamonds', {'uid': user_id, 'amt': amount}).execute()
        if not result.data:
            raise HTTPException(500, "Failed to update diamonds")
        return result.data
    except Exception as e:
        logger.error(f"increment_diamonds failed: {e}")
        raise HTTPException(500, "Diamond update error")

async def reverse_geocode(lat: float, lon: float) -> tuple:
    try:
        client = get_async_client()
        resp = await client.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": "HavenApp/1.0"}
        )
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
        client = get_async_client()
        resp = await client.get(f"http://ip-api.com/json/{ip}?fields=status,lat,lon,country,city")
        if resp.status_code == 200:
            data = resp.json()
            if data.get('status') == 'success':
                return {'latitude': data.get('lat'), 'longitude': data.get('lon'), 'country': data.get('country'), 'city': data.get('city'), 'source': 'ip'}
    except Exception as e:
        logger.error(f"IP geolocation failed: {e}")
    return None

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

def notify_user(user_id: str, ntype: str, message: str, from_user_id: str = "system"):
    nid = f"notif_{uuid.uuid4().hex[:12]}"
    sb.table("notifications").insert({"notification_id": nid, "user_id": user_id, "from_user_id": from_user_id, "type": ntype, "message": message, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
    try:
        channel = sb.channel(f"user-{user_id}")
        channel.send({"type": "broadcast", "event": "new_notification", "payload": {"type": ntype, "message": message}})
    except Exception as e:
        logger.error(f"Realtime broadcast failed: {e}")

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
    user["tokens"] = spend_tokens(user["user_id"], cost)

# ---------- Client detection ----------
def is_web_client(request: Request) -> bool:
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    web_domains = ["havenpositive.online", "haven-83b20.web.app", "haven-83b20.firebaseapp.com", "localhost:3000"]
    for domain in web_domains:
        if domain in origin or domain in referer:
            return True
    user_agent = request.headers.get("user-agent", "").lower()
    browser_signatures = ["mozilla", "chrome", "safari", "firefox", "edge"]
    if any(sig in user_agent for sig in browser_signatures) and "flutter" not in user_agent and "dart" not in user_agent:
        return True
    return False

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
    pref_max_distance: Optional[int] = 15000; pref_health_status: Optional[str] = ""
    pref_sexual_orientation: Optional[str] = ""
    profile_hidden: Optional[bool] = False
    hide_from_min_age: Optional[int] = None; hide_from_max_age: Optional[int] = None
    hide_from_health_statuses: Optional[str] = ""
    visible_to: Optional[str] = "all"
    lock_all_images: Optional[bool] = False
    phone_number: Optional[str]

    @validator('date_of_birth')
    def validate_age(cls, v):
        try:
            dob = datetime.strptime(v, "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if age < 18:
                raise ValueError("You must be at least 18 years old")
            if age > 100:
                raise ValueError("Age must be 100 or less")
            return v
        except ValueError as e:
            raise ValueError(str(e))

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
    phone_number: Optional[str] = None   # ← ADD THIS LINE
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

class AdminBanPayload(BaseModel):
    user_id: str
    reason: str = ""
    duration_days: Optional[int] = None

class AdminUnbanPayload(BaseModel):
    user_id: str

class AdminAnnouncePayload(BaseModel):
    message: str

class AdminVerifyUserPayload(BaseModel):
    user_id: str
    verified: bool = True

class PublicChatMessagePayload(BaseModel):
    content: str
    chat_type: str
    reply_to_id: Optional[str] = None

# ---------- Auth dependency ----------
def get_current_user(
    request: Request,
    session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "): token = authorization.split(" ", 1)[1]
    if not token: raise HTTPException(status_code=401, detail="Not authenticated")
    res = sb.table("user_sessions").select("user_id,expires_at").eq("session_token", token).maybe_single().execute()
    session = _maybe(res)
    if not session: raise HTTPException(status_code=401, detail="Invalid session")
    if _parse_dt(session["expires_at"]) < datetime.now(timezone.utc): raise HTTPException(status_code=401)
    user = _maybe(sb.table("users").select("user_id,email,name,picture,verified,premium_tier,premium_expires_at,auto_renew,tokens,diamonds,privacy_accepted_at,created_at,last_active,deleted,is_admin").eq("user_id", session["user_id"]).maybe_single().execute())
    if not user: raise HTTPException(status_code=401, detail="User not found")
    if user.get("deleted"):
        raise HTTPException(status_code=401, detail="Account deleted")

    old_tier = user.get("premium_tier")
    if old_tier and not is_premium(user):
        if user.get("auto_renew"):
            cost = GOLD_COST if old_tier == "gold" else PREMIUM_COST
            days = GOLD_DAYS if old_tier == "gold" else PREMIUM_DAYS
            if user["diamonds"] >= cost:
                new_expires = datetime.now(timezone.utc) + timedelta(days=days)
                upd = sb.table("users").update({
                    "diamonds": user["diamonds"] - cost,
                    "premium_expires_at": new_expires.isoformat()
                }).eq("user_id", user["user_id"]).eq("diamonds", user["diamonds"]).execute()
                if upd.data:
                    user["diamonds"] -= cost
                    user["premium_expires_at"] = new_expires.isoformat()
                else:
                    sb.table("users").update({"premium_tier": None, "premium_expires_at": None, "auto_renew": False}).eq("user_id", user["user_id"]).execute()
                    user["premium_tier"] = None
                    user["premium_expires_at"] = None
                    user["auto_renew"] = False
                    notify_user(user["user_id"], "auto_renew_failed", "Auto‑renewal failed – insufficient diamonds. Premium expired.")
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

    last_active = user.get("last_active")
    now = datetime.now(timezone.utc)
    if not last_active or (now - _parse_dt(last_active)) >= timedelta(minutes=5):
        sb.table("users").update({"last_active": now.isoformat()}).eq("user_id", user["user_id"]).execute()

    return user

# ==================== ROOT ====================
@app.get("/")
def root():
    return {"message": "Haven API is running"}

@api_router.get("/")
def api_root():
    return {"message": "Haven API"}

# ==================== AUTH ====================
@api_router.post("/auth/google")
def auth_google(payload: GoogleAuthPayload, request: Request, response: Response):
    check_rate_limit(f"auth_{request.client.host}", max_req=5)
    email, name, picture = payload.email, payload.name, payload.picture
    session_token = secrets.token_hex(32)
    existing = _maybe(sb.table("users").select("*").eq("email", email).eq("deleted", False).maybe_single().execute())
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing:
        user_id = existing["user_id"]
        sb.table("users").update({"name": name, "picture": picture, "last_active": now_iso, "deleted": False}).eq("user_id", user_id).execute()
    else:
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
                "tokens": 40, "diamonds": 5, "verified": False
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
        "created_at": user.get("created_at"),
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

# ==================== LOOKUP ====================
@api_router.get("/lookup/ethnicities")
def get_ethnicities():
    return ETHNICITY_LIST

# ==================== STATS ====================
@api_router.get("/stats/online")
def online_users():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)
    res = sb.table("users").select("user_id", count="exact").gte("last_active", cutoff.isoformat()).execute()
    count = res.count if hasattr(res, 'count') else 0
    return {"online": count}

# ==================== ECONOMY / PURCHASES ====================
@api_router.post("/purchase/verify")
def purchase_verify(user: dict = Depends(get_current_user)):
    if user.get("verified"): raise HTTPException(400, "Already verified")
    new_diamonds = spend_diamonds(user["user_id"], VERIFY_COST, "verify", log_purchase=True)
    sb.table("users").update({"verified": True}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "verified": True, "diamonds": new_diamonds}

@api_router.post("/purchase/premium")
def purchase_premium(tier: str = "gold", user: dict = Depends(get_current_user)):
    if not user.get("verified"):
        raise HTTPException(400, "You must be verified before purchasing premium.")
    if is_premium(user):
        raise HTTPException(400, "You already have an active premium subscription.")
    if tier not in ["gold", "platinum"]:
        raise HTTPException(400, "Invalid tier")
    cost = GOLD_COST if tier == "gold" else PREMIUM_COST
    days = GOLD_DAYS if tier == "gold" else PREMIUM_DAYS
    new_diamonds = spend_diamonds(user["user_id"], cost, f"{tier}_premium", log_purchase=True)
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    sb.table("users").update({
        "premium_tier": tier,
        "premium_expires_at": expires.isoformat()
    }).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "premium_tier": tier, "diamonds": new_diamonds, "expires_at": expires.isoformat()}

@api_router.put("/premium/auto-renew")
def toggle_auto_renew(user: dict = Depends(get_current_user)):
    current = user.get("auto_renew", False)
    new_val = not current
    sb.table("users").update({"auto_renew": new_val}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "auto_renew": new_val}

PAYPAL_API_BASE = "https://api-m.paypal.com"

@api_router.post("/diamonds/create-order")
async def create_diamond_order(package_id: str, request: Request, user: dict = Depends(get_current_user)):
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
    client = get_async_client()
    auth_response = await client.post(
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
            "amount": {"currency_code": "USD", "value": str(pkg["amount"])},
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
    order_response = await client.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        json=order_data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
    )
    if order_response.status_code != 201:
        raise HTTPException(500, "PayPal order creation failed")
    return order_response.json()

@api_router.post("/diamonds/capture-order")
async def capture_diamond_order(order_id: str, package_id: str, user: dict = Depends(get_current_user)):
    already = sb.table("diamond_purchases").select("purchase_id").eq("paypal_order_id", order_id).maybe_single().execute()
    if already.data:
        raise HTTPException(409, "Order already processed")
    PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(500, "PayPal credentials not configured")
    client = get_async_client()
    auth_response = await client.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        headers={"Accept": "application/json"},
    )
    if auth_response.status_code != 200:
        raise HTTPException(500, "PayPal authentication failed")
    access_token = auth_response.json()["access_token"]
    capture_response = await client.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
    )
    if capture_response.status_code not in (200, 201):
        raise HTTPException(500, "Payment capture failed")
    capture_data = capture_response.json()
    try:
        custom_id = capture_data["purchase_units"][0]["payments"]["captures"][0]["custom_id"]
        _, real_package_id = custom_id.split(":", 1)
    except (KeyError, ValueError):
        raise HTTPException(400, "Invalid PayPal capture data")
    packages = {"52": 52, "120": 120, "310": 310, "770": 770}
    diamonds = packages.get(real_package_id)
    if not diamonds:
        raise HTTPException(400, "Invalid package")
    purchase_record = {
        "purchase_id": f"diam_{uuid.uuid4().hex[:12]}",
        "user_id": user["user_id"],
        "item": f"{diamonds}_diamonds_paypal",
        "diamond_cost": 0,
        "purchased_at": datetime.now(timezone.utc).isoformat(),
        "paypal_order_id": order_id
    }
    sb.table("diamond_purchases").insert(purchase_record).execute()
    new_diamonds = increment_diamonds(user["user_id"], diamonds)
    return {"ok": True, "diamonds": new_diamonds}

@api_router.post("/purchase/google-play")
def verify_google_purchase(payload: dict, user: dict = Depends(get_current_user)):
    product_id = payload.get("product_id")
    purchase_token = payload.get("purchase_token")
    if not product_id or not purchase_token:
        raise HTTPException(400, "Missing product_id or purchase_token")
    already = sb.table("diamond_purchases").select("purchase_id").eq("google_purchase_token", purchase_token).maybe_single().execute()
    if already.data:
        raise HTTPException(409, "Purchase already processed")
    diamond_map = {"diamonds_52": 52, "diamonds_120": 120, "diamonds_310": 310, "diamonds_770": 770}
    diamonds = diamond_map.get(product_id)
    if not diamonds:
        raise HTTPException(400, "Invalid product_id")
    try:
        creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not creds_json:
            raise HTTPException(500, "Service account not configured")
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        service = build("androidpublisher", "v3", credentials=credentials)
        result = service.purchases().products().get(
            packageName="date.perfecthorse.havenpositive",
            productId=product_id,
            token=purchase_token,
        ).execute()
        if result.get("purchaseState") == 0:
            service.purchases().products().acknowledge(
                packageName="date.perfecthorse.havenpositive",
                productId=product_id,
                token=purchase_token,
                body={}
            ).execute()
            sb.table("diamond_purchases").insert({
                "purchase_id": f"diam_{uuid.uuid4().hex[:12]}",
                "user_id": user["user_id"],
                "item": f"{diamonds}_diamonds_google",
                "diamond_cost": 0,
                "purchased_at": datetime.now(timezone.utc).isoformat(),
                "google_purchase_token": purchase_token
            }).execute()
            new_diamonds = increment_diamonds(user["user_id"], diamonds)
            return {"ok": True, "diamonds": new_diamonds}
        else:
            raise HTTPException(400, "Purchase not valid or already consumed")
    except Exception as e:
        logger.exception("Google Play verification failed")
        raise HTTPException(400, f"Purchase verification failed: {str(e)}")

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

@api_router.post("/earn-tokens-rewarded")
def earn_tokens_rewarded(user: dict = Depends(get_current_user)):
    if is_premium(user):
        raise HTTPException(400, "Premium members don't earn tokens")
    last_earn = user.get("last_ad_token_earned")
    if last_earn:
        last = _parse_dt(last_earn)
        if datetime.now(timezone.utc) - last < timedelta(seconds=30):
            raise HTTPException(400, "You can earn ad tokens again in 30 seconds")
    new_tokens = user.get("tokens", 0) + 7
    sb.table("users").update({
        "tokens": new_tokens,
        "last_ad_token_earned": datetime.now(timezone.utc).isoformat()
    }).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "tokens_awarded": 7, "total_tokens": new_tokens}

@api_router.post("/spend-tokens")
def spend_tokens_endpoint(amount: int = 1, user: dict = Depends(get_current_user)):
    if is_premium(user):
        return {"ok": True, "spent": 0, "remaining_tokens": user.get("tokens", 0)}
    if amount <= 0:
        return {"ok": True, "spent": 0, "remaining_tokens": user.get("tokens", 0)}
    try:
        new_balance = spend_tokens(user["user_id"], amount)
        user["tokens"] = new_balance
        return {"ok": True, "spent": amount, "remaining_tokens": new_balance}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token spending failed: {e}")
        raise HTTPException(500, "Failed to spend tokens")

# ---------- Location ----------
@api_router.post("/location/update")
async def update_location(payload: LocationUpdatePayload, user: dict = Depends(get_current_user)):
    if not (-90 <= payload.latitude <= 90) or not (-180 <= payload.longitude <= 180):
        raise HTTPException(400, "Invalid coordinates")
    if payload.accuracy and payload.accuracy > 500:
        raise HTTPException(400, "Location accuracy too low")
    now = datetime.now(timezone.utc)
    country, city = await reverse_geocode(payload.latitude, payload.longitude)
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
            "pref_country": "", "pref_max_distance": 15000, "pref_health_status": "",
            "profile_hidden": False, "hide_from_min_age": None, "hide_from_max_age": None,
            "hide_from_health_statuses": "",
            "gps_latitude": None, "gps_longitude": None, "gps_verified_at": None,
            "location_source": "none",
            "last_active": user.get("last_active"),
            "verified": False, "premium_tier": None,
            "phone_number": "",   # ← ADD THIS LINE
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
        "pref_max_distance": profile.get("pref_max_distance", 15000),
        "pref_health_status": profile.get("pref_health_status",""),
        "pref_sexual_orientation": profile.get("pref_sexual_orientation",""),
        "profile_hidden": profile.get("profile_hidden", False),
        "hide_from_min_age": profile.get("hide_from_min_age"),
        "hide_from_max_age": profile.get("hide_from_max_age"),
        "hide_from_health_statuses": profile.get("hide_from_health_statuses",""),
        "phone_number": profile.get("phone_number", ""),   # ← ADD THIS LINE
        "visible_to": profile.get("visible_to", "all"),
        "lock_all_images": profile.get("lock_all_images", False),
        "gps_latitude": lat, "gps_longitude": lon,
        "gps_verified_at": profile.get("gps_verified_at"),
        "location_source": profile.get("location_source", "none"),
        "last_active": user.get("last_active"),
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

    uploaded_urls = []
    try:
        profile_image = process_image_field(payload.profile_image, user["user_id"], "profile")
        if profile_image and profile_image != payload.profile_image:
            uploaded_urls.append(profile_image)
    except Exception:
        raise HTTPException(400, "Failed to upload profile image")
    gallery = []
    for i, img in enumerate(payload.gallery_images or []):
        try:
            url = process_image_field(img, user["user_id"], f"gallery_{i}")
            if url != img:
                uploaded_urls.append(url)
            gallery.append(url)
        except Exception:
            for url in uploaded_urls:
                try:
                    path = extract_path_from_url(url)
                    if path:
                        sb.storage.from_(STORAGE_BUCKET).remove([path])
                except Exception:
                    pass
            raise HTTPException(400, "Failed to upload gallery image")

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
        "pref_max_distance": payload.pref_max_distance or 15000,
        "pref_health_status": payload.pref_health_status or "",
        "pref_sexual_orientation": payload.pref_sexual_orientation or "",
        "phone_number": payload.phone_number or "",   # ← ADD THIS LINE
        "profile_hidden": payload.profile_hidden if user.get("verified") else False,
        "hide_from_min_age": payload.hide_from_min_age if user.get("verified") else None,
        "hide_from_max_age": payload.hide_from_max_age if user.get("verified") else None,
        "hide_from_health_statuses": payload.hide_from_health_statuses if user.get("verified") else "",
        "visible_to": payload.visible_to if user.get("verified") else "all",
        "lock_all_images": payload.lock_all_images if user.get("verified") else False,
        "onboarding_complete": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
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
    except Exception:
        for url in uploaded_urls:
            try:
                path = extract_path_from_url(url)
                if path:
                    sb.storage.from_(STORAGE_BUCKET).remove([path])
            except Exception:
                pass
        raise HTTPException(500, "Failed to save profile")
    return {"ok": True, "profile": get_profile(user)}

@api_router.put("/profile")
def update_profile(payload: ProfileUpdatePayload, user: dict = Depends(get_current_user)):
    updates = {}
    uploaded_urls = []
    if payload.gallery_images is not None:
        if not is_premium(user) and len(payload.gallery_images) > 5:
            raise HTTPException(400, "Free users can only upload up to 5 images")
        new_gallery = []
        for i, img in enumerate(payload.gallery_images):
            try:
                url = process_image_field(img, user["user_id"], f"gallery_{i}")
                if url != img:
                    uploaded_urls.append(url)
                new_gallery.append(url)
            except Exception:
                for url in uploaded_urls:
                    try:
                        path = extract_path_from_url(url)
                        if path:
                            sb.storage.from_(STORAGE_BUCKET).remove([path])
                    except Exception:
                        pass
                raise HTTPException(400, "Failed to upload gallery image")
        updates["gallery_images"] = new_gallery
    if payload.profile_image is not None:
        try:
            url = process_image_field(payload.profile_image, user["user_id"], "profile")
            if url != payload.profile_image:
                uploaded_urls.append(url)
            updates["profile_image"] = url
        except Exception:
            for url in uploaded_urls:
                try:
                    path = extract_path_from_url(url)
                    if path:
                        sb.storage.from_(STORAGE_BUCKET).remove([path])
                except Exception:
                    pass
            raise HTTPException(400, "Failed to upload profile image")

    all_fields = [
        "date_of_birth", "gender", "health_status",
        "sexual_orientation", "positive_since", "height", "ethnicity", "religion",
        "display_name", "bio", "interests", "looking_for",
        "education", "kids", "want_kids", "smoke", "drink", "employment",
        "pref_gender", "pref_min_age", "pref_max_age", "pref_country",
        "pref_max_distance", "pref_health_status", "pref_sexual_orientation",
        "hide_from_min_age", "hide_from_max_age", "hide_from_health_statuses",
        "visible_to", "lock_all_images",
        "phone_number",   # ← ADD THIS LINE
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
            if field == "date_of_birth" and value is not None:
                try:
                    dob = datetime.strptime(value, "%Y-%m-%d").date()
                    today = datetime.now(timezone.utc).date()
                    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
                    if age < 18:
                        raise HTTPException(400, "You must be at least 18 years old")
                    if age > 100:
                        raise HTTPException(400, "Age must be 100 or less")
                except ValueError:
                    raise HTTPException(400, "Invalid date format")
            updates[field] = value
    if payload.profile_hidden is not None:
        updates["profile_hidden"] = payload.profile_hidden if is_premium(user) else False
    if not updates:
        return {"ok": True, "profile": get_profile(user)}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", user["user_id"]).maybe_single().execute())
    try:
        if existing:
            sb.table("user_profiles").update(updates).eq("user_id", user["user_id"]).execute()
        else:
            updates["user_id"] = user["user_id"]; updates["onboarding_complete"] = False
            updates["created_at"] = datetime.now(timezone.utc).isoformat()
            sb.table("user_profiles").insert(updates).execute()
    except Exception:
        for url in uploaded_urls:
            try:
                path = extract_path_from_url(url)
                if path:
                    sb.storage.from_(STORAGE_BUCKET).remove([path])
            except Exception:
                pass
        raise HTTPException(500, "Failed to update profile")
    return {"ok": True, "profile": get_profile(user)}

@api_router.get("/profile")
def get_my_profile(user: dict = Depends(get_current_user)):
    return get_profile(user)

# ---------- Discovery (WEB CLIENTS PAY TOKENS, APP IS FREE) ----------
@api_router.get("/discover/profiles")
def get_discover_profiles(
    request: Request,
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
    pref_max_distance = max_distance if max_distance is not None else viewer_profile.get("pref_max_distance", 15000)
    pref_sexual_orientation = sexual_orientation if sexual_orientation is not None else viewer_profile.get("pref_sexual_orientation", "")

    today = datetime.now(timezone.utc).date()
    min_birth_date = today.replace(year=today.year - pref_max_age)
    max_birth_date = today.replace(year=today.year - pref_min_age)

    matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").execute()
    matched_ids = set()
    for m in (matches.data or []):
        partner = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        matched_ids.add(partner)

    query = sb.table("user_profiles").select(
        "user_id,display_name,date_of_birth,profile_image,gallery_images,gender,country,city,gps_latitude,gps_longitude,health_status,sexual_orientation,religion,ethnicity,height,bio,interests,looking_for,education,kids,want_kids,smoke,drink,employment,profile_hidden,visible_to,lock_all_images,pref_gender,pref_health_status,pref_sexual_orientation,pref_country,pref_max_distance,pref_min_age,pref_max_age,hide_from_min_age,hide_from_max_age,hide_from_health_statuses,onboarding_complete,location_source,gps_verified_at,latitude,longitude,updated_at",
        count="exact"
    ).neq("user_id", user["user_id"]) \
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

    all_profiles = query.execute().data or []
    
    if not all_profiles:
        return []

    user_ids = [p["user_id"] for p in all_profiles]
    users_data = sb.table("users").select("user_id,verified,premium_tier,last_active").in_("user_id", user_ids).execute().data or []
    user_status = {u["user_id"]: u for u in users_data}

    filtered = []
    for p in all_profiles:
        if p.get("profile_hidden"): continue
        p_lat = p.get("gps_latitude")
        p_lon = p.get("gps_longitude")
        distance = None
        if p_lat is not None and p_lon is not None:
            distance = haversine(my_lat, my_lon, p_lat, p_lon)
            if pref_max_distance and distance > pref_max_distance: continue
        p["distance_km"] = round(distance, 1) if distance is not None else None
        status = user_status.get(p["user_id"], {})
        p["verified"] = status.get("verified", False)
        p["premium_tier"] = status.get("premium_tier")
        p["last_active"] = status.get("last_active")
        filtered.append(p)

    filtered.sort(key=lambda x: x.get("distance_km") or float('inf'))

    if page is not None and limit is not None:
        start = (page - 1) * limit
        end = start + limit
        filtered = filtered[start:end]

    # Conditionally charge tokens only for web clients
    if not is_premium(user) and is_web_client(request):
        require_token(user, len(filtered))

    return filtered

@api_router.post("/discover/swipe")
def swipe_profile(payload: SwipePayload, request: Request, user: dict = Depends(get_current_user)):
    # Conditionally charge tokens only for web clients
    if not is_premium(user) and is_web_client(request):
        require_token(user, 1)
    
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

# ---------- Matches / Messages ----------
@api_router.get("/discover/matches")
def get_matches(swipe_type: Optional[str] = 'dating', user: dict = Depends(get_current_user)):
    matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").eq("swipe_type", swipe_type).order("created_at", desc=True).execute()
    if not matches.data:
        return []
    partner_ids = []
    for m in matches.data:
        partner_id = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        partner_ids.append(partner_id)
    profiles = {}
    if partner_ids:
        prof_data = sb.table("user_profiles").select("user_id,display_name,profile_image,bio,country,city,health_status").in_("user_id", list(set(partner_ids))).execute().data or []
        profiles = {p["user_id"]: p for p in prof_data}
    match_ids = [m["match_id"] for m in matches.data]
    unread_map = {}
    if match_ids:
        unread_res = sb.table("match_messages").select("match_id,read,sender_id").in_("match_id", match_ids).eq("read", False).neq("sender_id", user["user_id"]).execute().data or []
        for msg in unread_res:
            unread_map[msg["match_id"]] = unread_map.get(msg["match_id"], 0) + 1
    result = []
    for m in matches.data:
        partner_id = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        prof = profiles.get(partner_id, {})
        result.append({
            "match_id": m["match_id"], "user_id": partner_id,
            "display_name": prof.get("display_name",""),
            "profile_image": prof.get("profile_image",""),
            "bio": prof.get("bio",""), "country": prof.get("country",""), "city": prof.get("city",""),
            "health_status": prof.get("health_status"),
            "created_at": m["created_at"],
            "unread_count": unread_map.get(m["match_id"], 0),
        })
    return result

@api_router.get("/discover/matches/{match_id}/messages")
def get_match_messages(match_id: str, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(404)
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(403)
    msgs = sb.table("match_messages").select("*").eq("match_id", match_id).order("created_at").execute().data or []
    other_id = match["user2_id"] if match["user1_id"] == user["user_id"] else match["user1_id"]
    sb.table("match_messages").update({"read": True}).eq("match_id", match_id).eq("sender_id", other_id).eq("read", False).execute()
    return msgs

@api_router.post("/discover/matches/{match_id}/messages")
def send_match_message(match_id: str, payload: MatchMessagePayload, user: dict = Depends(get_current_user)):
    if contains_profanity(payload.content):
        raise HTTPException(400, "Message contains inappropriate language")
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
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(403)
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
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(403)
    partner_id = match["user2_id"] if match["user1_id"] == user["user_id"] else match["user1_id"]
    return get_profile({"user_id": partner_id})

# ---------- Requests ----------
@api_router.get("/requests")
def get_requests(user: dict = Depends(get_current_user)):
    dating = sb.table("dating_requests").select("*").eq("to_user_id", user["user_id"]).eq("status", "pending").execute().data or []
    friend = sb.table("friend_requests").select("*").eq("to_user_id", user["user_id"]).eq("status", "pending").execute().data or []
    all_requests = []
    for req in dating:
        all_requests.append({"request_id": req["request_id"], "type": "dating", "from_user_id": req["from_user_id"], "created_at": req["created_at"], "status": req["status"]})
    for req in friend:
        all_requests.append({"request_id": req["request_id"], "type": "friend", "from_user_id": req["from_user_id"], "created_at": req["created_at"], "status": req["status"]})
    user_ids = list({r["from_user_id"] for r in all_requests})
    profiles = {}
    if user_ids:
        prof_data = sb.table("user_profiles").select("user_id,display_name,profile_image,country").in_("user_id", user_ids).execute().data or []
        profiles = {p["user_id"]: p for p in prof_data}
    result = []
    for r in all_requests:
        prof = profiles.get(r["from_user_id"])
        result.append({
            "request_id": r["request_id"], "type": r["type"],
            "from_user_id": r["from_user_id"],
            "from_name": prof.get("display_name","Someone") if prof else "Someone",
            "from_image": prof.get("profile_image","") if prof else "",
            "from_country": prof.get("country","") if prof else "",
            "created_at": r["created_at"], "status": r["status"],
        })
    return result

@api_router.post("/requests/{request_id}/respond")
def respond_request(request_id: str, action: str, user: dict = Depends(get_current_user)):
    if action not in ["accept","reject"]: raise HTTPException(400)
    req = _maybe(sb.table("dating_requests").select("*").eq("request_id", request_id).eq("to_user_id", user["user_id"]).maybe_single().execute())
    table = "dating_requests"
    if not req:
        req = _maybe(sb.table("friend_requests").select("*").eq("request_id", request_id).eq("to_user_id", user["user_id"]).maybe_single().execute())
        table = "friend_requests"
    if not req: raise HTTPException(404)
    new_status = "accepted" if action == "accept" else "rejected"
    if req["status"] != "pending": raise HTTPException(400)
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

# ---------- Notifications ----------
@api_router.get("/notifications")
def get_notifications(user: dict = Depends(get_current_user)):
    notifs = sb.table("notifications").select("*").eq("user_id", user["user_id"]).order("created_at", desc=True).limit(50).execute().data or []
    if not notifs:
        return notifs
    user_ids = list({n["from_user_id"] for n in notifs if n["from_user_id"] != "system"})
    profiles = {}
    if user_ids:
        prof_data = sb.table("user_profiles").select("user_id,display_name,profile_image").in_("user_id", user_ids).execute().data or []
        profiles = {p["user_id"]: p for p in prof_data}
    for n in notifs:
        if n["from_user_id"] == "system":
            n["from_name"] = "System"
            n["from_image"] = ""
        else:
            prof = profiles.get(n["from_user_id"])
            n["from_name"] = prof.get("display_name","Someone") if prof else "Someone"
            n["from_image"] = prof.get("profile_image","") if prof else ""
    return notifs

@api_router.post("/notifications/read")
def mark_notifications_read(user: dict = Depends(get_current_user)):
    sb.table("notifications").update({"read": True}).eq("user_id", user["user_id"]).eq("read", False).execute()
    return {"ok": True}

@api_router.get("/notifications/unread-count")
def get_unread_notification_count(user: dict = Depends(get_current_user)):
    res = sb.table("notifications") \
        .select("notification_id", count="exact") \
        .eq("user_id", user["user_id"]) \
        .eq("read", False) \
        .execute()
    count = res.count if hasattr(res, 'count') else 0
    return {"unread": count}

@api_router.get("/unread-counts")
def get_unread_counts(user: dict = Depends(get_current_user)):
    unread_msgs = sb.table("match_messages").select("match_id").neq("sender_id", user["user_id"]).eq("read", False).execute().data or []
    match_ids = list({m["match_id"] for m in unread_msgs})
    dating_count = 0
    friend_count = 0
    if match_ids:
        matches_data = sb.table("profile_matches").select("match_id,swipe_type").in_("match_id", match_ids).execute().data or []
        for match in matches_data:
            if match.get("swipe_type") == "friendship":
                friend_count += 1
            else:
                dating_count += 1
    return {"dating_unread": dating_count, "friend_unread": friend_count}

@api_router.post("/fcm/register")
def register_fcm_token(payload: dict, user: dict = Depends(get_current_user)):
    token = payload.get("token")
    if not token:
        raise HTTPException(400, "Token required")
    sb.table("users").update({"fcm_token": token}).eq("user_id", user["user_id"]).execute()
    return {"ok": True}

# ---------- Report / Block ----------
@api_router.post("/report")
def report_user(payload: ReportPayload, user: dict = Depends(get_current_user)):
    existing = _maybe(sb.table("user_reports").select("report_id").eq("reporter_id", user["user_id"]).eq("reported_user_id", payload.reported_user_id).maybe_single().execute())
    if existing: raise HTTPException(400, "Already reported")
    sb.table("user_reports").insert({"report_id": f"rep_{uuid.uuid4().hex[:12]}", "reporter_id": user["user_id"], "reported_user_id": payload.reported_user_id, "reason": payload.reason or "", "created_at": datetime.now(timezone.utc).isoformat()}).execute()
    return {"ok": True}

@api_router.post("/block")
def block_user(payload: BlockPayload, user: dict = Depends(get_current_user)):
    existing = _maybe(sb.table("user_blocks").select("block_id").eq("blocker_id", user["user_id"]).eq("blocked_user_id", payload.blocked_user_id).maybe_single().execute())
    if existing: raise HTTPException(400, "Already blocked")
    sb.table("user_blocks").insert({"block_id": f"blk_{uuid.uuid4().hex[:12]}", "blocker_id": user["user_id"], "blocked_user_id": payload.blocked_user_id, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
    sb.table("profile_matches").delete().or_(f"and(user1_id.eq.{user['user_id']},user2_id.eq.{payload.blocked_user_id}),and(user1_id.eq.{payload.blocked_user_id},user2_id.eq.{user['user_id']})").execute()
    return {"ok": True}

# ---------- Stories ----------
@api_router.post("/stories")
def create_story(payload: CreateStoryPayload, user: dict = Depends(get_current_user)):
    if not user.get("verified"):
        raise HTTPException(403, "Only verified users can post stories")
    if contains_profanity(payload.content): raise HTTPException(400)
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
    story_ids = [s["story_id"] for s in stories]
    liked_map = {}
    if story_ids:
        likes = sb.table("story_likes").select("story_id").eq("user_id", user["user_id"]).in_("story_id", story_ids).execute().data or []
        liked_map = {l["story_id"]: True for l in likes}
    for s in stories:
        s["liked_by_user"] = liked_map.get(s["story_id"], False)
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
    if contains_profanity(payload.content): raise HTTPException(400)
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

# ---------- Random Chat ----------
@api_router.post("/chat/start")
def chat_start(request: Request, user: dict = Depends(get_current_user)):
    # Conditionally charge tokens only for web clients
    if not is_premium(user) and is_web_client(request):
        require_token(user, 1)
    return {"ok": True}

# ---------- Flexer Board ----------
@api_router.post("/flexer/join")
def flexer_join(amount: int = 6, user: dict = Depends(get_current_user)):
    if amount < 6: raise HTTPException(400, "Minimum 6 diamonds")
    now = datetime.now(timezone.utc)
    existing = _maybe(sb.table("flexer_cards").select("*").eq("user_id", user["user_id"]).gt("expires_at", now.isoformat()).maybe_single().execute())
    if existing:
        new_total = existing["diamonds_committed"] + amount
        new_expiry = max(_parse_dt(existing["expires_at"]), now) + timedelta(days=30)
        new_diamonds = spend_diamonds(user["user_id"], amount, "flexer_join", log_purchase=False)
        sb.table("flexer_cards").update({"diamonds_committed": new_total, "expires_at": new_expiry.isoformat(), "last_renewed_at": now.isoformat()}).eq("card_id", existing["card_id"]).execute()
    else:
        new_diamonds = spend_diamonds(user["user_id"], amount, "flexer_join", log_purchase=False)
        card_id = f"flex_{uuid.uuid4().hex[:12]}"
        expires = now + timedelta(days=30)
        sb.table("flexer_cards").insert({"card_id": card_id, "user_id": user["user_id"], "diamonds_committed": amount, "created_at": now.isoformat(), "expires_at": expires.isoformat(), "last_renewed_at": now.isoformat()}).execute()
    return {"ok": True}

@api_router.post("/flexer/increment")
def flexer_increment(amount: int, user: dict = Depends(get_current_user)):
    if amount < 1:
        raise HTTPException(400)
    existing = _maybe(sb.table("flexer_cards").select("*")
        .eq("user_id", user["user_id"])
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .maybe_single().execute())
    if not existing:
        raise HTTPException(400, "No active flexer card")
    if (datetime.now(timezone.utc) - _parse_dt(existing["created_at"])) > timedelta(days=365):
        raise HTTPException(400, "Card too old to increment")
    spend_diamonds(user["user_id"], amount, "flexer_increment", log_purchase=False)
    new_total = existing["diamonds_committed"] + amount
    sb.table("flexer_cards").update({"diamonds_committed": new_total}).eq("card_id", existing["card_id"]).execute()
    return {"ok": True}

@api_router.get("/flexer/board")
def flexer_board(user:dict=Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    cards = sb.table("flexer_cards").select("*").gt("expires_at",now).order("diamonds_committed",desc=True).limit(100).execute().data or []
    if not cards:
        return []
    user_ids = [c["user_id"] for c in cards]
    profiles = {}
    if user_ids:
        prof_data = sb.table("user_profiles").select("user_id,display_name,profile_image,date_of_birth,country,city,health_status").in_("user_id", user_ids).execute().data or []
        profiles = {p["user_id"]: p for p in prof_data}
    result=[]
    for c in cards:
        profile = profiles.get(c["user_id"])
        if profile:
            age=None
            if profile.get("date_of_birth"):
                try:
                    dob=datetime.fromisoformat(str(profile["date_of_birth"])).date()
                    today=datetime.now(timezone.utc).date()
                    age=today.year-dob.year-((today.month,today.day)<(dob.month,dob.day))
                except: pass
            result.append({
                "card_id":c["card_id"],"user_id":c["user_id"],
                "display_name":profile.get("display_name","Someone"),
                "profile_image":profile.get("profile_image",""),
                "age":age,"country":profile.get("country",""),
                "city":profile.get("city",""),"health_status":profile.get("health_status"),
                "diamonds_committed":c["diamonds_committed"],
                "expires_at":c["expires_at"]
            })
    return result

# ---------- Countries/Cities ----------
_countries_cache: Optional[List[dict]] = None

def _cache_countries():
    global _countries_cache
    try:
        resp = httpx.get("https://restcountries.com/v3.1/all?fields=name,cca2", timeout=5)
        if resp.status_code == 200:
            _countries_cache = [{"code": c["cca2"], "name": c["name"]["common"]} for c in resp.json()]
    except Exception:
        _countries_cache = [{"code":"ZA","name":"South Africa"},{"code":"US","name":"United States"},{"code":"GB","name":"United Kingdom"},{"code":"CA","name":"Canada"},{"code":"AU","name":"Australia"},{"code":"IN","name":"India"}]

_cities_cache: Dict[str, tuple[Optional[List[dict]], float]] = {}
_cities_cache_lock = threading.Lock()
_CITIES_TTL = 3600

@api_router.get("/location/countries")
def get_countries():
    if _countries_cache is None:
        _cache_countries()
    return _countries_cache or []

@api_router.get("/location/cities")
def get_cities(country:str):
    now = time.time()
    with _cities_cache_lock:
        cached = _cities_cache.get(country)
        if cached and (now - cached[1]) < _CITIES_TTL:
            return cached[0] or []
    try:
        resp = httpx.post("https://countriesnow.space/api/v0.1/countries/cities", json={"country":country}, timeout=5)
        if resp.status_code==200 and not resp.json().get("error"):
            data = [{"name":c} for c in resp.json().get("data",[])]
            with _cities_cache_lock:
                _cities_cache[country] = (data, now)
            return data
    except: pass
    fallback = {"South Africa":["Johannesburg","Cape Town","Durban"],"United States":["New York","Los Angeles","Chicago"]}
    data = [{"name":c} for c in fallback.get(country,[])]
    with _cities_cache_lock:
        _cities_cache[country] = (data, now)
    return data

# ==================== GROUPS ====================
@api_router.post("/groups")
def create_group(payload: dict, user: dict = Depends(get_current_user)):
    if not is_premium(user):
        raise HTTPException(400, "Only premium users can create groups")
    title = payload.get("title")
    if not title or len(title.strip()) == 0:
        raise HTTPException(400, "Title required")
    if len(title) > 100:
        raise HTTPException(400, "Title too long")
    description = payload.get("description", "")
    rules = payload.get("rules", "")
    image = payload.get("image", "")
    join_cost = int(payload.get("join_cost", 0))
    if join_cost < 0: raise HTTPException(400)
    spend_diamonds(user["user_id"], 10, "group_creation", log_purchase=True)
    if image and (image.startswith("data:image") or "base64" in image):
        compressed = compress_image(image)
        filename = f"group_{uuid.uuid4().hex[:8]}.webp"
        image_url = upload_image_to_supabase(compressed, user["user_id"], filename)
    else:
        image_url = image or ""
    group_id = f"grp_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    sb.table("groups").insert({
        "group_id": group_id, "creator_id": user["user_id"],
        "title": title.strip(), "description": description,
        "rules": rules, "image": image_url, "join_cost": join_cost,
        "created_at": now, "updated_at": now
    }).execute()
    sb.table("group_members").insert({
        "group_id": group_id, "user_id": user["user_id"],
        "role": "creator", "joined_at": now
    }).execute()
    return {"ok": True, "group_id": group_id}

@api_router.get("/groups")
def list_groups(user: dict = Depends(get_current_user)):
    groups = sb.table("groups").select("*").execute().data or []
    group_ids = [g["group_id"] for g in groups]
    member_counts = {}
    if group_ids:
        members_data = sb.table("group_members").select("group_id,user_id").in_("group_id", group_ids).execute().data or []
        for m in members_data:
            member_counts[m["group_id"]] = member_counts.get(m["group_id"], 0) + 1
    for g in groups:
        g["member_count"] = member_counts.get(g["group_id"], 0)
    groups.sort(key=lambda x: x["member_count"], reverse=True)
    if group_ids:
        my_memberships = sb.table("group_members").select("group_id,role").eq("user_id", user["user_id"]).in_("group_id", group_ids).execute().data or []
        my_roles = {m["group_id"]: m["role"] for m in my_memberships}
    else:
        my_roles = {}
    for g in groups:
        g["my_role"] = my_roles.get(g["group_id"])
    return groups

@api_router.get("/groups/{group_id}")
def get_group(group_id: str, user: dict = Depends(get_current_user)):
    group = _maybe(sb.table("groups").select("*").eq("group_id", group_id).maybe_single().execute())
    if not group: raise HTTPException(404)
    members = sb.table("group_members").select("user_id,role").eq("group_id", group_id).execute().data or []
    user_ids = [m["user_id"] for m in members]
    profiles = {}
    if user_ids:
        prof_data = sb.table("user_profiles").select("user_id,display_name,profile_image").in_("user_id", user_ids).execute().data or []
        profiles = {p["user_id"]: p for p in prof_data}
    for m in members:
        prof = profiles.get(m["user_id"])
        m["display_name"] = prof.get("display_name") if prof else "Unknown"
        m["profile_image"] = prof.get("profile_image") if prof else ""
    group["members"] = members
    bans = sb.table("group_bans").select("*").eq("group_id", group_id).execute().data or []
    group["bans"] = bans
    my_member = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    group["my_role"] = my_member.get("role") if my_member else None
    ban = _maybe(sb.table("group_bans").select("banned_until").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    group["am_i_banned"] = False
    if ban:
        if ban.get("banned_until") is None or _parse_dt(ban["banned_until"]) > datetime.now(timezone.utc):
            group["am_i_banned"] = True
    return group

@api_router.post("/groups/{group_id}/join")
def join_group(group_id: str, user: dict = Depends(get_current_user)):
    group = _maybe(sb.table("groups").select("*").eq("group_id", group_id).maybe_single().execute())
    if not group: raise HTTPException(404)
    existing = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if existing: raise HTTPException(400, "Already a member")
    ban = _maybe(sb.table("group_bans").select("banned_until").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if ban and (ban.get("banned_until") is None or _parse_dt(ban["banned_until"]) > datetime.now(timezone.utc)):
        raise HTTPException(403, "You are banned")
    join_cost = group.get("join_cost", 0)
    if join_cost > 0:
        spend_diamonds(user["user_id"], join_cost, "group_join", log_purchase=True)
    sb.table("group_members").insert({
        "group_id": group_id, "user_id": user["user_id"],
        "role": "member", "joined_at": datetime.now(timezone.utc).isoformat()
    }).execute()
    notify_user(group["creator_id"], "group_join", f"{user.get('name', 'Someone')} joined your group {group['title']}", user["user_id"])
    return {"ok": True}

@api_router.delete("/groups/{group_id}/members/{user_id}")
def remove_member(group_id: str, user_id: str, user: dict = Depends(get_current_user)):
    group = _maybe(sb.table("groups").select("creator_id").eq("group_id", group_id).maybe_single().execute())
    if not group: raise HTTPException(404)
    requester = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if not requester: raise HTTPException(403)
    if requester.get("role") == "creator" and user_id != user["user_id"]:
        sb.table("group_members").delete().eq("group_id", group_id).eq("user_id", user_id).execute()
        return {"ok": True}
    if requester.get("role") == "moderator":
        target = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user_id).maybe_single().execute())
        if target and target.get("role") not in ("creator", "moderator"):
            sb.table("group_members").delete().eq("group_id", group_id).eq("user_id", user_id).execute()
            return {"ok": True}
    if user_id == user["user_id"]:
        sb.table("group_members").delete().eq("group_id", group_id).eq("user_id", user["user_id"]).execute()
        return {"ok": True}
    raise HTTPException(403)

@api_router.put("/groups/{group_id}/members/{user_id}/role")
def change_member_role(group_id: str, user_id: str, payload: dict, user: dict = Depends(get_current_user)):
    new_role = payload.get("role")
    if new_role not in ("moderator", "member"):
        raise HTTPException(400)
    group = _maybe(sb.table("groups").select("creator_id").eq("group_id", group_id).maybe_single().execute())
    if not group or group.get("creator_id") != user["user_id"]:
        raise HTTPException(403)
    sb.table("group_members").update({"role": new_role}).eq("group_id", group_id).eq("user_id", user_id).execute()
    return {"ok": True}

@api_router.post("/groups/{group_id}/messages")
def send_group_message(group_id: str, payload: dict, user: dict = Depends(get_current_user)):
    content = payload.get("content", "")
    reply_to_id = payload.get("reply_to_id")
    reply_to_content = payload.get("reply_to_content", "")
    reply_to_sender = payload.get("reply_to_sender", "")
    
    if not content.strip(): raise HTTPException(400)
    if contains_profanity(content): raise HTTPException(400)
    member = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if not member: raise HTTPException(403)
    ban = _maybe(sb.table("group_bans").select("banned_until").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if ban and (ban.get("banned_until") is None or _parse_dt(ban["banned_until"]) > datetime.now(timezone.utc)):
        raise HTTPException(403)
    if not is_premium(user):
        msgs = sb.table("group_messages").select("message_id").eq("group_id", group_id).eq("sender_id", user["user_id"]).execute().data or []
        msg_count = len(msgs)
        if msg_count >= 2:
            require_token(user, 1)
    message_id = f"gm_{uuid.uuid4().hex[:12]}"
    
    insert_data = {
        "message_id": message_id,
        "group_id": group_id,
        "sender_id": user["user_id"],
        "content": content.strip(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    if reply_to_id:
        insert_data["reply_to_id"] = reply_to_id
    
    sb.table("group_messages").insert(insert_data).execute()
    
    # Get sender's display_name
    profile = _maybe(sb.table("user_profiles").select("display_name").eq("user_id", user["user_id"]).maybe_single().execute())
    
    response = {
        "ok": True,
        "message_id": message_id,
        "sender_name": profile.get("display_name", "Unknown") if profile else "Unknown",
    }
    
    if reply_to_id:
        response["reply_to"] = {
            "message_id": reply_to_id,
            "sender_name": reply_to_sender,
            "content": reply_to_content,
        }
    
    return response


@api_router.get("/groups/{group_id}/messages")
def get_group_messages(group_id: str, limit: int = 50, before: Optional[str] = None, user: dict = Depends(get_current_user)):
    member = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if not member: raise HTTPException(403)
    query = sb.table("group_messages").select("*").eq("group_id", group_id).order("created_at", desc=True).limit(limit)
    if before: query = query.lt("created_at", before)
    msgs = query.execute().data or []
    msgs.reverse()
    
    # Get sender display_names from user_profiles
    sender_ids = list({m["sender_id"] for m in msgs})
    sender_names = {}
    if sender_ids:
        profiles_data = sb.table("user_profiles").select("user_id,display_name").in_("user_id", sender_ids).execute().data or []
        sender_names = {p["user_id"]: p.get("display_name", "Unknown") for p in profiles_data}
    
    # Build reply lookup
    reply_ids = [m.get("reply_to_id") for m in msgs if m.get("reply_to_id")]
    reply_map = {}
    if reply_ids:
        reply_data = sb.table("group_messages").select("message_id,sender_id,content").in_("message_id", reply_ids).execute().data or []
        reply_sender_ids = list({r["sender_id"] for r in reply_data})
        reply_profiles = {}
        if reply_sender_ids:
            rp = sb.table("user_profiles").select("user_id,display_name").in_("user_id", reply_sender_ids).execute().data or []
            reply_profiles = {r["user_id"]: r for r in rp}
        for r in reply_data:
            rp = reply_profiles.get(r["sender_id"], {})
            reply_map[r["message_id"]] = {
                "sender_name": rp.get("display_name", "Unknown"),
                "content": r["content"],
            }
    
    for msg in msgs:
        msg["sender_name"] = sender_names.get(msg["sender_id"], "Unknown")
        if msg.get("reply_to_id") and msg["reply_to_id"] in reply_map:
            msg["reply_to"] = reply_map[msg["reply_to_id"]]
    
    return msgs


@api_router.delete("/groups/{group_id}/messages/{message_id}")
def delete_group_message(group_id: str, message_id: str, user: dict = Depends(get_current_user)):
    msg = _maybe(sb.table("group_messages").select("*").eq("message_id", message_id).eq("group_id", group_id).maybe_single().execute())
    if not msg: raise HTTPException(404)
    if msg.get("sender_id") == user["user_id"]:
        sb.table("group_messages").update({"deleted": True}).eq("message_id", message_id).execute()
        return {"ok": True}
    member = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if not member or member.get("role") not in ("creator", "moderator"):
        raise HTTPException(403)
    sb.table("group_messages").update({"deleted": True}).eq("message_id", message_id).execute()
    return {"ok": True}

@api_router.post("/groups/{group_id}/bans")
def ban_user(group_id: str, payload: dict, user: dict = Depends(get_current_user)):
    group = _maybe(sb.table("groups").select("creator_id").eq("group_id", group_id).maybe_single().execute())
    if not group or group.get("creator_id") != user["user_id"]:
        raise HTTPException(403)
    target_id = payload.get("user_id")
    duration_hours = payload.get("duration_hours")
    reason = payload.get("reason", "")
    banned_until = None
    if duration_hours:
        banned_until = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
    ban_id = f"ban_{uuid.uuid4().hex[:12]}"
    sb.table("group_bans").insert({
        "ban_id": ban_id, "group_id": group_id, "user_id": target_id,
        "banned_by": user["user_id"], "reason": reason,
        "banned_until": banned_until.isoformat() if banned_until else None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }).execute()
    sb.table("group_members").delete().eq("group_id", group_id).eq("user_id", target_id).execute()
    notify_user(target_id, "group_ban", f"You have been banned from {group['title']}: {reason}", user["user_id"])
    return {"ok": True}

@api_router.put("/groups/{group_id}")
def edit_group(group_id: str, payload: dict, user: dict = Depends(get_current_user)):
    group = _maybe(sb.table("groups").select("creator_id").eq("group_id", group_id).maybe_single().execute())
    if not group or group.get("creator_id") != user["user_id"]:
        raise HTTPException(403)
    updates = {}
    for field in ["title", "description", "rules", "join_cost"]:
        if field in payload:
            if field == "title":
                if not payload["title"].strip(): raise HTTPException(400)
                updates["title"] = payload["title"].strip()
            elif field == "join_cost":
                cost = int(payload["join_cost"])
                if cost < 0: raise HTTPException(400)
                updates["join_cost"] = cost
            else:
                updates[field] = payload[field]
    if "image" in payload:
        img = payload["image"]
        if img and (img.startswith("data:image") or "base64" in img):
            compressed = compress_image(img)
            filename = f"group_{uuid.uuid4().hex[:8]}.webp"
            updates["image"] = upload_image_to_supabase(compressed, user["user_id"], filename)
        else:
            updates["image"] = img or ""
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("groups").update(updates).eq("group_id", group_id).execute()
    return {"ok": True}

# ---------- Group message comments ----------
@api_router.post("/groups/{group_id}/messages/{message_id}/comments")
def create_group_comment(group_id: str, message_id: str, payload: dict, user: dict = Depends(get_current_user)):
    content = payload.get("content", "")
    parent_id = payload.get("parent_id")
    if not content.strip(): raise HTTPException(400)
    if contains_profanity(content): raise HTTPException(400)
    member = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if not member: raise HTTPException(403)
    ban = _maybe(sb.table("group_bans").select("banned_until").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if ban and (ban.get("banned_until") is None or _parse_dt(ban["banned_until"]) > datetime.now(timezone.utc)):
        raise HTTPException(403)
    msg = _maybe(sb.table("group_messages").select("message_id").eq("message_id", message_id).eq("group_id", group_id).maybe_single().execute())
    if not msg: raise HTTPException(404)
    if parent_id:
        parent = _maybe(sb.table("group_message_comments").select("comment_id").eq("comment_id", parent_id).maybe_single().execute())
        if not parent: raise HTTPException(404)
    comment_id = f"gmc_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    sb.table("group_message_comments").insert({
        "comment_id": comment_id, "message_id": message_id, "user_id": user["user_id"],
        "content": content.strip(), "parent_id": parent_id, "created_at": now
    }).execute()
    original_author = _maybe(sb.table("group_messages").select("sender_id").eq("message_id", message_id).maybe_single().execute())
    if original_author and original_author["sender_id"] != user["user_id"]:
        notify_user(original_author["sender_id"], "group_comment", f"Someone commented on your message in group {group_id}", user["user_id"])
    return {"ok": True, "comment_id": comment_id}

@api_router.get("/groups/{group_id}/messages/{message_id}/comments")
def get_group_comments(group_id: str, message_id: str, user: dict = Depends(get_current_user)):
    member = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
    if not member: raise HTTPException(403)
    comments = sb.table("group_message_comments").select("*").eq("message_id", message_id).order("created_at").execute().data or []
    user_ids = list({c["user_id"] for c in comments})
    author_names = {}
    if user_ids:
        # Use display_name from user_profiles
        profiles_data = sb.table("user_profiles").select("user_id,display_name").in_("user_id", user_ids).execute().data or []
        author_names = {p["user_id"]: p.get("display_name", "Unknown") for p in profiles_data}
    for c in comments:
        c["author_name"] = author_names.get(c["user_id"], "Unknown")
    return build_comment_tree(comments)

@api_router.delete("/groups/{group_id}/messages/{message_id}/comments/{comment_id}")
def delete_group_comment(group_id: str, message_id: str, comment_id: str, user: dict = Depends(get_current_user)):
    comment = _maybe(sb.table("group_message_comments").select("*").eq("comment_id", comment_id).eq("message_id", message_id).maybe_single().execute())
    if not comment: raise HTTPException(404)
    if comment["user_id"] == user["user_id"]:
        pass
    else:
        member = _maybe(sb.table("group_members").select("role").eq("group_id", group_id).eq("user_id", user["user_id"]).maybe_single().execute())
        if not member or member.get("role") not in ("creator", "moderator"):
            raise HTTPException(403)
    sb.table("group_message_comments").delete().eq("comment_id", comment_id).execute()
    return {"ok": True}
# ==================== EMAIL / PASSWORD AUTH ====================
def send_email(to_email: str, subject: str, body: str):
    brevo_api_key = os.environ.get("BREVO_API_KEY")
    smtp_from = os.environ.get("SMTP_FROM", "pastperfect@havenpositive.online")
    if not brevo_api_key:
        logger.info(f"Brevo API key not configured – email not sent.")
        return
    payload = {
        "sender": {"name": "Haven", "email": smtp_from},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": body,
    }
    try:
        resp = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers={"api-key": brevo_api_key, "Content-Type": "application/json", "Accept": "application/json"},
            timeout=10.0,
        )
        if resp.status_code not in (200, 201, 202):
            logger.error(f"Brevo API error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")

@api_router.post("/auth/signup")
def signup_email(payload: dict, request: Request, response: Response):
    email = payload.get("email", "").strip().lower()
    password = payload.get("password", "")
    name = payload.get("name", email.split("@")[0])
    if not email or not password:
        raise HTTPException(400, "Email and password required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    existing = _maybe(sb.table("users").select("user_id,deleted").eq("email", email).maybe_single().execute())
    if existing:
        if existing.get("deleted"):
            raise HTTPException(400, "An account with this email was deleted. Contact support.")
        raise HTTPException(400, "An account with this email already exists")
    raw_password = password[:72].encode("utf-8")
    password_hash = bcrypt.hashpw(raw_password, bcrypt.gensalt()).decode()
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    verification_token = uuid.uuid4().hex
    verification_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    session_token = secrets.token_hex(32)
    sb.table("users").insert({
        "user_id": user_id, "email": email, "name": name,
        "password_hash": password_hash, "email_verified": False,
        "verification_token": verification_token,
        "verification_token_expires": verification_expires,
        "created_at": now, "last_active": now,
        "tokens": 45, "diamonds": 5, "verified": False,
    }).execute()
    verify_link = f"https://havenpositive.online/verify-email?token={verification_token}"
    body = f"""<h2>Welcome to Haven!</h2><p>Please verify your email by clicking the link below:</p><a href="{verify_link}">{verify_link}</a><p>This link expires in 24 hours.</p>"""
    logger.info("VERIFICATION LINK: " + verify_link)
    def send_verification():
        try: send_email(email, "Verify your Haven account", body)
        except Exception as e: logger.error(f"Failed to send verification email: {e}")
    threading.Thread(target=send_verification).start()
    return {"ok": True, "message": "Account created! Please check your email to verify."}

@api_router.post("/auth/login")
def login_email(payload: dict, request: Request, response: Response):
    email = payload.get("email", "").strip().lower()
    password = payload.get("password", "")
    if not email or not password:
        raise HTTPException(400, "Email and password required")
    user = _maybe(sb.table("users").select("*").eq("email", email).eq("deleted", False).maybe_single().execute())
    if not user or not user.get("password_hash"):
        raise HTTPException(401, "Invalid email or password")
    try:
        if not bcrypt.checkpw(password[:72].encode("utf-8"), user["password_hash"].encode()):
            raise HTTPException(401, "Invalid email or password")
    except Exception:
        raise HTTPException(401, "Invalid email or password")
    if not user.get("email_verified"):
        raise HTTPException(403, "Please verify your email before logging in.")
    session_token = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    sb.table("user_sessions").upsert({
        "session_token": session_token, "user_id": user["user_id"],
        "expires_at": expires_at.isoformat(), "created_at": datetime.now(timezone.utc).isoformat()
    }).execute()
    sb.table("users").update({"last_active": datetime.now(timezone.utc).isoformat()}).eq("user_id", user["user_id"]).execute()
    response.set_cookie(key="session_token", value=session_token, httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/", max_age=7*24*60*60)
    return {"ok": True, "user_id": user["user_id"], "token": session_token}

@api_router.post("/auth/verify-email")
def verify_email(payload: dict):
    token = payload.get("token", "")
    if not token: raise HTTPException(400, "Missing verification token")
    user = _maybe(sb.table("users").select("*").eq("verification_token", token).maybe_single().execute())
    if not user: raise HTTPException(400, "Invalid or expired token")
    if user.get("email_verified"):
        return {"ok": True, "message": "Email already verified"}
    if _parse_dt(user["verification_token_expires"]) < datetime.now(timezone.utc):
        raise HTTPException(400, "Verification token has expired.")
    sb.table("users").update({
        "email_verified": True, "verification_token": None, "verification_token_expires": None
    }).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "message": "Email verified! You can now log in."}

@api_router.post("/auth/forgot-password")
def forgot_password(payload: dict):
    email = payload.get("email", "").strip().lower()
    if not email: raise HTTPException(400, "Email required")
    user = _maybe(sb.table("users").select("*").eq("email", email).eq("deleted", False).maybe_single().execute())
    if not user:
        return {"ok": True, "message": "If that email is registered, you'll receive a password reset link."}
    reset_token = uuid.uuid4().hex
    reset_expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sb.table("users").update({
        "reset_token": reset_token, "reset_token_expires": reset_expires
    }).eq("user_id", user["user_id"]).execute()
    reset_link = f"https://havenpositive.online/reset-password?token={reset_token}"
    body = f"""<h2>Password Reset</h2><p>Click the link below to reset your password:</p><a href="{reset_link}">{reset_link}</a><p>This link expires in 1 hour.</p>"""
    logger.info("PASSWORD RESET LINK: " + reset_link)
    def send_reset():
        try: send_email(email, "Reset your Haven password", body)
        except Exception as e: logger.error(f"Failed to send password reset email: {e}")
    threading.Thread(target=send_reset).start()
    return {"ok": True, "message": "If that email is registered, you'll receive a password reset link."}

@api_router.post("/auth/reset-password")
def reset_password(payload: dict):
    token = payload.get("token", "")
    new_password = payload.get("password", "")
    if not token or not new_password: raise HTTPException(400, "Token and new password required")
    if len(new_password) < 6: raise HTTPException(400, "Password must be at least 6 characters")
    user = _maybe(sb.table("users").select("*").eq("reset_token", token).maybe_single().execute())
    if not user: raise HTTPException(400, "Invalid or expired token")
    if _parse_dt(user["reset_token_expires"]) < datetime.now(timezone.utc):
        raise HTTPException(400, "Reset token has expired")
    raw_password = new_password[:72].encode("utf-8")
    new_hash = bcrypt.hashpw(raw_password, bcrypt.gensalt()).decode()
    sb.table("users").update({
        "password_hash": new_hash, "reset_token": None, "reset_token_expires": None
    }).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "message": "Password reset successfully."}

# ==================== ADMIN ====================
@api_router.get("/admin/check")
def admin_check(user: dict = Depends(get_current_user)):
    return {"is_admin": user.get("is_admin", False)}

@api_router.get("/admin/reports")
def admin_get_reports(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    reports = sb.table("user_reports").select("*").order("created_at", desc=True).execute().data or []
    all_ids = set()
    for r in reports:
        all_ids.add(r["reporter_id"])
        all_ids.add(r["reported_user_id"])
    users_map = {}
    if all_ids:
        users_data = sb.table("users").select("user_id,email,name").in_("user_id", list(all_ids)).execute().data or []
        users_map = {u["user_id"]: u for u in users_data}
    result = []
    for r in reports:
        reporter = users_map.get(r["reporter_id"])
        reported = users_map.get(r["reported_user_id"])
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
    if not user.get("is_admin"): raise HTTPException(403)
    sb.table("user_reports").delete().eq("report_id", report_id).execute()
    return {"ok": True}

@api_router.post("/admin/ban")
def admin_ban_user(payload: AdminBanPayload, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    update_data = {"deleted": True, "banned": True, "banned_reason": payload.reason}
    if payload.duration_days:
        update_data["banned_until"] = (datetime.now(timezone.utc) + timedelta(days=payload.duration_days)).isoformat()
    else:
        update_data["banned_until"] = None
    sb.table("users").update(update_data).eq("user_id", payload.user_id).execute()
    sb.table("user_sessions").delete().eq("user_id", payload.user_id).execute()
    return {"ok": True}

@api_router.post("/admin/unban")
def admin_unban_user(payload: AdminUnbanPayload, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    sb.table("users").update({"deleted": False, "banned": False, "banned_reason": None, "banned_until": None}).eq("user_id", payload.user_id).execute()
    return {"ok": True}

@api_router.get("/admin/stories")
def admin_get_stories(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    stories = sb.table("stories").select("*").order("created_at", desc=True).limit(200).execute().data or []
    return stories

@api_router.delete("/admin/stories/{story_id}")
def admin_delete_story(story_id: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    sb.table("story_comments").delete().eq("story_id", story_id).execute()
    sb.table("story_likes").delete().eq("story_id", story_id).execute()
    sb.table("stories").delete().eq("story_id", story_id).execute()
    return {"ok": True}

@api_router.get("/admin/users")
def admin_search_users(query: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    res = sb.table("users").select("user_id,email,name,verified,premium_tier,deleted,banned,tokens,diamonds,last_active,created_at").or_(f"email.ilike.%{query}%,name.ilike.%{query}%").limit(20).execute()
    users = res.data or []
    profiles = sb.table("user_profiles").select("user_id,display_name").or_(f"display_name.ilike.%{query}%").limit(20).execute().data or []
    profile_map = {p["user_id"]: p["display_name"] for p in profiles}
    result = []
    for u in users:
        u["display_name"] = profile_map.get(u["user_id"]) or u.get("name","")
        result.append(u)
    return result

@api_router.get("/admin/stats")
def admin_stats(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    total_users = sb.table("users").select("user_id", count="exact").eq("deleted", False).execute().count
    today = datetime.now(timezone.utc).date()
    new_today = sb.table("users").select("user_id", count="exact").gte("created_at", today.isoformat()).execute().count
    active = sb.table("users").select("user_id", count="exact").gte("last_active", (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()).execute().count
    purchases = sb.table("diamond_purchases").select("item").execute().data or []
    total_purchases = len(purchases)
    return {"total_users": total_users, "new_today": new_today, "active_last_24h": active, "diamond_purchases": total_purchases}

@api_router.post("/admin/announce")
def admin_announce(payload: AdminAnnouncePayload, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    users = sb.table("users").select("user_id").eq("deleted", False).execute().data or []
    sent_count = 0
    errors = 0
    for u in users:
        try:
            notify_user(u["user_id"], "announcement", payload.message, user["user_id"])
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to notify {u['user_id']}: {e}")
            errors += 1
    return {"ok": True, "sent_to": sent_count, "errors": errors}

@api_router.post("/admin/verify-user")
def admin_verify_user(payload: AdminVerifyUserPayload, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    sb.table("users").update({"verified": payload.verified}).eq("user_id", payload.user_id).execute()
    return {"ok": True}

@api_router.get("/admin/image-queue")
def admin_image_queue(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
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

@api_router.delete("/admin/images/{user_id}/{image_type}/{image_index}")
def admin_delete_image(user_id: str, image_type: str, image_index: int, admin_user: dict = Depends(get_current_user)):
    if not admin_user.get("is_admin"): raise HTTPException(403)
    if image_type not in ("profile", "gallery"): raise HTTPException(400)
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user_id).maybe_single().execute())
    if not profile: raise HTTPException(404)
    if image_type == "profile":
        image_url = profile.get("profile_image")
        if not image_url: raise HTTPException(400)
        path = extract_path_from_url(image_url)
        if path:
            try: sb.storage.from_(STORAGE_BUCKET).remove([path])
            except Exception as e: logger.warning(f"Could not delete profile image {path}: {e}")
        sb.table("user_profiles").update({"profile_image": None}).eq("user_id", user_id).execute()
    else:
        gallery = profile.get("gallery_images") or []
        if image_index < 0 or image_index >= len(gallery): raise HTTPException(400)
        image_url = gallery[image_index]
        path = extract_path_from_url(image_url)
        if path:
            try: sb.storage.from_(STORAGE_BUCKET).remove([path])
            except Exception as e: logger.warning(f"Could not delete gallery image {path}: {e}")
        del gallery[image_index]
        sb.table("user_profiles").update({"gallery_images": gallery}).eq("user_id", user_id).execute()
    return {"ok": True}

def extract_path_from_url(url: str) -> Optional[str]:
    if not url: return None
    prefix = f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/"
    if url.startswith(prefix):
        return url[len(prefix):]
    return None

# ---------- Report content ----------
@api_router.post("/report-content")
def report_content(payload: dict, user: dict = Depends(get_current_user)):
    reported_user_id = payload.get("reported_user_id")
    reason = payload.get("reason", "")
    image_index = payload.get("image_index")
    story_id = payload.get("story_id")
    if not reported_user_id or not reason: raise HTTPException(400, "Missing reported_user_id or reason")
    full_reason = reason
    if image_index is not None: full_reason += f" (image {image_index})"
    if story_id: full_reason += f" (story {story_id})"
    sb.table("user_reports").insert({
        "report_id": f"rep_{uuid.uuid4().hex[:12]}",
        "reporter_id": user["user_id"],
        "reported_user_id": reported_user_id,
        "reason": full_reason,
        "created_at": datetime.now(timezone.utc).isoformat()
    }).execute()
    notify_user(reported_user_id, "warning", f"Your content has been reported for: {reason}. Our team will review it.", user["user_id"])
    return {"ok": True}

@api_router.get("/admin/reports-detailed")
def get_detailed_reports(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    reports = sb.table("user_reports").select("*").order("created_at", desc=True).execute().data or []
    all_ids = set()
    for r in reports:
        all_ids.add(r["reporter_id"])
        all_ids.add(r["reported_user_id"])
    users_map = {}
    if all_ids:
        users_data = sb.table("users").select("user_id,email,name").in_("user_id", list(all_ids)).execute().data or []
        users_map = {u["user_id"]: u for u in users_data}
    enriched = []
    for r in reports:
        item = dict(r)
        reason_full = r.get("reason", "")
        base_reason = reason_full
        image_index = None
        story_id = None
        if "(image " in reason_full:
            base_reason = reason_full.split(" (image ")[0]
            try: image_index = int(reason_full.split("(image ")[1].split(")")[0])
            except: pass
        if "(story " in reason_full:
            base_reason = reason_full.split(" (story ")[0]
            story_id = reason_full.split("(story ")[1].rstrip(")").strip()
        item["reason_clean"] = base_reason
        item["type"] = "photo" if image_index is not None else ("story" if story_id else "user")
        reporter = users_map.get(r["reporter_id"])
        item["reporter_name"] = reporter["name"] or reporter["email"] if reporter else "Unknown"
        target = users_map.get(r["reported_user_id"])
        item["reported_name"] = target["name"] or target["email"] if target else "Unknown"
        item["reported_email"] = target["email"] if target else ""
        if item["type"] == "photo" and image_index is not None:
            profile = _maybe(sb.table("user_profiles").select("profile_image,gallery_images").eq("user_id", r["reported_user_id"]).maybe_single().execute())
            if profile:
                if image_index == 0: item["image_url"] = profile.get("profile_image")
                else:
                    gallery = profile.get("gallery_images") or []
                    if image_index - 1 < len(gallery): item["image_url"] = gallery[image_index - 1]
        if item["type"] == "story" and story_id:
            story = _maybe(sb.table("stories").select("content").eq("story_id", story_id).maybe_single().execute())
            if story: item["story_content"] = story.get("content", "")
        enriched.append(item)
    return enriched

@api_router.post("/admin/suspend")
def admin_suspend_user(payload: dict, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    target_id = payload.get("user_id")
    reason = payload.get("reason", "Violation of guidelines")
    sb.table("users").update({"deleted": True, "banned": True, "banned_reason": reason}).eq("user_id", target_id).execute()
    sb.table("user_sessions").delete().eq("user_id", target_id).execute()
    notify_user(target_id, "warning", f"Your account has been suspended for: {reason}.", user["user_id"])
    return {"ok": True}

@api_router.delete("/admin/reports/{report_id}/delete-photo")
def admin_delete_reported_photo(report_id: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    report = _maybe(sb.table("user_reports").select("*").eq("report_id", report_id).maybe_single().execute())
    if not report: raise HTTPException(404)
    reason = report.get("reason", "")
    image_index = None
    if "(image " in reason:
        try: image_index = int(reason.split("(image ")[1].split(")")[0])
        except: raise HTTPException(400)
    target_user_id = report["reported_user_id"]
    profile = _maybe(sb.table("user_profiles").select("profile_image,gallery_images").eq("user_id", target_user_id).maybe_single().execute())
    if profile:
        if image_index == 0:
            sb.table("user_profiles").update({"profile_image": None}).eq("user_id", target_user_id).execute()
        else:
            gallery = profile.get("gallery_images") or []
            if image_index - 1 < len(gallery):
                del gallery[image_index - 1]
                sb.table("user_profiles").update({"gallery_images": gallery}).eq("user_id", target_user_id).execute()
    sb.table("user_reports").delete().eq("report_id", report_id).execute()
    notify_user(target_user_id, "warning", f"Your photo was removed because it was reported as {reason}.", user["user_id"])
    return {"ok": True}

@api_router.delete("/admin/reports/{report_id}/delete-story")
def admin_delete_reported_story(report_id: str, user: dict = Depends(get_current_user)):
    if not user.get("is_admin"): raise HTTPException(403)
    report = _maybe(sb.table("user_reports").select("*").eq("report_id", report_id).maybe_single().execute())
    if not report: raise HTTPException(404)
    reason = report.get("reason", "")
    story_id = None
    if "(story " in reason: story_id = reason.split("(story ")[1].rstrip(")")
    if story_id: sb.table("stories").delete().eq("story_id", story_id).execute()
    sb.table("user_reports").delete().eq("report_id", report_id).execute()
    notify_user(report["reported_user_id"], "warning", f"Your story was removed because it was reported as {reason}.", user["user_id"])
    return {"ok": True}

# ---------- User profile lookup ----------
@api_router.get("/users/{user_id}/profile")
def get_user_profile(user_id: str):
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user_id).maybe_single().execute())
    if not profile: raise HTTPException(404, "User not found")
    return profile

# ==================== PUBLIC CHATS ====================

def ensure_public_chats():
    """Create HIV, HSV, HPV public chats if they don't exist."""
    for chat_type in ["HIV", "HSV", "HPV"]:
        existing = _maybe(sb.table("public_chats").select("chat_id").eq("chat_type", chat_type).maybe_single().execute())
        if not existing:
            chat_id = f"pub_{chat_type.lower()}_{uuid.uuid4().hex[:8]}"
            sb.table("public_chats").insert({
                "chat_id": chat_id,
                "chat_type": chat_type,
                "title": f"{chat_type} Community Chat",
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()

@api_router.get("/public-chats")
def get_public_chats(user: dict = Depends(get_current_user)):
    chats = sb.table("public_chats").select("*").execute().data or []
    result = []
    for chat in chats:
        last_msg = sb.table("public_chat_messages").select("*").eq("chat_id", chat["chat_id"]).order("created_at", desc=True).limit(1).execute().data
        last_message = last_msg[0] if last_msg else None
        five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        active_senders = sb.table("public_chat_messages").select("sender_id").eq("chat_id", chat["chat_id"]).gte("created_at", five_min_ago).execute().data or []
        active_count = len(set(m["sender_id"] for m in active_senders))
        total_res = sb.table("public_chat_messages").select("message_id", count="exact").eq("chat_id", chat["chat_id"]).execute()
        total_count = total_res.count if hasattr(total_res, 'count') else 0
        result.append({
            "chat_id": chat["chat_id"],
            "chat_type": chat["chat_type"],
            "title": chat["title"],
            "last_message": last_message["content"][:100] if last_message else None,
            "last_message_time": last_message["created_at"] if last_message else None,
            "active_users": active_count,
            "total_messages": total_count,
        })
    return result

@api_router.get("/public-chats/{chat_id}/messages")
def get_public_chat_messages(chat_id: str, limit: int = 50, user: dict = Depends(get_current_user)):
    chat = _maybe(sb.table("public_chats").select("*").eq("chat_id", chat_id).maybe_single().execute())
    if not chat:
        raise HTTPException(404, "Chat not found")
    msgs = sb.table("public_chat_messages").select("*").eq("chat_id", chat_id).order("created_at", desc=True).limit(limit).execute().data or []
    msgs.reverse()
    sender_ids = list({m["sender_id"] for m in msgs})
    profiles = {}
    if sender_ids:
        prof_data = sb.table("user_profiles").select("user_id,display_name,profile_image").in_("user_id", sender_ids).execute().data or []
        profiles = {p["user_id"]: p for p in prof_data}
    reply_ids = [m.get("reply_to_id") for m in msgs if m.get("reply_to_id")]
    reply_map = {}
    if reply_ids:
        reply_data = sb.table("public_chat_messages").select("message_id,sender_id,content").in_("message_id", reply_ids).execute().data or []
        reply_sender_ids = list({r["sender_id"] for r in reply_data})
        reply_profiles = {}
        if reply_sender_ids:
            rp = sb.table("user_profiles").select("user_id,display_name").in_("user_id", reply_sender_ids).execute().data or []
            reply_profiles = {r["user_id"]: r for r in rp}
        for r in reply_data:
            rp = reply_profiles.get(r["sender_id"], {})
            reply_map[r["message_id"]] = {
                "sender_name": rp.get("display_name", "Someone"),
                "content": r["content"],
            }
    result = []
    for msg in msgs:
        prof = profiles.get(msg["sender_id"], {})
        item = {
            "message_id": msg["message_id"],
            "chat_id": msg["chat_id"],
            "sender_id": msg["sender_id"],
            "sender_name": prof.get("display_name", "Anonymous"),
            "sender_avatar": prof.get("profile_image", ""),
            "content": msg["content"],
            "created_at": msg["created_at"],
        }
        if msg.get("reply_to_id") and msg["reply_to_id"] in reply_map:
            item["reply_to"] = reply_map[msg["reply_to_id"]]
        result.append(item)
    return result

@api_router.post("/public-chats/{chat_id}/messages")
def send_public_chat_message(chat_id: str, payload: PublicChatMessagePayload, user: dict = Depends(get_current_user)):
    if contains_profanity(payload.content):
        raise HTTPException(400, "Message contains inappropriate language")
    chat = _maybe(sb.table("public_chats").select("*").eq("chat_id", chat_id).maybe_single().execute())
    if not chat:
        raise HTTPException(404, "Chat not found")
    message_id = f"pubmsg_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    insert_data = {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_id": user["user_id"],
        "content": payload.content.strip(),
        "created_at": now
    }
    if payload.reply_to_id:
        insert_data["reply_to_id"] = payload.reply_to_id
    sb.table("public_chat_messages").insert(insert_data).execute()
    profile = _maybe(sb.table("user_profiles").select("display_name,profile_image").eq("user_id", user["user_id"]).maybe_single().execute())
    response_msg = {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_id": user["user_id"],
        "sender_name": profile.get("display_name", user.get("name", "Anonymous")) if profile else user.get("name", "Anonymous"),
        "sender_avatar": profile.get("profile_image", "") if profile else "",
        "content": payload.content.strip(),
        "created_at": now,
    }
    if payload.reply_to_id:
        reply_msg = _maybe(sb.table("public_chat_messages").select("message_id,sender_id,content").eq("message_id", payload.reply_to_id).maybe_single().execute())
        if reply_msg:
            reply_prof = _maybe(sb.table("user_profiles").select("display_name").eq("user_id", reply_msg["sender_id"]).maybe_single().execute())
            response_msg["reply_to"] = {
                "sender_name": reply_prof.get("display_name", "Someone") if reply_prof else "Someone",
                "content": reply_msg["content"],
            }
    return {"ok": True, "message": response_msg}

@api_router.delete("/public-chats/messages/{message_id}")
def delete_public_chat_message(message_id: str, user: dict = Depends(get_current_user)):
    msg = _maybe(sb.table("public_chat_messages").select("*").eq("message_id", message_id).maybe_single().execute())
    if not msg:
        raise HTTPException(404, "Message not found")
    if msg["sender_id"] != user["user_id"] and not user.get("is_admin"):
        raise HTTPException(403, "Cannot delete this message")
    sb.table("public_chat_messages").delete().eq("message_id", message_id).execute()
    return {"ok": True}


# ==================== WHATSAPP (TWILIO) ====================

# ==================== WHATSAPP (TWILIO) ====================

import os
from twilio.rest import Client as TwilioClient

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")


def send_whatsapp_message(to_phone: str, sender_name: str, message: str):
    """Send a WhatsApp message via Twilio."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(500, "WhatsApp service not configured")
    
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    
    # Remove any existing whatsapp: prefix, then add it fresh
    if to_phone.startswith("whatsapp:"):
        to_phone = to_phone[9:]  # Remove "whatsapp:"
    
    # Clean the number (remove spaces, etc.)
    to_phone = to_phone.strip().replace(" ", "")
    
    # Add the whatsapp: prefix
    to_whatsapp = f"whatsapp:{to_phone}"
    
    logger.info(f"Sending WhatsApp from {TWILIO_WHATSAPP_NUMBER} to {to_whatsapp}")
    
    msg = client.messages.create(
        body=f"💜 Haven\n{sender_name} sent you a message:\n\n{message}\n\n💬 Chat: https://havenpositive.online/matches",
        from_=TWILIO_WHATSAPP_NUMBER,
        to=to_whatsapp
    )
    return msg.sid


class WhatsAppPayload(BaseModel):
    user_id: str
    message: str


@api_router.post("/whatsapp/send")
def send_whatsapp(payload: WhatsAppPayload, user: dict = Depends(get_current_user)):
    if not user.get("verified"):
        raise HTTPException(400, "Only verified users can send WhatsApp messages")
    
    if user.get("diamonds", 0) < 3:
        raise HTTPException(402, "You need 3 diamonds to send a WhatsApp message")
    
    target_profile = _maybe(sb.table("user_profiles").select("phone_number,display_name").eq("user_id", payload.user_id).maybe_single().execute())
    if not target_profile or not target_profile.get("phone_number"):
        raise HTTPException(400, "This user hasn't added their phone number yet")
    
    uid1, uid2 = sorted([user["user_id"], payload.user_id])
    match = _maybe(sb.table("profile_matches").select("*").eq("user1_id", uid1).eq("user2_id", uid2).maybe_single().execute())
    if not match:
        raise HTTPException(403, "You can only send WhatsApp messages to matched users")
    
    spend_diamonds(user["user_id"], 3, "whatsapp_message", log_purchase=True)
    
    sender_profile = _maybe(sb.table("user_profiles").select("display_name").eq("user_id", user["user_id"]).maybe_single().execute())
    sender_name = sender_profile.get("display_name", "Someone") if sender_profile else "Someone"
    
    try:
        send_whatsapp_message(target_profile["phone_number"], sender_name, payload.message.strip())
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")
        raise HTTPException(500, "Failed to send WhatsApp message")
    
    notify_user(payload.user_id, "whatsapp_message", f"{sender_name} sent you a WhatsApp message", user["user_id"])
    
    return {"ok": True, "message": "WhatsApp message sent!"}


@api_router.get("/whatsapp/contacts")
def get_whatsapp_contacts(user: dict = Depends(get_current_user)):
    sent_dating = sb.table("dating_requests").select("*").eq("from_user_id", user["user_id"]).eq("status", "pending").execute().data or []
    sent_friend = sb.table("friend_requests").select("*").eq("from_user_id", user["user_id"]).eq("status", "pending").execute().data or []
    
    pending_requests = []
    for req in sent_dating:
        pending_requests.append({"request_id": req["request_id"], "type": "dating", "user_id": req["to_user_id"], "status": "pending", "created_at": req["created_at"]})
    for req in sent_friend:
        pending_requests.append({"request_id": req["request_id"], "type": "friend", "user_id": req["to_user_id"], "status": "pending", "created_at": req["created_at"]})
    
    dating_matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").eq("swipe_type", "dating").execute().data or []
    friend_matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").eq("swipe_type", "friendship").execute().data or []
    
    all_user_ids = set()
    for req in pending_requests: all_user_ids.add(req["user_id"])
    for m in dating_matches:
        pid = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        all_user_ids.add(pid)
    for m in friend_matches:
        pid = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        all_user_ids.add(pid)
    
    profiles = {}
    if all_user_ids:
        prof_data = sb.table("user_profiles").select("user_id,display_name,profile_image,phone_number,country,city").in_("user_id", list(all_user_ids)).execute().data or []
        profiles = {p["user_id"]: p for p in prof_data}
    
    result = {"pending_requests": [], "dating_matches": [], "friend_matches": []}
    
    for req in pending_requests:
        prof = profiles.get(req["user_id"], {})
        result["pending_requests"].append({"request_id": req["request_id"], "type": req["type"], "user_id": req["user_id"], "display_name": prof.get("display_name", "Unknown"), "profile_image": prof.get("profile_image", ""), "phone_number": prof.get("phone_number", ""), "country": prof.get("country", ""), "city": prof.get("city", ""), "created_at": req["created_at"]})
    
    for m in dating_matches:
        pid = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        prof = profiles.get(pid, {})
        result["dating_matches"].append({"match_id": m["match_id"], "user_id": pid, "display_name": prof.get("display_name", "Unknown"), "profile_image": prof.get("profile_image", ""), "phone_number": prof.get("phone_number", ""), "country": prof.get("country", ""), "city": prof.get("city", ""), "created_at": m["created_at"]})
    
    for m in friend_matches:
        pid = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        prof = profiles.get(pid, {})
        result["friend_matches"].append({"match_id": m["match_id"], "user_id": pid, "display_name": prof.get("display_name", "Unknown"), "profile_image": prof.get("profile_image", ""), "phone_number": prof.get("phone_number", ""), "country": prof.get("country", ""), "city": prof.get("city", ""), "created_at": m["created_at"]})
    
    return result

# ==================== EMAIL MESSAGING ====================

class EmailPayload(BaseModel):
    user_id: str
    subject: str
    message: str


@api_router.post("/email/send")
def send_email_message(payload: EmailPayload, user: dict = Depends(get_current_user)):
    """Send an email to a matched user. Free for all verified users."""
    
    # Check verification
    if not user.get("verified"):
        raise HTTPException(400, "Only verified users can send email messages")
    
    # Get target user's email
    target_user = _maybe(sb.table("users").select("email").eq("user_id", payload.user_id).maybe_single().execute())
    if not target_user or not target_user.get("email"):
        raise HTTPException(400, "This user's email is not available")
    
    # Verify they are matched (either dating or friends)
    uid1, uid2 = sorted([user["user_id"], payload.user_id])
    match = _maybe(sb.table("profile_matches").select("*").eq("user1_id", uid1).eq("user2_id", uid2).maybe_single().execute())
    if not match:
        raise HTTPException(403, "You can only send emails to matched users")
    
    # Get sender's display name
    sender_profile = _maybe(sb.table("user_profiles").select("display_name").eq("user_id", user["user_id"]).maybe_single().execute())
    sender_name = sender_profile.get("display_name", "Someone") if sender_profile else "Someone"
    
    # Send the email
    try:
        email_body = f"""
        <h2>💜 Haven Dating</h2>
        <p><strong>{sender_name}</strong> sent you a message:</p>
        <blockquote style="background:#f5f3ee;padding:12px;border-radius:8px;">
            {payload.message}
        </blockquote>
        <p>💬 <a href="https://havenpositive.online/matches">Chat on Haven</a></p>
        """
        send_email(target_user["email"], payload.subject or f"Message from {sender_name}", email_body)
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        raise HTTPException(500, "Failed to send email")
    
    # Notify the recipient
    notify_user(
        payload.user_id,
        "email_message",
        f"{sender_name} sent you an email",
        user["user_id"]
    )
    
    return {"ok": True, "message": "Email sent!"}


# ==================== MOUNT ROUTER ====================
app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


