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
JACKETT_URL = os.environ.get("JACKETT_URL", "").rstrip("/")
JACKETT_APIKEY = os.environ.get("JACKETT_APIKEY", "")
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
    # Routage : [{"category": "radarr", "indexers": [clé d'indexer, …]}] (clé = URL Torznab normalisée)
    "routes": [],
    # Sources d'indexers saisies dans l'interface (le .env reste prioritaire)
    "sources": {"prowlarr": {"url": "", "apikey": ""}, "jackett": {"url": "", "apikey": ""}},
}
SOURCES = ("prowlarr", "jackett")

_lock = threading.Lock()

RULE_TYPES = ("group", "contains", "starts", "category", "regex")


def _words(value: str) -> list:
    return [w.lstrip("-") for w in re.split(r"[\s,;]+", value.strip()) if w.lstrip("-")]


def categories(value: str) -> list:
    """Catégories d'une règle « Catégorie », séparées par des virgules (une catégorie peut contenir des espaces)."""
    return [c.strip() for c in str(value or "").split(",") if c.strip()]


def rule_pattern(rule: dict) -> str:
    """Construit l'expression régulière à partir d'une règle « humaine ».
    Pour une règle « Catégorie », l'expression porte sur la catégorie de la release, pas sur son nom."""
    t, v = rule.get("type", "regex"), str(rule.get("value", ""))
    if t == "category":
        cats = categories(v)
        return f"^(?:{'|'.join(re.escape(c) for c in cats)})$" if cats else ""
    if t == "group":
        groups = _words(v)
        return f"-(?:{'|'.join(re.escape(g) for g in groups)})(\\.\\w{{2,4}})?$" if groups else ""
    if t == "contains":
        return "".join(f"(?=.*{re.escape(w)})" for w in _words(v))
    if t == "starts":
        return f"^{re.escape(v.strip())}" if v.strip() else ""
    return v.strip()


def rule_label(rule: dict) -> str:
    """Étiquette d'une règle : sa valeur (plus de nom séparé, source de confusion).
    Préfixée pour une règle « Catégorie », pour ne pas la confondre avec un groupe de même nom."""
    value = str(rule.get("value") or "").strip()
    if rule.get("type") == "category":
        return f"Catégorie {', '.join(categories(value))}" if value else "Catégorie"
    return value or "Règle"


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
    src = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    merged["routes"] = clean_routes(merged.get("routes"))
    merged["sources"] = {n: {"url": str((src.get(n) or {}).get("url", "")),
                             "apikey": str((src.get(n) or {}).get("apikey", ""))} for n in SOURCES}
    return merged


def clean_routes(routes) -> list:
    """Routes valides : une catégorie non vide, une seule fois (majuscules et minuscules confondues)."""
    out, seen = [], set()
    for r in routes if isinstance(routes, list) else []:
        if not isinstance(r, dict):
            continue
        cat = str(r.get("category", "")).strip()
        if not cat or cat.lower() in seen:
            continue
        seen.add(cat.lower())
        idx = [str(k).strip() for k in r.get("indexers") or [] if str(k).strip()]
        out.append({"category": cat, "indexers": list(dict.fromkeys(idx))})
    return out


def public_settings(s: dict) -> dict:
    """Réglages renvoyés au navigateur : jamais les clés API."""
    return {k: v for k, v in s.items() if k != "sources"}


def env_source(name: str) -> tuple:
    return {"prowlarr": (PROWLARR_URL, PROWLARR_APIKEY), "jackett": (JACKETT_URL, JACKETT_APIKEY)}[name]


def source(name: str) -> tuple:
    """(adresse, clé, origine) d'une source d'indexers : .env, sinon Réglages, sinon vide."""
    url, key = env_source(name)
    if url or key:
        return url, key, ".env"
    s = load_settings()["sources"][name]
    if s["url"] or s["apikey"]:
        return s["url"], s["apikey"], "Réglages"
    return "", "", ""


def _clean_source(new: dict, old: dict) -> dict:
    url = str(new.get("url", "")).strip().rstrip("/")
    if url and not re.match(r"^https?://[^\s/]+", url):
        raise ValueError(f"Adresse invalide : {url} (attendu : http://hôte:port)")
    key = new.get("apikey")
    key = old["apikey"] if key is None else str(key).strip()   # None : clé inchangée
    if not url:
        key = ""
    return {"url": url, "apikey": key}


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
    if isinstance(data.get("sources"), dict):
        for n in SOURCES:
            if isinstance(data["sources"].get(n), dict):
                clean["sources"][n] = _clean_source(data["sources"][n], clean["sources"][n])
    if "routes" in data:
        clean["routes"] = clean_routes(data["routes"])
    if "tracker_aliases" in data:
        clean["tracker_aliases"] = {str(k): str(v).strip()
                                    for k, v in data["tracker_aliases"].items() if str(v).strip()}
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _settings_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
        tmp.chmod(0o600)   # contient les clés API des sources d'indexers
        tmp.replace(_settings_path())
    return clean
