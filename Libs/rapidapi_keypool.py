"""Rotating pool of RapidAPI keys with per-key quota tracking.

Multiple RapidAPI accounts (primary + backups) are tried in order. The
quota headers every response already carries are recorded per key, so the
app can rotate to a backup BEFORE the active key is exhausted (default
margin: 200 requests) and never spends extra quota learning the state.

Design constraints shared with the rest of the signer stack:

- Both the GUI process and the SEI proxy child resolve the key through
  this module. They share state via logs/rapidapi_keypool.json (atomic
  writes, like every other JSON in logs/); the selection is deterministic
  from that file, so both processes agree on the active key without any
  IPC.
- No raw key is ever written to the state file; keys are identified by a
  sha256 fingerprint.
- Env vars (RAPIDAPI_KEY / RAPIDAPI_BACKUP_KEYS /
  RAPIDAPI_ROTATION_THRESHOLD) extend the candidate list for child
  processes, matching the existing env-first convention in signers.py.

Selection rule (current_key):
  walk the candidates in order; a candidate is usable when its remaining
  quota is unknown (never called, or its monthly reset has passed) or at
  or above the rotation threshold. The first usable candidate wins. When
  no candidate is usable, the first candidate is returned anyway so the
  caller fails with the existing QUOTA error and the app pauses the
  stream instead of silently switching.
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

try:
    from Libs.app_paths import logs_dir as _app_logs_dir
    from Libs.app_paths import user_data_dir as _app_user_data_dir
except Exception:
    try:
        from app_paths import logs_dir as _app_logs_dir
        from app_paths import user_data_dir as _app_user_data_dir
    except Exception:
        _app_logs_dir = None
        _app_user_data_dir = None

APP_ROOT = Path(__file__).resolve().parents[1]

# Margin kept on the active key before rotating. 200 requests comfortably
# covers the pause/resume round-trips and a short wait for a new key.
DEFAULT_THRESHOLD = 200

# A 429 while the key still shows plenty of monthly quota is a burst/hourly
# rate limit, not exhaustion: park the key for a few minutes so rotation
# moves on, and let it back in automatically afterwards.
RATE_LIMIT_PARK_SECONDS = 300


def _logs_dir() -> Path:
    if _app_logs_dir is not None:
        try:
            return Path(_app_logs_dir())
        except Exception:
            pass
    return APP_ROOT / "logs"


STATE_PATH = _logs_dir() / "rapidapi_keypool.json"
_LOCK = threading.Lock()


def _read_config() -> dict:
    paths = [APP_ROOT / "config.json", APP_ROOT.parent / "config.json",
             Path.cwd() / "config.json"]
    # The GUI keeps its config in the user data dir (next to the EXE on
    # Windows/Linux, ~/Library/Application Support on macOS). Libs modules
    # resolve it through app_paths so the SEI proxy children agree.
    if _app_user_data_dir is not None:
        try:
            user_config = Path(_app_user_data_dir()) / "config.json"
        except Exception:
            user_config = None
        if user_config is not None:
            paths.insert(0, user_config)
    for path in paths:
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as fh:
                    value = json.load(fh)
                if isinstance(value, dict):
                    return value
        except Exception:
            continue
    return {}


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clean(value):
    return str(value or "").strip()


def _dedup(items):
    seen = set()
    out = []
    for item in items:
        item = _clean(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _env_list(name):
    return _dedup((_clean(part) for part in re_split(_clean(os.environ.get(name)))))


def re_split(value):
    """Split an env/config string on commas, semicolons and newlines."""
    for separator in ("\n", ";", ","):
        if separator in value:
            value = value.replace(separator, "\1")
    return value.split("\1") if "\1" in value else ([value] if value else [])


def _as_list(value):
    """Config values may be a list or a newline/comma separated string."""
    if isinstance(value, str):
        return _dedup(re_split(_clean(value)))
    if isinstance(value, (list, tuple)):
        return _dedup(value)
    return []


def keys():
    """Ordered candidate list: primary first, then backups."""
    config = _read_config()
    primary_env = _clean(os.environ.get("RAPIDAPI_KEY"))
    primary_config = _clean(config.get("rapidapi_key"))
    backups_env = _env_list("RAPIDAPI_BACKUP_KEYS")
    backups_config = _as_list(config.get("rapidapi_backup_keys"))
    return _dedup([primary_env, primary_config, *backups_env, *backups_config])


def threshold():
    value = _clean(os.environ.get("RAPIDAPI_ROTATION_THRESHOLD"))
    if not value:
        value = _clean(_read_config().get("rapidapi_rotation_threshold"))
    parsed = _to_int(value)
    if parsed is None or parsed < 1:
        return DEFAULT_THRESHOLD
    return parsed


def fingerprint(key):
    return hashlib.sha256(_clean(key).encode("utf-8")).hexdigest()[:10]


def _state_path(path=None):
    return Path(path) if path is not None else STATE_PATH


def read_state(path=None):
    try:
        with _state_path(path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and isinstance(data.get("keys"), dict) else {}
    except Exception:
        return {}


def _write_state(state, path=None):
    try:
        target = _state_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, target)
    except Exception:
        pass


def _snapshot_fields(snapshot):
    remaining = _to_int((snapshot or {}).get("remaining"))
    limit = _to_int((snapshot or {}).get("limit"))
    updated = _to_int((snapshot or {}).get("updated")) or 0
    reset = _to_int((snapshot or {}).get("reset"))
    reset_epoch = None
    if reset:
        reset_epoch = reset if reset > 100_000_000 else (updated + reset if updated else None)
    return remaining, limit, reset_epoch, updated


def _effective_remaining(fp, state, now=None):
    """Remaining for a key, or None when unknown/stale.

    A key whose monthly reset has passed is unknown again: the next call
    re-learns its real remaining (and succeeds), which auto-recovers the
    pool after the quota resets.
    """
    snapshot = (state.get("keys") or {}).get(fp) or {}
    if snapshot.get("invalid"):
        return -1
    now = int(now if now is not None else time.time())
    if int(snapshot.get("cooldown_until") or 0) > now:
        return 0
    remaining, _limit, reset_epoch, _updated = _snapshot_fields(snapshot)
    if remaining is None:
        return None
    if reset_epoch and now >= reset_epoch:
        return None
    return remaining


def usable(key, state=None, at_threshold=None):
    """True when this key may serve the next request."""
    if state is None:
        state = read_state()
    margin = threshold() if at_threshold is None else at_threshold
    remaining = _effective_remaining(fingerprint(key), state)
    if remaining is None:
        return True
    return remaining >= margin


def current_key(state=None):
    """The key the next signing call must use."""
    candidates = keys()
    if not candidates:
        return ""
    if state is None:
        state = read_state()
    for candidate in candidates:
        if usable(candidate, state=state):
            return candidate
    # Nobody is usable: return the first candidate so the call fails with
    # the caller's QUOTA/SUBSCRIPTION error (the app pauses the stream).
    return candidates[0]


def note_headers(key, headers, *, success=True, path=None):
    """Record the quota headers of a response obtained with `key`.

    A successful response also clears a stale invalid marker or rate-limit
    park. Mirrors the snapshot into the legacy flat file
    (logs/rapidapi_quota.json) so the existing quota bar keeps working.
    """
    key = _clean(key)
    if not key:
        return {}
    fp = fingerprint(key)
    snapshot = _extract_headers(headers)
    if not snapshot and success:
        # No quota headers at all (non-RapidAPI signer): still clear stale
        # markers so a recovered key is not parked forever.
        with _LOCK:
            state = read_state(path)
            entry = dict((state.get("keys") or {}).get(fp) or {})
            changed = entry.pop("invalid", None) is not None or \
                entry.pop("cooldown_until", None) is not None
            if changed or entry:
                state.setdefault("keys", {})[fp] = entry
                state["updated"] = int(time.time())
                _write_state(state, path)
        return {}
    if not snapshot:
        return {}
    with _LOCK:
        state = read_state(path)
        entry = dict((state.get("keys") or {}).get(fp) or {})
        if success:
            entry.pop("invalid", None)
            entry.pop("cooldown_until", None)
        entry.update(snapshot)
        state.setdefault("keys", {})[fp] = entry
        state["updated"] = int(time.time())
        _write_state(state, path)
        _mirror_flat_quota(entry)
    return snapshot


def _mirror_flat_quota(entry):
    """Keep logs/rapidapi_quota.json in sync for the active-key bar."""
    try:
        from Libs.rapidapi_quota import update_from_headers as _flat_update
    except Exception:
        try:
            from rapidapi_quota import update_from_headers as _flat_update
        except Exception:
            return
    _flat_update({
        "x-ratelimit-requests-limit": str(entry.get("limit") or ""),
        "x-ratelimit-requests-remaining": str(entry.get("remaining") or ""),
        "x-ratelimit-requests-used": str(entry.get("used") or ""),
        "x-ratelimit-requests-reset": str(entry.get("reset") or ""),
    })


_HEADER_NAMES = {
    "limit": ("x-ratelimit-requests-limit", "x-quota-limit"),
    "remaining": ("x-ratelimit-requests-remaining", "x-quota-remaining"),
    "used": ("x-ratelimit-requests-used", "x-quota-used"),
    "reset": ("x-ratelimit-requests-reset", "x-quota-reset"),
}


def _header_value(headers, names):
    getter = getattr(headers, "get", None)
    if getter is None:
        try:
            headers = {str(k).lower(): v for k, v in headers.items()}
            getter = headers.get
        except Exception:
            return ""
    for name in names:
        try:
            value = getter(name)
        except Exception:
            value = None
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _extract_headers(headers):
    snapshot = {}
    for field, names in _HEADER_NAMES.items():
        value = _header_value(headers, names)
        if value:
            snapshot[field] = value
    if snapshot:
        snapshot["updated"] = int(time.time())
    return snapshot


def note_invalid(key, path=None):
    """Mark a key as rejected (401/403): never selected again this run."""
    key = _clean(key)
    if not key:
        return
    fp = fingerprint(key)
    with _LOCK:
        state = read_state(path)
        entry = dict((state.get("keys") or {}).get(fp) or {})
        entry["invalid"] = True
        state.setdefault("keys", {})[fp] = entry
        state["updated"] = int(time.time())
        _write_state(state, path)


def note_exhausted(key, path=None):
    """A 429: either the monthly quota is gone (remember remaining=0) or,
    while the headers still show healthy monthly quota, a burst/hourly rate
    limit — park the key briefly so rotation moves on and readmits it later.
    """
    key = _clean(key)
    if not key:
        return
    fp = fingerprint(key)
    with _LOCK:
        state = read_state(path)
        entry = dict((state.get("keys") or {}).get(fp) or {})
        remaining = _to_int(entry.get("remaining"))
        if remaining is not None and remaining >= threshold():
            entry["cooldown_until"] = int(time.time()) + RATE_LIMIT_PARK_SECONDS
        else:
            entry["remaining"] = "0"
            entry.pop("cooldown_until", None)
        state.setdefault("keys", {})[fp] = entry
        state["updated"] = int(time.time())
        _write_state(state, path)


def pool_status():
    """Summary for the GUI: per-key lines plus the active selection."""
    candidates = keys()
    state = read_state()
    margin = threshold()
    active = current_key(state=state)
    lines = []
    for index, key in enumerate(candidates, start=1):
        fp = fingerprint(key)
        remaining = _effective_remaining(fp, state)
        entry = (state.get("keys") or {}).get(fp) or {}
        if entry.get("invalid"):
            state_text = "invalid"
        elif remaining is None:
            state_text = "quota unknown"
        else:
            state_text = f"{remaining:,} left"
        if key == active:
            state_text += " · active"
        lines.append(f"Key #{index} (…{fp}): {state_text}")
    usable_keys = [k for k in candidates if usable(k, state=state)]
    return {
        "keys": candidates,
        "active": active,
        "active_fp": fingerprint(active) if active else "",
        "active_index": candidates.index(active) + 1 if active in candidates else 0,
        "threshold": margin,
        "lines": lines,
        "usable_count": len(usable_keys),
        "usable_backups": max(0, len(usable_keys) - 1),
        "has_usable": bool(usable_keys),
    }
