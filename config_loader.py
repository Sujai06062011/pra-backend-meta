"""
config_loader.py — Load clinic configuration from clinic_config table.

All scheduler timings, feature flags, message templates and clinic info
live in the DB so they can be changed without redeployment.
"""
import os
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from supabase import create_client

_sb = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)

DOCTOR_ID = "8c33abe0-5d2e-4613-9437-c7c375e8d162"

# Simple in-process cache so every job function doesn't hit the DB
_cache: dict = {}
_cache_loaded = False


def load_config(doctor_id: str = DOCTOR_ID) -> dict:
    """
    Fetch all config rows for a doctor and return as a typed dict.
    Caches result in-process; call invalidate_cache() to refresh.
    """
    global _cache, _cache_loaded
    if _cache_loaded and doctor_id == DOCTOR_ID:
        return _cache

    try:
        result = _sb.table("clinic_config") \
            .select("config_key, config_value, config_type") \
            .eq("doctor_id", doctor_id) \
            .execute()

        config: dict = {}
        for row in (result.data or []):
            key   = row["config_key"]
            value = row["config_value"]
            ctype = row.get("config_type", "string")

            if ctype == "boolean":
                config[key] = value.lower() == "true"
            elif ctype == "integer":
                config[key] = int(value)
            elif ctype == "json":
                config[key] = json.loads(value)
            else:
                config[key] = value

        if doctor_id == DOCTOR_ID:
            _cache = config
            _cache_loaded = True

        print(f"✅ Config loaded: {len(config)} keys for doctor {doctor_id[:8]}...")
        return config

    except Exception as e:
        print(f"⚠️  Config load error (using defaults): {e}")
        return {}


def invalidate_cache():
    """Force next call to load_config() to re-fetch from DB."""
    global _cache, _cache_loaded
    _cache = {}
    _cache_loaded = False
    print("🔄 Config cache invalidated")


def get(key: str, default=None, doctor_id: str = DOCTOR_ID):
    """Get a single typed config value."""
    return load_config(doctor_id).get(key, default)


def get_scheduler_time(job_name: str, doctor_id: str = DOCTOR_ID) -> tuple[int, int]:
    """
    Return (hour, minute) for a scheduler job.
    Reads 'scheduler.<job_name>.time' key (HH:MM format).
    """
    time_str = get(f"scheduler.{job_name}.time", "08:00", doctor_id)
    try:
        t = datetime.strptime(time_str, "%H:%M")
        return t.hour, t.minute
    except Exception:
        return 8, 0


def is_enabled(feature: str, doctor_id: str = DOCTOR_ID) -> bool:
    """Check a feature flag: 'feature.<feature>.enabled'."""
    return bool(get(f"feature.{feature}.enabled", True, doctor_id))


def clinic_name(doctor_id: str = DOCTOR_ID) -> str:
    return get("clinic.name", "Dr. Kumar Child Care Clinic", doctor_id)


def doctor_name(doctor_id: str = DOCTOR_ID) -> str:
    return get("clinic.doctor_name", "Dr. Kumar", doctor_id)


def google_review_link(doctor_id: str = DOCTOR_ID) -> str:
    return get("feature.google_review_link", "https://g.page/r/YOUR_CLINIC_REVIEW_LINK", doctor_id)


def get_template(template_key: str, language: str,
                 variables: dict, doctor_id: str = DOCTOR_ID) -> str:
    """
    Fetch template from DB by 'template.<template_key>.<language>' and
    format with variables. Falls back to English if language not found.
    """
    config = load_config(doctor_id)
    lang_key    = f"template.{template_key}.{language}"
    english_key = f"template.{template_key}.english"
    template = config.get(lang_key) or config.get(english_key, "")
    if not template:
        return ""
    try:
        return template.format(**variables)
    except KeyError as e:
        print(f"⚠️  Template variable missing for {lang_key}: {e}")
        return template
