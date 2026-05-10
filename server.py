from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Cookie, Header
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import os, logging, uuid, random, math, httpx, io, base64
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone, timedelta
from PIL import Image
import httpx as httpx_lib

# SendGrid for email reminders
import sendgrid
from sendgrid.helpers.mail import Mail

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']

# Create Supabase client normally
sb: Client = create_client(SUPABASE_URL.rstrip('/'), SUPABASE_KEY)

# Override the internal httpx client to use HTTP/1.1 (prevents ReadError)
http_client = httpx_lib.Client(http2=False)
sb.postgrest.session = http_client

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://havenpositive.online",
        "https://www.havenpositive.online",
        "https://haven-83b20.web.app",
        "https://haven-83b20.firebaseapp.com",
        "https://haven-hmwq.onrender.com",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter(prefix="/api")

# ---------- Constants ----------
EARTH_RADIUS_KM = 6371
MAX_GPS_AGE_HOURS = 24
STORAGE_BUCKET = "avatars"

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
        resp = httpx.get(
            f"https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": "HavenApp/1.0"},
            timeout=5
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

async def get_location_from_ip(ip: str) -> tuple:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://ip-api.com/json/{ip}?fields=status,lat,lon,country,city")
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    return {
                        'latitude': data.get('lat'),
                        'longitude': data.get('lon'),
                        'country': data.get('country'),
                        'city': data.get('city'),
                        'source': 'ip'
                    }
    except Exception as e:
        logger.error(f"IP geolocation failed: {e}")
    return None

# ---------- Email helper ----------
def send_reminder_email(to_email: str, name: str):
    sg_api_key = os.environ.get("SENDGRID_API_KEY")
    if not sg_api_key:
        logger.warning("SENDGRID_API_KEY not set – cannot send email")
        return

    sg = sendgrid.SendGridAPIClient(api_key=sg_api_key)

    message = Mail(
        from_email=os.environ.get("FROM_EMAIL", "hello@havenpositive.online"),
        to_emails=to_email,
        subject="You've been missed on Haven 💜",
        html_content=f"""
        <p>Hi {name or 'there'},</p>
        <p>We noticed you haven't visited Haven in a few days. Your community is waiting for you!</p>
        <p><a href="https://havenpositive.online">Come back to Haven</a></p>
        <p>Warmly,<br>The Haven Team</p>
        """
    )

    sg.send(message)

# ---------- Image Helpers (WebP) ----------
def compress_image(base64_str: str, max_size_kb: int = 300) -> bytes:
    if "," in base64_str:
        base64_str = base64_str.split(",", 1)[1]
    img_data = base64.b64decode(base64_str)
    img = Image.open(io.BytesIO(img_data))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    w, h = img.size
    if w > 1200 or h > 1200:
        img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    quality = 85
    while True:
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality)
        size_kb = buf.tell() / 1024
        if size_kb <= max_size_kb or quality <= 20:
            break
        quality -= 5
    return buf.getvalue()

def upload_image_to_supabase(file_bytes: bytes, user_id: str, filename: str) -> str:
    path = f"{user_id}/{filename}"
    sb.storage.from_(STORAGE_BUCKET).upload(
        path=path,
        file=file_bytes,
        file_options={
            "content-type": "image/webp",
            "cache-control": "public, max-age=31536000, immutable"
        }
    )
    # Return direct public URL (CDN will cache it)
    return f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{path}"

def process_image_field(image_value: str, user_id: str, filename_prefix: str) -> str:
    if not image_value:
        return image_value
    if image_value.startswith("data:image") or (len(image_value) > 1000 and "base64" in image_value):
        try:
            compressed = compress_image(image_value)
            filename = f"{filename_prefix}_{uuid.uuid4().hex[:8]}.jpg"   # extension doesn't matter, content-type is webp
            return upload_image_to_supabase(compressed, user_id, filename)
        except Exception as e:
            logger.error(f"Image compression/upload failed: {e}")
            return image_value
    return image_value

# ---------- Models ----------
class LocationUpdatePayload(BaseModel):
    latitude: float
    longitude: float
    accuracy: Optional[float] = None

class GoogleAuthPayload(BaseModel):
    id_token: str; email: str; name: str; picture: str; ref: Optional[str] = None

class ProfileSetupPayload(BaseModel):
    date_of_birth: str; gender: str; health_status: str
    display_name: Optional[str] = ""; bio: Optional[str] = ""
    interests: Optional[str] = ""; looking_for: Optional[str] = ""
    education: Optional[str] = ""; kids: Optional[str] = ""
    want_kids: Optional[str] = ""; smoke: Optional[str] = ""
    drink: Optional[str] = ""; employment: Optional[str] = ""
    profile_image: Optional[str] = ""; gallery_images: Optional[List[str]] = []
    pref_gender: Optional[str] = ""; pref_min_age: Optional[int] = 18
    pref_max_age: Optional[int] = 99; pref_country: Optional[str] = ""
    pref_max_distance: Optional[int] = 50; pref_health_status: Optional[str] = ""
    profile_hidden: Optional[bool] = False
    hide_from_min_age: Optional[int] = None; hide_from_max_age: Optional[int] = None
    hide_from_health_statuses: Optional[str] = ""

class ProfileUpdatePayload(BaseModel):
    date_of_birth: Optional[str] = None; gender: Optional[str] = None
    health_status: Optional[str] = None
    display_name: Optional[str] = None; bio: Optional[str] = None
    interests: Optional[str] = None; looking_for: Optional[str] = None
    education: Optional[str] = None; kids: Optional[str] = None
    want_kids: Optional[str] = None; smoke: Optional[str] = None
    drink: Optional[str] = None; employment: Optional[str] = None
    profile_image: Optional[str] = None; gallery_images: Optional[List[str]] = None
    pref_gender: Optional[str] = None; pref_min_age: Optional[int] = None
    pref_max_age: Optional[int] = None; pref_country: Optional[str] = None
    pref_max_distance: Optional[int] = None; pref_health_status: Optional[str] = None
    profile_hidden: Optional[bool] = None
    hide_from_min_age: Optional[int] = None; hide_from_max_age: Optional[int] = None
    hide_from_health_statuses: Optional[str] = None

class CreateStoryPayload(BaseModel):
    content: str; category: str; title: Optional[str] = ""
class CreateCommentPayload(BaseModel):
    content: str; parent_id: Optional[str] = None
class SwipePayload(BaseModel):
    swiped_id: str; direction: str; swipe_type: Optional[str] = 'dating'
class MatchMessagePayload(BaseModel):
    content: str

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
    return user

@app.get("/")
def root():
    return {"message": "Haven API is running"}

@api_router.get("/")
def api_root():
    return {"message": "Haven API"}

@api_router.post("/auth/google")
def auth_google(payload: GoogleAuthPayload, request: Request, response: Response):
    email, name, picture = payload.email, payload.name, payload.picture
    session_token = f"session_{uuid.uuid4().hex[:32]}"
    existing = _maybe(sb.table("users").select("*").eq("email", email).maybe_single().execute())
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing:
        user_id = existing["user_id"]
        sb.table("users").update({"name": name, "picture": picture}).eq("user_id", user_id).execute()
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        sb.table("users").insert({
            "user_id": user_id, "email": email, "name": name, "picture": picture,
            "created_at": now_iso,
        }).execute()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    sb.table("user_sessions").upsert({"session_token": session_token, "user_id": user_id, "expires_at": expires_at.isoformat(), "created_at": now_iso}).execute()
    # Record login time
    sb.table("users").update({"last_login": now_iso}).eq("user_id", user_id).execute()
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
        max_age=7*24*60*60
    )
    return {"ok": True, "user_id": user_id, "token": session_token}

@api_router.get("/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    onboarding_complete = profile.get("onboarding_complete", False) if profile else False
    has_gps = profile and profile.get("gps_latitude") is not None if profile else False
    gps_stale = False
    if has_gps and profile.get("gps_verified_at"):
        gps_age = datetime.now(timezone.utc) - _parse_dt(profile["gps_verified_at"])
        gps_stale = gps_age > timedelta(hours=MAX_GPS_AGE_HOURS)
    notif_count = sb.table("notifications").select("notification_id", count="exact").eq("user_id", user["user_id"]).eq("read", False).execute()
    unread = notif_count.count if hasattr(notif_count, 'count') else 0
    return {
        "user_id": user["user_id"], "email": user["email"], "name": user["name"],
        "picture": user.get("picture", ""),
        "onboarding_complete": onboarding_complete,
        "unread_notifications": unread,
        "has_gps": has_gps,
        "gps_stale": gps_stale,
        "needs_location": not has_gps or gps_stale,
    }

@api_router.post("/auth/logout")
def auth_logout(response: Response, session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"), authorization: Optional[str] = Header(default=None)):
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "): token = authorization.split(" ", 1)[1]
    if token: sb.table("user_sessions").delete().eq("session_token", token).execute()
    response.delete_cookie(key="session_token", path="/", samesite="lax", secure=False)
    return {"ok": True}

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
        "gps_latitude": payload.latitude,
        "gps_longitude": payload.longitude,
        "gps_verified_at": now.isoformat(),
        "gps_accuracy": payload.accuracy,
        "location_source": "gps",
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "country": country,
        "city": city or "",
        "updated_at": now.isoformat(),
    }
    existing = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", user["user_id"]).maybe_single().execute())
    if existing:
        sb.table("user_profiles").update(profile_data).eq("user_id", user["user_id"]).execute()
    else:
        profile_data["user_id"] = user["user_id"]
        profile_data["created_at"] = now.isoformat()
        sb.table("user_profiles").insert(profile_data).execute()
    return {
        "ok": True,
        "message": "GPS location updated",
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "country": country,
        "city": city,
    }

@api_router.get("/location/ip-fallback")
async def ip_fallback(request: Request, user: dict = Depends(get_current_user)):
    client_ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    location = await get_location_from_ip(client_ip)
    if location:
        now = datetime.now(timezone.utc)
        profile_data = {
            "gps_latitude": location['latitude'],
            "gps_longitude": location['longitude'],
            "gps_verified_at": now.isoformat(),
            "location_source": "ip",
            "latitude": location['latitude'],
            "longitude": location['longitude'],
            "country": location.get('country', ''),
            "city": location.get('city', ''),
            "updated_at": now.isoformat(),
        }
        existing = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", user["user_id"]).maybe_single().execute())
        if existing:
            sb.table("user_profiles").update(profile_data).eq("user_id", user["user_id"]).execute()
        else:
            profile_data["user_id"] = user["user_id"]
            sb.table("user_profiles").insert(profile_data).execute()
        return {
            "ok": True,
            "latitude": location['latitude'],
            "longitude": location['longitude'],
            "country": location.get('country'),
            "city": location.get('city'),
            "source": "ip"
        }
    return {"ok": False, "message": "Could not determine location from IP"}

@api_router.get("/location/status")
def get_location_status(user: dict = Depends(get_current_user)):
    profile = _maybe(sb.table("user_profiles").select("gps_latitude,gps_longitude,gps_verified_at,location_source").eq("user_id", user["user_id"]).maybe_single().execute())
    if not profile or profile.get("gps_latitude") is None:
        return {"has_location": False, "needs_location": True, "message": "No GPS location set"}
    gps_age = datetime.now(timezone.utc) - _parse_dt(profile["gps_verified_at"])
    is_stale = gps_age > timedelta(hours=MAX_GPS_AGE_HOURS)
    return {
        "has_location": True,
        "needs_location": is_stale,
        "is_stale": is_stale,
        "location_source": profile.get("location_source", "unknown"),
        "last_updated": profile.get("gps_verified_at"),
        "message": "GPS location expired - please refresh" if is_stale else "GPS location valid"
    }

# ---------- Profile ----------
def get_profile(user: dict) -> dict:
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    if not profile:
        return {
            "user_id": user["user_id"], "email": user.get("email",""), "name": user.get("name",""),
            "date_of_birth": None, "gender": None, "country": None, "city": None,
            "health_status": None, "latitude": None, "longitude": None,
            "display_name": user.get("name",""), "bio": "", "interests": "", "looking_for": "",
            "education": "", "kids": "", "want_kids": "", "smoke": "", "drink": "", "employment": "",
            "profile_image": user.get("picture",""),
            "gallery_images": [],
            "onboarding_complete": False,
            "pref_gender": "", "pref_min_age": 18, "pref_max_age": 99,
            "pref_country": "", "pref_max_distance": 50, "pref_health_status": "",
            "profile_hidden": False, "hide_from_min_age": None, "hide_from_max_age": None,
            "hide_from_health_statuses": "",
            "gps_latitude": None, "gps_longitude": None, "gps_verified_at": None,
            "location_source": "none",
        }
    lat = profile.get("gps_latitude")
    lon = profile.get("gps_longitude")
    country = profile.get("country") if lat is not None else None
    city = profile.get("city") if lat is not None else None
    return {
        "user_id": profile["user_id"], "email": user.get("email",""), "name": user.get("name",""),
        "date_of_birth": profile.get("date_of_birth"), "gender": profile.get("gender"),
        "country": country, "city": city,
        "health_status": profile.get("health_status"),
        "latitude": lat, "longitude": lon,
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
        "profile_hidden": profile.get("profile_hidden", False),
        "hide_from_min_age": profile.get("hide_from_min_age"),
        "hide_from_max_age": profile.get("hide_from_max_age"),
        "hide_from_health_statuses": profile.get("hide_from_health_statuses",""),
        "gps_latitude": lat,
        "gps_longitude": lon,
        "gps_verified_at": profile.get("gps_verified_at"),
        "location_source": profile.get("location_source", "none"),
    }

@api_router.post("/profile/setup")
def setup_profile(payload: ProfileSetupPayload, user: dict = Depends(get_current_user)):
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
        "user_id": user["user_id"],
        "date_of_birth": payload.date_of_birth, "gender": payload.gender,
        "country": country, "city": city,
        "health_status": payload.health_status,
        "latitude": lat, "longitude": lon,
        "display_name": payload.display_name or user.get("name",""),
        "bio": payload.bio or "", "interests": payload.interests or "",
        "looking_for": payload.looking_for or "",
        "education": payload.education or "", "kids": payload.kids or "",
        "want_kids": payload.want_kids or "", "smoke": payload.smoke or "",
        "drink": payload.drink or "", "employment": payload.employment or "",
        "profile_image": profile_image,
        "gallery_images": gallery,
        "pref_gender": payload.pref_gender or "", "pref_min_age": payload.pref_min_age,
        "pref_max_age": payload.pref_max_age, "pref_country": payload.pref_country or "",
        "pref_max_distance": payload.pref_max_distance,
        "pref_health_status": payload.pref_health_status or "",
        "profile_hidden": payload.profile_hidden,
        "hide_from_min_age": payload.hide_from_min_age,
        "hide_from_max_age": payload.hide_from_max_age,
        "hide_from_health_statuses": payload.hide_from_health_statuses or "",
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
async def update_profile(payload: ProfileUpdatePayload, user: dict = Depends(get_current_user)):
    updates = {}
    all_fields = [
        "date_of_birth", "gender", "health_status",
        "display_name", "bio", "interests", "looking_for",
        "education", "kids", "want_kids", "smoke", "drink", "employment",
        "pref_gender", "pref_min_age", "pref_max_age", "pref_country",
        "pref_max_distance", "pref_health_status",
        "profile_hidden", "hide_from_min_age", "hide_from_max_age", "hide_from_health_statuses"
    ]
    for field in all_fields:
        value = getattr(payload, field, None)
        if value is not None: updates[field] = value
    if payload.profile_image is not None:
        updates["profile_image"] = process_image_field(payload.profile_image, user["user_id"], "profile")
    if payload.gallery_images is not None:
        new_gallery = []
        for i, img in enumerate(payload.gallery_images):
            new_gallery.append(process_image_field(img, user["user_id"], f"gallery_{i}"))
        updates["gallery_images"] = new_gallery
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

# ---------- Discovery ----------
@api_router.get("/discover/profiles")
def get_discover_profiles(user: dict = Depends(get_current_user)):
    viewer_profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    if not viewer_profile: return []
    my_lat = viewer_profile.get("gps_latitude") or viewer_profile.get("latitude")
    my_lon = viewer_profile.get("gps_longitude") or viewer_profile.get("longitude")
    if my_lat is None or my_lon is None:
        return []
    pref_gender = viewer_profile.get("pref_gender","")
    pref_min_age = viewer_profile.get("pref_min_age",18)
    pref_max_age = viewer_profile.get("pref_max_age",99)
    pref_country = viewer_profile.get("pref_country","")
    pref_max_distance = viewer_profile.get("pref_max_distance",50)
    pref_health_status = viewer_profile.get("pref_health_status","")
    today = datetime.now(timezone.utc).date()
    matches = sb.table("profile_matches").select("*").or_(f"user1_id.eq.{user['user_id']},user2_id.eq.{user['user_id']}").execute()
    matched_ids = set()
    for m in (matches.data or []):
        partner = m["user2_id"] if m["user1_id"] == user["user_id"] else m["user1_id"]
        matched_ids.add(partner)
    query = sb.table("user_profiles").select("*").neq("user_id", user["user_id"]).eq("onboarding_complete", True)
    query = query.not_.is_("gps_latitude", None)
    for mid in matched_ids:
        query = query.neq("user_id", mid)
    profiles = (query.limit(200).execute()).data or []
    filtered = []
    for p in profiles:
        if p.get("profile_hidden"): continue
        hide_min = p.get("hide_from_min_age")
        hide_max = p.get("hide_from_max_age")
        hide_health = p.get("hide_from_health_statuses","")
        viewer_age = None
        if viewer_profile.get("date_of_birth"):
            try:
                dob = datetime.fromisoformat(str(viewer_profile["date_of_birth"])).date()
                viewer_age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            except: pass
        if viewer_age is not None and ((hide_min and viewer_age < hide_min) or (hide_max and viewer_age > hide_max)):
            continue
        if hide_health and viewer_profile.get("health_status") in [x.strip() for x in hide_health.split(",") if x.strip()]:
            continue
        if pref_gender and p.get("gender") != pref_gender: continue
        age = None
        if p.get("date_of_birth"):
            try:
                dob = datetime.fromisoformat(str(p["date_of_birth"])).date()
                age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            except: pass
        if age is not None and (age < pref_min_age or age > pref_max_age): continue
        if pref_health_status and p.get("health_status") != pref_health_status: continue
        if pref_country and p.get("country") != pref_country: continue
        p_lat = p.get("gps_latitude")
        p_lon = p.get("gps_longitude")
        distance = None
        if p_lat is not None and p_lon is not None:
            distance = haversine(my_lat, my_lon, p_lat, p_lon)
            if pref_max_distance and distance > pref_max_distance:
                continue
        p["distance_km"] = round(distance, 1) if distance is not None else None
        p["age"] = age
        filtered.append(p)
    random.shuffle(filtered)
    return filtered[:50]

@api_router.post("/discover/swipe")
def swipe_profile(payload: SwipePayload, user: dict = Depends(get_current_user)):
    if payload.direction not in ["like","pass"]: raise HTTPException(400)
    target = _maybe(sb.table("user_profiles").select("user_id").eq("user_id", payload.swiped_id).maybe_single().execute())
    if not target: raise HTTPException(404)
    existing = _maybe(sb.table("profile_swipes").select("*").eq("swiper_id", user["user_id"]).eq("swiped_id", payload.swiped_id).eq("swipe_type", payload.swipe_type).maybe_single().execute())
    if not existing:
        try:
            sb.table("profile_swipes").insert({
                "swipe_id": f"swp_{uuid.uuid4().hex[:12]}",
                "swiper_id": user["user_id"],
                "swiped_id": payload.swiped_id,
                "direction": payload.direction,
                "swipe_type": payload.swipe_type
            }).execute()
        except Exception:
            # Duplicate swipe – just ignore it
            pass

    matched = False
    match_id = None
    if payload.direction == "like":
        from_profile = get_profile(user)
        if payload.swipe_type == "dating":
            existing_req = _maybe(sb.table("dating_requests").select("*").eq("from_user_id", user["user_id"]).eq("to_user_id", payload.swiped_id).maybe_single().execute())
            if not existing_req:
                sb.table("dating_requests").insert({"request_id": f"dr_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": payload.swiped_id, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
                sb.table("notifications").insert({
                    "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
                    "user_id": payload.swiped_id,
                    "from_user_id": user["user_id"],
                    "type": "dating_request",
                    "message": f"{from_profile.get('display_name', 'Someone')} sent you a dating request",
                    "created_at": datetime.now(timezone.utc).isoformat()
                }).execute()
        elif payload.swipe_type == "friendship":
            existing_req = _maybe(sb.table("friend_requests").select("*").eq("from_user_id", user["user_id"]).eq("to_user_id", payload.swiped_id).maybe_single().execute())
            if not existing_req:
                sb.table("friend_requests").insert({"request_id": f"fr_{uuid.uuid4().hex[:12]}", "from_user_id": user["user_id"], "to_user_id": payload.swiped_id, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
                sb.table("notifications").insert({
                    "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
                    "user_id": payload.swiped_id,
                    "from_user_id": user["user_id"],
                    "type": "friend_request",
                    "message": f"{from_profile.get('display_name', 'Someone')} sent you a friend request",
                    "created_at": datetime.now(timezone.utc).isoformat()
                }).execute()
        other = _maybe(sb.table("profile_swipes").select("*").eq("swiper_id", payload.swiped_id).eq("swiped_id", user["user_id"]).eq("direction","like").eq("swipe_type", payload.swipe_type).maybe_single().execute())
        if other:
            uid1, uid2 = sorted([user["user_id"], payload.swiped_id])
            exist_match = _maybe(sb.table("profile_matches").select("*").eq("user1_id", uid1).eq("user2_id", uid2).eq("swipe_type", payload.swipe_type).maybe_single().execute())
            if not exist_match:
                match_id = f"match_{uuid.uuid4().hex[:12]}"
                sb.table("profile_matches").insert({"match_id": match_id, "user1_id": uid1, "user2_id": uid2, "swipe_type": payload.swipe_type, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
                matched = True
                sb.table("notifications").insert({
                    "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
                    "user_id": payload.swiped_id,
                    "from_user_id": user["user_id"],
                    "type": "match_new",
                    "message": f"You matched with {from_profile.get('display_name', 'Someone')}!",
                    "created_at": datetime.now(timezone.utc).isoformat()
                }).execute()
                sb.table("notifications").insert({
                    "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
                    "user_id": user["user_id"],
                    "from_user_id": payload.swiped_id,
                    "type": "match_new",
                    "message": f"You matched with {from_profile.get('display_name', 'Someone')}!",
                    "created_at": datetime.now(timezone.utc).isoformat()
                }).execute()
            else:
                match_id = exist_match["match_id"]
    return {"ok": True, "matched": matched, "match_id": match_id, "direction": payload.direction}

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
                "created_at": m["created_at"],
                "unread_count": unread
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
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match: raise HTTPException(404)
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]: raise HTTPException(403)
    msg = {"message_id": f"msg_{uuid.uuid4().hex[:12]}", "match_id": match_id, "sender_id": user["user_id"], "content": payload.content, "read": False, "created_at": datetime.now(timezone.utc).isoformat()}
    sb.table("match_messages").insert(msg).execute()
    other_id = match["user2_id"] if match["user1_id"] == user["user_id"] else match["user1_id"]
    from_profile = get_profile(user)
    sb.table("notifications").insert({
        "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
        "user_id": other_id,
        "from_user_id": user["user_id"],
        "type": "match_message",
        "message": f"New message from {from_profile.get('display_name', 'Someone')}",
        "created_at": datetime.now(timezone.utc).isoformat()
    }).execute()
    return {"ok": True, "message": msg}

@api_router.delete("/discover/matches/{match_id}")
def unmatch(match_id: str, user: dict = Depends(get_current_user)):
    match = _maybe(sb.table("profile_matches").select("*").eq("match_id", match_id).maybe_single().execute())
    if not match:
        raise HTTPException(404, "Match not found")
    if user["user_id"] not in [match["user1_id"], match["user2_id"]]:
        raise HTTPException(403, "Not your match")
    sb.table("match_messages").delete().eq("match_id", match_id).execute()
    sb.table("profile_matches").delete().eq("match_id", match_id).execute()
    uid1, uid2 = match["user1_id"], match["user2_id"]
    sb.table("dating_requests").delete() \
        .or_(f"from_user_id.eq.{uid1},to_user_id.eq.{uid1}") \
        .or_(f"from_user_id.eq.{uid2},to_user_id.eq.{uid2}") \
        .execute()
    sb.table("friend_requests").delete() \
        .or_(f"from_user_id.eq.{uid1},to_user_id.eq.{uid1}") \
        .or_(f"from_user_id.eq.{uid2},to_user_id.eq.{uid2}") \
        .execute()
    sb.table("profile_swipes").delete() \
        .or_(f"swiper_id.eq.{uid1},swiped_id.eq.{uid1}") \
        .or_(f"swiper_id.eq.{uid2},swiped_id.eq.{uid2}") \
        .execute()
    return {"ok": True}

@api_router.get("/unread-counts")
def get_unread_counts(user: dict = Depends(get_current_user)):
    unread_res = sb.table("match_messages") \
        .select("match_id") \
        .neq("sender_id", user["user_id"]) \
        .eq("read", False) \
        .execute()
    match_ids = set()
    for row in (unread_res.data or []):
        match_ids.add(row["match_id"])
    dating_count = 0
    friend_count = 0
    if match_ids:
        for mid in match_ids:
            match = _maybe(sb.table("profile_matches").select("swipe_type").eq("match_id", mid).maybe_single().execute())
            if match:
                if match.get("swipe_type") == "friendship":
                    friend_count += 1
                else:
                    dating_count += 1
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
                "request_id": req["request_id"],
                "type": "dating",
                "from_user_id": req["from_user_id"],
                "from_name": from_profile.get("display_name","Someone"),
                "from_image": from_profile.get("profile_image",""),
                "from_country": from_profile.get("country",""),
                "created_at": req["created_at"],
                "status": req["status"],
            })
    for req in friend:
        from_profile = _maybe(sb.table("user_profiles").select("display_name,profile_image,country").eq("user_id", req["from_user_id"]).maybe_single().execute())
        if from_profile:
            result.append({
                "request_id": req["request_id"],
                "type": "friend",
                "from_user_id": req["from_user_id"],
                "from_name": from_profile.get("display_name","Someone"),
                "from_image": from_profile.get("profile_image",""),
                "from_country": from_profile.get("country",""),
                "created_at": req["created_at"],
                "status": req["status"],
            })
    return result

@api_router.post("/requests/{request_id}/respond")
def respond_request(request_id: str, action: str, user: dict = Depends(get_current_user)):
    if action not in ["accept","reject"]:
        raise HTTPException(400, "Action must be 'accept' or 'reject'")
    req = _maybe(sb.table("dating_requests").select("*").eq("request_id", request_id).eq("to_user_id", user["user_id"]).maybe_single().execute())
    table = "dating_requests"
    if not req:
        req = _maybe(sb.table("friend_requests").select("*").eq("request_id", request_id).eq("to_user_id", user["user_id"]).maybe_single().execute())
        table = "friend_requests"
    if not req:
        raise HTTPException(404, "Request not found")
    new_status = "accepted" if action == "accept" else "rejected"
    if req["status"] != "pending":
        raise HTTPException(400, "Request already handled")
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
    sb.table("notifications").insert({
        "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
        "user_id": req["from_user_id"],
        "from_user_id": user["user_id"],
        "type": notif_type,
        "message": f"{from_profile.get('display_name','Someone')} {action}ed your request",
        "created_at": datetime.now(timezone.utc).isoformat()
    }).execute()
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

# ---------- Cron: Send reminders to inactive users ----------
@api_router.post("/cron/send-reminders")
def send_inactive_reminders():
    now = datetime.now(timezone.utc)
    # TESTING: use 2 minutes instead of 5 days
    two_minutes_ago = now - timedelta(minutes=2)

    # Find users whose last login is before 2 minutes ago
    # and haven't been reminded in that window
    users = sb.table("users").select("user_id,email,name") \
        .lt("last_login", two_minutes_ago) \
        .or_("last_reminder_sent.is.null,last_reminder_sent.lt.{}".format(two_minutes_ago.isoformat())) \
        .execute()

    sent_count = 0
    for u in (users.data or []):
        email = u.get("email")
        if not email:
            continue
        try:
            send_reminder_email(email, u.get("name", "there"))
            # Mark reminder as sent
            sb.table("users").update({"last_reminder_sent": now.isoformat()}).eq("user_id", u["user_id"]).execute()
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to send reminder to {email}: {e}")

    return {"ok": True, "reminders_sent": sent_count}

# ---------- Stories ----------
@api_router.post("/stories")
def create_story(payload: CreateStoryPayload, user: dict = Depends(get_current_user)):
    if payload.category not in ["HIV","HPV","HSV"]: raise HTTPException(400)
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    author_avatar = profile.get("profile_image") if profile else user.get("picture","")
    story = {"story_id": f"story_{uuid.uuid4().hex[:12]}", "user_id": user["user_id"],
             "author_name": profile.get("display_name", user.get("name","")),
             "author_avatar": author_avatar,
             "content": payload.content, "category": payload.category, "title": payload.title or "",
             "likes": 0, "comment_count": 0, "created_at": datetime.now(timezone.utc).isoformat()}
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
    story = _maybe(sb.table("stories").select("story_id,comment_count").eq("story_id", story_id).maybe_single().execute())
    if not story: raise HTTPException(404)
    if payload.parent_id:
        parent = _maybe(sb.table("story_comments").select("comment_id").eq("comment_id", payload.parent_id).maybe_single().execute())
        if not parent: raise HTTPException(404)
    profile = _maybe(sb.table("user_profiles").select("*").eq("user_id", user["user_id"]).maybe_single().execute())
    author_avatar = profile.get("profile_image") if profile else user.get("picture","")
    comment = {"comment_id": f"cmt_{uuid.uuid4().hex[:12]}", "story_id": story_id, "user_id": user["user_id"],
               "author_name": profile.get("display_name", user.get("name","")),
               "author_avatar": author_avatar,
               "content": payload.content, "parent_id": payload.parent_id, "likes": 0, "reply_count": 0,
               "created_at": datetime.now(timezone.utc).isoformat()}
    sb.table("story_comments").insert(comment).execute()
    sb.table("stories").update({"comment_count": story.get("comment_count",0)+1}).eq("story_id", story_id).execute()
    if payload.parent_id:
        pc = _maybe(sb.table("story_comments").select("reply_count").eq("comment_id", payload.parent_id).maybe_single().execute())
        if pc: sb.table("story_comments").update({"reply_count": pc.get("reply_count",0)+1}).eq("comment_id", payload.parent_id).execute()
    return {"ok": True, "comment": comment}

def build_comment_tree(comments):
    cmap = {c["comment_id"]: {**c, "replies": []} for c in comments}
    roots = []
    for c in comments:
        node = cmap[c["comment_id"]]
        if c.get("parent_id") and c["parent_id"] in cmap: cmap[c["parent_id"]]["replies"].append(node)
        else: roots.append(node)
    return roots

# ---------- Countries/Cities ----------
@api_router.get("/location/countries")
def get_countries():
    try:
        resp = httpx.get("https://restcountries.com/v3.1/all?fields=name,cca2", timeout=5)
        if resp.status_code == 200:
            return [{"code": c["cca2"], "name": c["name"]["common"]} for c in resp.json()]
    except: pass
    return [
        {"code":"ZA","name":"South Africa"}, {"code":"US","name":"United States"},
        {"code":"GB","name":"United Kingdom"}, {"code":"CA","name":"Canada"},
        {"code":"AU","name":"Australia"}, {"code":"IN","name":"India"},
    ]

@api_router.get("/location/cities")
def get_cities(country: str):
    try:
        resp = httpx.post("https://countriesnow.space/api/v0.1/countries/cities", json={"country": country}, timeout=5)
        if resp.status_code == 200 and not resp.json().get("error"):
            return [{"name": c} for c in resp.json().get("data", [])]
    except: pass
    fallback = {"South Africa": ["Johannesburg","Cape Town","Durban"], "United States": ["New York","Los Angeles","Chicago"]}
    return [{"name": c} for c in fallback.get(country, [])]

app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))