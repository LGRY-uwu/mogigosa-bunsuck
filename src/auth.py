from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from src.config import _safe_get_secret, resolve_admin_config
from src.models import TierType, UserRecord
from src.storage import StorageManager

KST = timezone(timedelta(hours=9))
FREE_DAILY_LIMIT = 5
VALID_TIERS: tuple[TierType, ...] = ("FREE", "CUSTOM", "PRO", "ADMIN")
TIER_LABELS: dict[TierType, str] = {
    "FREE": "무료 회원",
    "CUSTOM": "개인 키 모드",
    "PRO": "VIP 회원",
    "ADMIN": "최고 관리자",
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,30}$")


def get_kst_today_str() -> str:
    """한국 표준시(KST) 기준 오늘 날짜 YYYY-MM-DD를 반환한다."""
    return datetime.now(KST).strftime("%Y-%m-%d")


def hash_password(password: str, salt: str | None = None) -> str:
    """SHA-256 + random salt로 비밀번호를 안전하게 해싱한다."""
    actual_salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256(f"{actual_salt}:{password}".encode("utf-8")).hexdigest()
    return f"{actual_salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    """저장된 salt$sha256 해시와 입력 비밀번호를 상수 시간 비교로 검증한다."""
    if "$" not in stored_hash:
        return False
    salt, expected_digest = stored_hash.split("$", 1)
    candidate_digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate_digest, expected_digest)


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def mask_api_key(api_key: str) -> str:
    """API 키 전체를 노출하지 않고 앞 6자리 + 마스킹 기호로 반환한다."""
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 6:
        return key[:2] + "••••••••"
    return f"{key[:6]}••••••••"


SESSION_TOKEN_TTL_SECONDS: int = 7 * 24 * 3600


def _get_session_signing_key(storage: StorageManager | None = None) -> bytes:
    """새로고침 이후에도 유지되는 서명용 비밀키를 결정론적으로 로드한다."""
    parts: list[str] = []
    for key_name in ("SESSION_SECRET", "ADMIN_PASSWORD", "GEMINI_API_KEY", "GOOGLE_DRIVE_FOLDER_ID"):
        sec_val = _safe_get_secret([key_name, key_name.lower()])
        if isinstance(sec_val, str) and sec_val.strip():
            parts.append(f"{key_name}:{sec_val.strip()}")
        elif os.environ.get(key_name, "").strip():
            parts.append(f"{key_name}:{os.environ[key_name].strip()}")

    if parts:
        seed = "|".join(parts)
        return hashlib.sha256(f"mogigosa-session-v1:{seed}".encode("utf-8")).digest()

    if storage is not None:
        secret_file = storage.local_root / ".session_secret"
        try:
            if secret_file.exists():
                existing = secret_file.read_text(encoding="utf-8").strip()
                if existing:
                    return hashlib.sha256(existing.encode("utf-8")).digest()
            generated = secrets.token_hex(32)
            secret_file.write_text(generated, encoding="utf-8")
            return hashlib.sha256(generated.encode("utf-8")).digest()
        except Exception:
            pass

    return hashlib.sha256(b"mogigosa-local-fallback-secret").digest()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def create_session_token(
    username: str,
    storage: StorageManager | None = None,
    ttl_seconds: int = SESSION_TOKEN_TTL_SECONDS,
) -> str:
    """브라우저 새로고침 시 로그인 상태를 안전하게 복원하기 위한 HMAC 서명 세션 토큰을 생성한다."""
    norm = normalize_username(username)
    payload = {"u": norm, "exp": int(time.time()) + max(60, int(ttl_seconds))}
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    sig = hmac.new(
        _get_session_signing_key(storage),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_session_token(token: str, storage: StorageManager | None = None) -> str | None:
    """HMAC 서명 세션 토큰을 검증하고 유효한 경우 정규화된 username을 반환한다."""
    raw = (token or "").strip()
    if "." not in raw:
        return None
    payload_b64, sig_b64 = raw.split(".", 1)
    if not payload_b64 or not sig_b64:
        return None

    expected_sig = hmac.new(
        _get_session_signing_key(storage),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(actual_sig, expected_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    username = normalize_username(str(payload.get("u", "")))
    exp = payload.get("exp")
    if not username or not isinstance(exp, (int, float)):
        return None
    if time.time() > float(exp):
        return None
    return username


def _sync_user_state(user: UserRecord, today: str, admin_usernames: set[str]) -> bool:
    """자정(KST) 초기화 및 관리자 아이디 자동 ADMIN 승급을 동기화한다."""
    changed = False
    if user.last_used_date != today:
        user.daily_count = 0
        user.last_used_date = today
        changed = True
    if user.username in admin_usernames and user.tier != "ADMIN":
        user.tier = "ADMIN"
        changed = True
    return changed


def get_user_by_username(storage: StorageManager, username: str) -> UserRecord | None:
    norm = normalize_username(username)
    users = storage.load_users()
    today = get_kst_today_str()
    admin_names = resolve_admin_config().admin_usernames
    changed = False
    found: UserRecord | None = None

    for u in users:
        if u.username == norm:
            if _sync_user_state(u, today, admin_names):
                changed = True
            found = u
            break

    if changed:
        storage.save_users(users)
    return found


def register_user(
    storage: StorageManager,
    username: str,
    nickname: str,
    password: str,
) -> tuple[bool, str, UserRecord | None]:
    raw_id = (username or "").strip()
    if not USERNAME_RE.match(raw_id):
        return (
            False,
            "아이디는 3~30자의 영문, 숫자, 밑줄(_), 마침표(.), 하이픈(-)만 사용할 수 있습니다.",
            None,
        )

    clean_nick = (nickname or "").strip()
    if not (1 <= len(clean_nick) <= 20):
        return False, "닉네임은 1~20자 이내로 입력해 주세요.", None

    if len(password or "") < 4:
        return False, "비밀번호는 최소 4자 이상이어야 합니다.", None

    norm_id = normalize_username(raw_id)
    users = storage.load_users()
    if any(u.username == norm_id for u in users):
        return False, "이미 사용 중인 아이디입니다. 다른 아이디를 입력해 주세요.", None

    today = get_kst_today_str()
    admin_cfg = resolve_admin_config()
    initial_tier: TierType = "ADMIN" if norm_id in admin_cfg.admin_usernames else "FREE"
    new_user = UserRecord(
        username=norm_id,
        nickname=clean_nick,
        password_hash=hash_password(password),
        tier=initial_tier,
        custom_api_key="",
        daily_count=0,
        last_used_date=today,
        created_at=datetime.now(KST).isoformat(timespec="seconds"),
    )
    users.append(new_user)
    err = storage.save_users(users)
    tier_desc = TIER_LABELS[initial_tier]
    if err:
        return True, f"회원가입 완료! ({err})", new_user
    return True, f"환영합니다, {clean_nick}님! (등급: {tier_desc} · {initial_tier})", new_user


def authenticate_user(
    storage: StorageManager,
    username: str,
    password: str,
) -> tuple[bool, str, UserRecord | None]:
    norm_id = normalize_username(username)
    if not norm_id or not password:
        return False, "아이디와 비밀번호를 모두 입력해 주세요.", None

    users = storage.load_users()
    today = get_kst_today_str()
    admin_names = resolve_admin_config().admin_usernames
    for u in users:
        if u.username == norm_id:
            if not verify_password(password, u.password_hash):
                return False, "비밀번호가 일치하지 않습니다.", None
            if _sync_user_state(u, today, admin_names):
                storage.save_users(users)
            return True, f"{u.nickname}님, 로그인되었습니다.", u

    return False, "등록되지 않은 아이디입니다. 회원가입 탭에서 가입해 주세요.", None


def save_user_custom_api_key(
    storage: StorageManager,
    username: str,
    api_key: str,
) -> tuple[bool, str, UserRecord | None]:
    """개인 Gemini API 키를 저장하고 즉시 CUSTOM 등급으로 전환한다."""
    cleaned_key = (api_key or "").strip()
    if len(cleaned_key) < 12 or " " in cleaned_key:
        return False, "올바른 형식의 Gemini API 키(`AIza...`)를 입력해 주세요.", None

    norm_id = normalize_username(username)
    users = storage.load_users()
    target: UserRecord | None = None

    for u in users:
        if u.username == norm_id:
            u.custom_api_key = cleaned_key
            if u.tier != "ADMIN":
                u.tier = "CUSTOM"
            target = u
            break

    if target is None:
        return False, "사용자 계정을 찾을 수 없습니다.", None

    err = storage.save_users(users)
    if err:
        return True, f"개인 API 키 저장 완료 ({err})", target
    return True, "개인 API 키가 계정에 영구 저장되어 '개인 키 모드(CUSTOM)'로 전환되었습니다!", target


def clear_user_custom_api_key(
    storage: StorageManager,
    username: str,
) -> tuple[bool, str, UserRecord | None]:
    """저장된 개인 API 키를 삭제하고 FREE(무료 회원) 등급으로 복귀시킨다."""
    norm_id = normalize_username(username)
    users = storage.load_users()
    target: UserRecord | None = None

    for u in users:
        if u.username == norm_id:
            u.custom_api_key = ""
            if u.tier != "ADMIN":
                u.tier = "FREE"
            target = u
            break

    if target is None:
        return False, "사용자 계정을 찾을 수 없습니다.", None

    err = storage.save_users(users)
    if err:
        return True, f"개인 키 삭제 완료 ({err})", target
    return True, "개인 API 키를 삭제하고 무료 회원(FREE)으로 복귀했습니다.", target


def resolve_active_gemini_api_key(user: UserRecord, server_api_key: str) -> tuple[str, str]:
    """등급에 따라 실제 Gemini 호출에 사용할 API 키를 라우팅한다."""
    if user.tier == "CUSTOM":
        return user.custom_api_key.strip(), "개인 키 모드 (CUSTOM)"
    return (server_api_key or "").strip(), f"서버 키 ({TIER_LABELS.get(user.tier, user.tier)})"


def get_remaining_free_quota(user: UserRecord) -> int | None:
    """FREE 등급의 오늘 남은 무료 추출 문항 수를 반환한다. CUSTOM/PRO/ADMIN은 None(무제한)."""
    if user.tier in ("CUSTOM", "PRO", "ADMIN"):
        return None
    today = get_kst_today_str()
    used_today = user.daily_count if user.last_used_date == today else 0
    return max(0, FREE_DAILY_LIMIT - used_today)


def can_use_premium_features(user: UserRecord) -> bool:
    """상위 과목 문제 자동 개조 및 맞춤형 시험지 PDF 병합 허용 여부 (CUSTOM/PRO/ADMIN)."""
    return user.tier in ("CUSTOM", "PRO", "ADMIN")


def can_access_shared_concept_db(user: UserRecord | None) -> bool:
    """공용 개념서·교과서 DB 구축(업로드) 및 단원별 불러오기 권한 여부 (PRO[VIP] / ADMIN 전용)."""
    if user is None:
        return False
    return user.tier in ("PRO", "ADMIN")


def consume_extraction_quota(
    storage: StorageManager,
    username: str,
    extracted_count: int,
) -> UserRecord | None:
    """
    추출한 문항 수만큼 서버 사용량을 누적한다.
    CUSTOM 회원은 본인 API 키를 사용하므로 서버 한도(daily_count)를 차감하지 않는다.
    """
    norm_id = normalize_username(username)
    users = storage.load_users()
    today = get_kst_today_str()
    admin_names = resolve_admin_config().admin_usernames
    target: UserRecord | None = None

    for u in users:
        if u.username == norm_id:
            _sync_user_state(u, today, admin_names)
            if u.tier != "CUSTOM":
                u.daily_count += max(0, extracted_count)
                u.last_used_date = today
            target = u
            break

    if target is not None:
        storage.save_users(users)
    return target


def batch_update_user_tiers(
    storage: StorageManager,
    tier_updates: dict[str, TierType],
) -> tuple[int, str | None]:
    """관리자가 여러 회원의 등급을 한 번에 업데이트해 users.json에 저장한다."""
    users = storage.load_users()
    changed_count = 0
    for u in users:
        new_tier = tier_updates.get(u.username)
        if new_tier in VALID_TIERS and u.tier != new_tier:
            u.tier = new_tier
            changed_count += 1

    if changed_count > 0:
        err = storage.save_users(users)
        return changed_count, err
    return 0, None
