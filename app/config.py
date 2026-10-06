"""Configuration (variables d'environnement) et réglages modifiables depuis l'interface."""
import json
import os
import re
import threading
from pathlib import Path

QBT_URL = os.environ.get("QBT_URL", "http://qbittorrent:8080").rstrip("/")
QBT_APIKEY = os.environ.get("QBT_APIKEY", "")
XS_URL = os.environ.get("XS_URL", "http://cross-seed:2468").rstrip("/")
XS_APIKEY = os.environ.get("XS_APIKEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "").rstrip("/")
PROWLARR_APIKEY = os.environ.get("PROWLARR_APIKEY", "")
XS_CONFIG_DIR = Path(os.environ.get("XS_CONFIG_DIR", "/cs-config"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DOCKER_URL = os.environ.get("DOCKER_URL", "").rstrip("/")   # ex. http://xse-docker-proxy:2375
XS_CONTAINER = os.environ.get("XS_CONTAINER", "cross-seed")
UI_USER = os.environ.get("UI_USER", "admin")
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")

# Lecture seule (instance de dev) : aucune action sur cross-seed, aucune écriture de config.js.
READONLY = os.environ.get("XSE_READONLY", "").strip().lower() in ("1", "true", "yes", "on")
# Exception à la lecture seule : écriture de config.js autorisée quand c'est une copie locale (dev).
ALLOW_CONFIG_WRITE = os.environ.get("XSE_ALLOW_CONFIG_WRITE", "").strip().lower() in ("1", "true", "yes", "on")


class ReadOnlyError(RuntimeError):
    """Action refusée parce que XSE tourne en lecture seule."""


def guard(action: str, *, config_write: bool = False) -> None:
    if READONLY and not (config_write and ALLOW_CONFIG_WRITE):
        raise ReadOnlyError(f"Lecture seule (XSE_READONLY) : {action} désactivé")

LOGS_DIR = XS_CONFIG_DIR / "logs"
PENDING_DIR = XS_CONFIG_DIR / "cross-seeds"
XS_DB = XS_CONFIG_DIR / "cross-seed.db"
XS_CONFIG_JS = XS_CONFIG_DIR / "config.js"

DEFAULT_SETTINGS = {
    "delay": 60,
    "rules": [],
    "tracker_aliases": {},
}

_lock = threading.Lock()

RULE_TYPES = ("group", "contains", "starts", "regex")


def _words(value: str) -> list:
    return [w.lstrip("-") for w in re.split(r"[\s,;]+", value.strip()) if w.lstrip("-")]


def rule_pattern(rule: dict) -> str:
    """Construit l'expression régulière à partir d'une règle « humaine »."""
    t, v = rule.get("type", "regex"), str(rule.get("value", ""))
    if t == "group":
        groups = _words(v)
        return f"-(?:{'|'.join(re.escape(g) for g in groups)})(\\.\\w{{2,4}})?$" if groups else ""
    if t == "contains":
        return "".join(f"(?=.*{re.escape(w)})" for w in _words(v))
    if t == "starts":
        return f"^{re.escape(v.strip())}" if v.strip() else ""
    return v.strip()


def rule_label(rule: dict) -> str:
    """Étiquette d'une règle : sa valeur (plus de nom séparé, source de confusion)."""
    return str(rule.get("value") or "").strip() or "Règle"


def _upgrade(rule: dict) -> dict:
    """Anciennes règles (motif seul) : on reconnaît le cas « groupe de release »."""
    if rule.get("type") in RULE_TYPES:
        return rule
    pat = rule.get("pattern", "")
    m = re.fullmatch(r"-(?:\(\?:)?([\w|]+)\)?\(\\\.\\w\{2,4\}\)\?\$", pat)
    if m:
        return {**rule, "type": "group", "value": " ".join(m.group(1).split("|"))}
    return {**rule, "type": "regex", "value": pat}


def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def load_settings() -> dict:
    with _lock:
        try:
            data = json.loads(_settings_path().read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    merged["rules"] = [{k: v for k, v in _upgrade(r).items() if k != "name"} for r in merged["rules"]]
    return merged


def save_settings(data: dict) -> dict:
    clean = load_settings()
    if "delay" in data:
        clean["delay"] = max(5, min(3600, int(data["delay"])))
    if "rules" in data:
        rules = []
        for r in data["rules"]:
            r = _upgrade(r) if r.get("type") not in RULE_TYPES else r
            rule = {"type": r["type"], "value": str(r.get("value", "")).strip(),
                    "enabled": bool(r.get("enabled", True))}
            rule["pattern"] = rule_pattern(rule)
            if not rule["pattern"]:
                continue
            rules.append(rule)
        clean["rules"] = rules
    if "tracker_aliases" in data:
        clean["tracker_aliases"] = {str(k): str(v).strip()
                                    for k, v in data["tracker_aliases"].items() if str(v).strip()}
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _settings_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
        tmp.replace(_settings_path())
    return clean
