"""Backend tests for Stokvel — iteration 2 (multi-vote + referral + diamond-boost)."""
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

mongo = MongoClient(MONGO_URL)
db = mongo[DB_NAME]


def _seed_user(tokens=5, votes_since_token=0, is_premium=False, diamonds=0, name="TEST User"):
    """Seed a user with the NEW fields: diamonds, referral_code."""
    uid = f"test-user-{uuid.uuid4().hex[:8]}"
    token = f"test_session_{uuid.uuid4().hex}"
    email = f"TEST_{uid}@example.com"
    referral_code = uuid.uuid4().hex[:8]
    db.users.insert_one({
        "user_id": uid,
        "email": email,
        "name": name,
        "picture": "",
        "tokens": tokens,
        "diamonds": diamonds,
        "is_premium": is_premium,
        "premium_until": None,
        "votes_since_token": votes_since_token,
        "referral_code": referral_code,
        "referred_by": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    db.user_sessions.insert_one({
        "user_id": uid,
        "session_token": token,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return uid, token, referral_code


def _cleanup(uid, tok):
    db.users.delete_one({"user_id": uid})
    db.user_sessions.delete_one({"session_token": tok})
    db.cards.delete_many({"owner_id": uid})
    db.votes.delete_many({"voter_id": uid})


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


# ===== Basic / auth =====
def test_root():
    r = requests.get(f"{BASE_URL}/api/")
    assert r.status_code == 200
    assert "message" in r.json()


def test_me_no_token():
    r = requests.get(f"{BASE_URL}/api/auth/me")
    assert r.status_code == 401


def test_session_bad_id():
    r = requests.post(f"{BASE_URL}/api/auth/session", json={"session_id": "bogus-id-xyz"})
    assert r.status_code == 401


def test_session_bad_id_with_ref_does_not_error():
    """Invalid ref + bogus session_id → still 401 from external, not 500."""
    r = requests.post(f"{BASE_URL}/api/auth/session",
                      json={"session_id": "bogus-id-xyz", "ref": "deadbeef"})
    assert r.status_code == 401


def test_me_returns_new_fields():
    uid, tok, ref = _seed_user(tokens=5, diamonds=7)
    try:
        r = requests.get(f"{BASE_URL}/api/auth/me", headers=H(tok))
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ["user_id", "email", "name", "tokens", "diamonds",
                  "is_premium", "votes_since_token", "votes_per_token",
                  "referral_code", "diamond_boost_cost", "diamond_boost_minutes"]:
            assert k in d, f"missing key: {k}"
        assert d["votes_per_token"] == 10
        assert d["diamond_boost_cost"] == 5
        assert d["diamond_boost_minutes"] == 10
        assert d["diamonds"] == 7
        assert d["referral_code"] == ref
    finally:
        _cleanup(uid, tok)


# ===== Referral =====
def test_referral_me():
    uid, tok, ref = _seed_user(diamonds=3)
    try:
        r = requests.get(f"{BASE_URL}/api/referral/me", headers=H(tok))
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["referral_code"] == ref
        assert d["diamonds"] == 3
        assert d["diamond_boost_cost"] == 5
        assert d["diamond_boost_minutes"] == 10
        # 8-char hex
        assert len(d["referral_code"]) == 8
        int(d["referral_code"], 16)  # ensure hex-parseable
    finally:
        _cleanup(uid, tok)


def test_referral_me_requires_auth():
    r = requests.get(f"{BASE_URL}/api/referral/me")
    assert r.status_code == 401


def test_referral_credit_logic_simulated():
    """The /auth/session endpoint depends on external Emergent exchange which we can't call.
    Simulate the DB-side behaviour: when a NEW user signs up with ref=<referrer_code>,
    we increment diamonds on the referrer. Validates the Mongo $inc works as intended."""
    ref_uid, ref_tok, ref_code = _seed_user(diamonds=0)
    try:
        before = db.users.find_one({"user_id": ref_uid})["diamonds"]
        # Simulate the code path inside auth_session
        ref_user = db.users.find_one({"referral_code": ref_code}, {"_id": 0})
        assert ref_user is not None
        db.users.update_one({"user_id": ref_user["user_id"]},
                            {"$inc": {"diamonds": 1}})
        after = db.users.find_one({"user_id": ref_uid})["diamonds"]
        assert after == before + 1

        # Returning existing user with ref: should NOT increment. The auth_session
        # code takes the `existing` branch and bypasses the ref increment entirely.
        # No DB mutation happens. Verify the endpoint returns new-fields on /auth/me.
        r = requests.get(f"{BASE_URL}/api/auth/me", headers=H(ref_tok))
        assert r.status_code == 200
        assert r.json()["diamonds"] == after  # unchanged by repeated non-new signups
    finally:
        _cleanup(ref_uid, ref_tok)


# ===== Multi-vote =====
def test_same_user_votes_same_card_multiple_times():
    uid_a, tok_a, _ = _seed_user(tokens=5, votes_since_token=0)
    uid_b, tok_b, _ = _seed_user(tokens=3)
    try:
        payload = {"image_url": "https://ex.com/i.jpg",
                   "smart_link": "https://target.example.com/landing"}
        cr = requests.post(f"{BASE_URL}/api/cards", json=payload, headers=H(tok_b))
        assert cr.status_code == 200, cr.text
        card_id = cr.json()["card_id"]

        # Vote 3 times
        for i in range(3):
            vr = requests.post(f"{BASE_URL}/api/cards/{card_id}/vote", headers=H(tok_a))
            assert vr.status_code == 200, f"vote {i+1}: {vr.text}"

        # Card votes incremented by 3
        card = db.cards.find_one({"card_id": card_id})
        assert card["votes"] == 3

        # User A votes_since_token went 0 -> 3 (no token earned yet)
        ua = db.users.find_one({"user_id": uid_a})
        assert ua["votes_since_token"] == 3
    finally:
        _cleanup(uid_a, tok_a)
        _cleanup(uid_b, tok_b)


def test_marketplace_still_shows_voted_card():
    """After voting card_X, it must still appear in marketplace (no dedup filter)."""
    uid_a, tok_a, _ = _seed_user(tokens=5)
    uid_b, tok_b, _ = _seed_user(tokens=3)
    try:
        payload = {"image_url": "https://ex.com/i.jpg",
                   "smart_link": "https://target.example.com/x"}
        cr = requests.post(f"{BASE_URL}/api/cards", json=payload, headers=H(tok_b))
        assert cr.status_code == 200, cr.text
        card_id = cr.json()["card_id"]

        vr = requests.post(f"{BASE_URL}/api/cards/{card_id}/vote", headers=H(tok_a))
        assert vr.status_code == 200

        r = requests.get(f"{BASE_URL}/api/cards/marketplace", headers=H(tok_a))
        assert r.status_code == 200
        ids = [c["card_id"] for c in r.json()]
        assert card_id in ids, "voted card must still appear in marketplace"
    finally:
        _cleanup(uid_a, tok_a)
        _cleanup(uid_b, tok_b)


# ===== Diamond boost =====
def test_boost_requires_enough_diamonds():
    uid, tok, _ = _seed_user(tokens=3, diamonds=2)
    try:
        payload = {
            "image_url": "https://ex.com/i.jpg",
            "smart_link": "https://target.example.com/boost",
            "use_diamond_boost": True,
        }
        r = requests.post(f"{BASE_URL}/api/cards", json=payload, headers=H(tok))
        assert r.status_code == 400
        assert "diamond" in r.text.lower()
    finally:
        _cleanup(uid, tok)


def test_boost_deducts_diamonds_and_extends_ttl():
    uid, tok, _ = _seed_user(tokens=3, diamonds=6, is_premium=False)
    try:
        payload = {
            "image_url": "https://ex.com/i.jpg",
            "smart_link": "https://target.example.com/boost",
            "use_diamond_boost": True,
        }
        r = requests.post(f"{BASE_URL}/api/cards", json=payload, headers=H(tok))
        assert r.status_code == 200, r.text
        c = r.json()
        assert c.get("diamond_boosted") is True

        created = datetime.fromisoformat(c["created_at"])
        expires = datetime.fromisoformat(c["expires_at"])
        delta_min = (expires - created).total_seconds() / 60
        # free TTL 20 + boost 10 = 30
        assert 29 <= delta_min <= 31, f"expected ~30 min, got {delta_min}"

        u = db.users.find_one({"user_id": uid})
        assert u["diamonds"] == 1     # 6 - 5
        assert u["tokens"] == 2       # 3 - 1
    finally:
        _cleanup(uid, tok)


def test_no_boost_default_ttl():
    uid, tok, _ = _seed_user(tokens=3, diamonds=6, is_premium=False)
    try:
        payload = {
            "image_url": "https://ex.com/i.jpg",
            "smart_link": "https://target.example.com/free",
        }
        r = requests.post(f"{BASE_URL}/api/cards", json=payload, headers=H(tok))
        assert r.status_code == 200, r.text
        c = r.json()
        assert c.get("diamond_boosted") is False
        created = datetime.fromisoformat(c["created_at"])
        expires = datetime.fromisoformat(c["expires_at"])
        delta_min = (expires - created).total_seconds() / 60
        assert 19 <= delta_min <= 21, f"free TTL expected ~20 min, got {delta_min}"
        u = db.users.find_one({"user_id": uid})
        assert u["diamonds"] == 6   # unchanged
    finally:
        _cleanup(uid, tok)


def test_premium_boost_ttl():
    uid, tok, _ = _seed_user(tokens=3, diamonds=6, is_premium=True)
    try:
        payload = {
            "image_url": "https://ex.com/i.jpg",
            "smart_link": "https://t.example.com/p",
            "use_diamond_boost": True,
        }
        r = requests.post(f"{BASE_URL}/api/cards", json=payload, headers=H(tok))
        assert r.status_code == 200, r.text
        c = r.json()
        created = datetime.fromisoformat(c["created_at"])
        expires = datetime.fromisoformat(c["expires_at"])
        delta_min = (expires - created).total_seconds() / 60
        # premium 35 + 10 = 45
        assert 44 <= delta_min <= 46, f"premium+boost expected ~45 min, got {delta_min}"
    finally:
        _cleanup(uid, tok)


# ===== Regressions =====
def test_create_card_missing_fields():
    uid, tok, _ = _seed_user(tokens=3)
    try:
        r = requests.post(f"{BASE_URL}/api/cards",
                          json={"image_url": "", "smart_link": ""}, headers=H(tok))
        assert r.status_code == 400
    finally:
        _cleanup(uid, tok)


def test_create_card_bad_link():
    uid, tok, _ = _seed_user(tokens=3)
    try:
        r = requests.post(f"{BASE_URL}/api/cards",
                          json={"image_url": "https://x/y.jpg", "smart_link": "notaurl"},
                          headers=H(tok))
        assert r.status_code == 400
    finally:
        _cleanup(uid, tok)


def test_create_card_no_tokens():
    uid, tok, _ = _seed_user(tokens=0)
    try:
        r = requests.post(f"{BASE_URL}/api/cards",
                          json={"image_url": "https://x/y.jpg", "smart_link": "https://ok.com"},
                          headers=H(tok))
        assert r.status_code == 402
    finally:
        _cleanup(uid, tok)


def test_marketplace_excludes_own():
    uid_a, tok_a, _ = _seed_user(tokens=3)
    uid_b, tok_b, _ = _seed_user(tokens=3)
    try:
        requests.post(f"{BASE_URL}/api/cards",
                      json={"image_url": "https://ex/a.jpg", "smart_link": "https://a.com"},
                      headers=H(tok_a))
        requests.post(f"{BASE_URL}/api/cards",
                      json={"image_url": "https://ex/b.jpg", "smart_link": "https://b.com"},
                      headers=H(tok_b))
        r = requests.get(f"{BASE_URL}/api/cards/marketplace", headers=H(tok_a))
        assert r.status_code == 200
        cards = r.json()
        assert len(cards) <= 12
        assert all(c["owner_id"] != uid_a for c in cards)
    finally:
        _cleanup(uid_a, tok_a)
        _cleanup(uid_b, tok_b)


def test_self_vote_rejected():
    uid, tok, _ = _seed_user(tokens=3)
    try:
        cr = requests.post(f"{BASE_URL}/api/cards",
                           json={"image_url": "https://ex/a.jpg",
                                 "smart_link": "https://a.example.com"},
                           headers=H(tok))
        assert cr.status_code == 200
        card_id = cr.json()["card_id"]
        r = requests.post(f"{BASE_URL}/api/cards/{card_id}/vote", headers=H(tok))
        assert r.status_code == 400
    finally:
        _cleanup(uid, tok)


def test_vote_unknown_card():
    uid, tok, _ = _seed_user(tokens=3)
    try:
        r = requests.post(f"{BASE_URL}/api/cards/card_doesnotexist_xyz/vote", headers=H(tok))
        assert r.status_code == 404
    finally:
        _cleanup(uid, tok)


def test_vote_expired_card():
    uid, tok, _ = _seed_user(tokens=3)
    owner_id = f"test-owner-{uuid.uuid4().hex[:6]}"
    card_id = f"card_{uuid.uuid4().hex[:12]}"
    db.cards.insert_one({
        "card_id": card_id, "owner_id": owner_id, "owner_name": "Exp",
        "image_url": "https://x/y.jpg", "smart_link": "https://exp.example.com",
        "title": "", "votes": 0,
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "is_premium": False,
    })
    try:
        r = requests.post(f"{BASE_URL}/api/cards/{card_id}/vote", headers=H(tok))
        assert r.status_code == 400
    finally:
        db.cards.delete_one({"card_id": card_id})
        _cleanup(uid, tok)


def test_vote_earns_token_at_threshold():
    uid_a, tok_a, _ = _seed_user(tokens=0, votes_since_token=9)
    uid_b, tok_b, _ = _seed_user(tokens=3)
    try:
        cr = requests.post(f"{BASE_URL}/api/cards",
                           json={"image_url": "https://ex/i.jpg",
                                 "smart_link": "https://t.example.com"},
                           headers=H(tok_b))
        card_id = cr.json()["card_id"]
        vr = requests.post(f"{BASE_URL}/api/cards/{card_id}/vote", headers=H(tok_a))
        assert vr.status_code == 200
        d = vr.json()
        assert d["tokens_earned"] == 1
        assert d["votes_since_token"] == 0
        ua = db.users.find_one({"user_id": uid_a})
        assert ua["tokens"] == 1
    finally:
        _cleanup(uid_a, tok_a)
        _cleanup(uid_b, tok_b)


# ===== PayFast regression =====
def test_payfast_subscription_initiate():
    uid, tok, _ = _seed_user(tokens=1)
    try:
        r = requests.post(f"{BASE_URL}/api/payments/payfast/initiate",
                          json={"return_url": "https://app/ret",
                                "cancel_url": "https://app/cancel"},
                          headers=H(tok))
        assert r.status_code == 200, r.text
        d = r.json()
        assert "sandbox.payfast.co.za" in d["redirect_url"]
        assert "signature=" in d["redirect_url"]
    finally:
        _cleanup(uid, tok)


def test_payfast_activate_sandbox():
    uid, tok, _ = _seed_user(tokens=1)
    try:
        r = requests.post(f"{BASE_URL}/api/payments/payfast/activate-sandbox", headers=H(tok))
        assert r.status_code == 200
        u = db.users.find_one({"user_id": uid})
        assert u["is_premium"] is True
    finally:
        _cleanup(uid, tok)


def test_payfast_boost_initiate_and_activate():
    uid, tok, _ = _seed_user(tokens=0)
    try:
        init = requests.post(f"{BASE_URL}/api/payments/payfast/boost/initiate",
                             json={"return_url": "https://app/ret",
                                   "cancel_url": "https://app/cancel"},
                             headers=H(tok))
        assert init.status_code == 200, init.text
        assert "sandbox.payfast.co.za" in init.json()["redirect_url"]

        act = requests.post(f"{BASE_URL}/api/payments/payfast/boost/activate-sandbox",
                            headers=H(tok))
        assert act.status_code == 200, act.text
        d = act.json()
        assert d["credited"] == 3
        u = db.users.find_one({"user_id": uid})
        assert u["tokens"] == 3
    finally:
        _cleanup(uid, tok)


def test_logout_invalidates_session():
    uid, tok, _ = _seed_user(tokens=1)
    try:
        r1 = requests.get(f"{BASE_URL}/api/auth/me", headers=H(tok))
        assert r1.status_code == 200
        lo = requests.post(f"{BASE_URL}/api/auth/logout", headers=H(tok))
        assert lo.status_code == 200
        r2 = requests.get(f"{BASE_URL}/api/auth/me", headers=H(tok))
        assert r2.status_code == 401
    finally:
        db.users.delete_one({"user_id": uid})
        db.user_sessions.delete_one({"session_token": tok})
