"""Configuration (variables d'environnement) et réglages modifiables depuis l'interface."""
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Client torrent unique, format historique (qBittorrent seul). Voir aussi TORRENT_CLIENT_N plus bas.
QBT_URL = os.environ.get("QBT_URL", "http://qbittorrent:8080").rstrip("/")
QBT_APIKEY = os.environ.get("QBT_APIKEY", "")
QBT_URL_SET = bool(os.environ.get("QBT_URL", "").strip())
XS_URL = os.environ.get("XS_URL", "http://cross-seed:2468").rstrip("/")
XS_APIKEY = os.environ.get("XS_APIKEY", "")
XS_VERSION = os.environ.get("XS_VERSION", "").strip()   # vide : détection automatique (plus bas)
if XS_VERSION not in ("", "6", "7"):
    raise ValueError("XS_VERSION doit être 6, 7 ou vide (détection automatique)")
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

# --- clients torrent ------------------------------------------------------------
# Même syntaxe que torrentClients de cross-seed : "type[:readonly]:url", identifiants dans l'URL.
CLIENT_TYPES = {"qbittorrent": "qBittorrent", "rtorrent": "rTorrent",
                "transmission": "Transmission", "deluge": "Deluge"}
_CLIENT_RE = re.compile(r"^\s*([a-z]+):(readonly:)?(\S+)\s*$", re.I)


def parse_client(entry: str) -> dict:
    """Décompose une entrée torrentClients. Lève ValueError si elle n'est pas reconnue.
    "readonly:" est accepté et sans effet : XSE ne fait que lire les clients."""
    m = _CLIENT_RE.match(str(entry or ""))
    kind = m.group(1).lower() if m else ""
    if kind not in CLIENT_TYPES:
        raise ValueError(f"Client torrent non reconnu : {_hide_password(str(entry))} "
                         f"(attendu : {'|'.join(CLIENT_TYPES)}:http://hôte:port)")
    p = urlsplit(m.group(3))
    if p.scheme not in ("http", "https", "scgi") or (p.scheme == "scgi" and kind != "rtorrent"):
        raise ValueError(f"Adresse de client invalide : {_hide_password(m.group(3))}")
    if p.scheme != "scgi" and not p.hostname:
        raise ValueError(f"Adresse de client sans hôte : {_hide_password(m.group(3))}")
    try:
        port = p.port
    except ValueError as e:
        raise ValueError(f"Port invalide : {_hide_password(m.group(3))}") from e
    host = p.hostname or ""
    return {"type": kind, "scheme": p.scheme, "host": host, "port": port,
            "path": p.path.rstrip("/") if p.scheme != "scgi" else p.path,
            "user": unquote(p.username or ""), "password": unquote(p.password or ""),
            "apikey": "", "name": ""}


def _hide_password(url: str) -> str:
    return re.sub(r"(//[^:/@\s]*:)[^@/\s]*@", r"\1•••@", url)


def client_url(c: dict) -> str:
    """URL du client sans identifiants (requêtes et affichage)."""
    if c["scheme"] == "scgi" and not c["host"]:
        return f"scgi://{c['path']}"
    host = f"[{c['host']}]" if ":" in c["host"] else c["host"]
    port = f":{c['port']}" if c["port"] else ""
    return f"{c['scheme']}://{host}{port}{c['path']}"


def client_id(c: dict) -> str:
    """Identifiant stable d'un client (noms personnalisés dans les réglages)."""
    return f"{c['type']}:{client_url(c).split('://', 1)[1]}"


def _env_clients() -> list:
    """TORRENT_CLIENT_1, TORRENT_CLIENT_2… (numéros triés, trous permis), avec _APIKEY et _NAME."""
    nums = sorted(int(m.group(1)) for k in os.environ if (m := re.fullmatch(r"TORRENT_CLIENT_(\d+)", k))
                  and os.environ[k].strip())
    out = []
    for n in nums:
        c = parse_client(os.environ[f"TORRENT_CLIENT_{n}"])
        c["apikey"] = os.environ.get(f"TORRENT_CLIENT_{n}_APIKEY", "").strip()
        c["name"] = os.environ.get(f"TORRENT_CLIENT_{n}_NAME", "").strip()[:40]
        out.append(c)
    return out


ENV_CLIENTS = _env_clients()   # erreur de syntaxe : l'app ne démarre pas (comme XS_VERSION)
# Hôte qui remplace localhost dans les adresses lues dans la config de cross-seed (cross-seed dans
# le réseau de Gluetun, par exemple). Vide : l'hôte de QBT_URL s'il est renseigné.
CLIENT_HOST = os.environ.get("TORRENT_CLIENT_HOST", "").strip()


def legacy_client() -> dict:
    c = parse_client(f"qbittorrent:{QBT_URL}")
    c["apikey"] = QBT_APIKEY
    return c


LOGS_DIR = XS_CONFIG_DIR / "logs"
PENDING_DIR = XS_CONFIG_DIR / "cross-seeds"
XS_DB = XS_CONFIG_DIR / "cross-seed.db"


def _detect_version() -> str:
    """cross-seed v7 ajoute la colonne indexer.enabled (migration 14) ; absente en v6.
    Base illisible ou absente : v6, le fonctionnement historique."""
    if not XS_DB.exists():
        return "6"
    uri = f"file:{XS_DB}?mode=ro"
    for suffix in ("", "&immutable=1"):
        try:
            con = sqlite3.connect(uri + suffix, uri=True, timeout=5)
            try:
                cols = {r[1] for r in con.execute("PRAGMA table_info('indexer')")}
            finally:
                con.close()
            return "7" if "enabled" in cols else "6"
        except sqlite3.Error:
            continue
    return "6"


XS_VERSION_SOURCE = "XS_VERSION" if XS_VERSION else "détection automatique"
XS_VERSION = XS_VERSION or _detect_version()
XS_CONFIG_JS = XS_CONFIG_DIR / "config.js"

DEFAULT_SETTINGS = {
    "delay": 60,
    "rules": [],
    "tracker_aliases": {},
    # Groupes de trackers, construits par l'utilisateur selon sa stack :
    # [{"name": "Vidéo", "categories": ["radarr", "sonarr"], "others": False, "indexers": [clé d'indexer, …]}]
    # (clé = URL Torznab normalisée ; "" dans categories = torrents sans catégorie ; "others" : le groupe
    # reçoit aussi toutes les catégories rangées dans aucun autre groupe). Remplace l'ancien « routes ».
    "groups": [],
    # Scan planifié par XSE (remplace ou complète le scan complet de cross-seed)
    "scan": {"enabled": False, "every_hours": 24, "limit": 400, "recent_days": 7},
    # Sources d'indexers saisies dans l'interface (le .env reste prioritaire)
    "sources": {"prowlarr": {"url": "", "apikey": ""}, "jackett": {"url": "", "apikey": ""}},
    # Noms affichés des clients torrent : {identifiant du client: nom} (le .env reste prioritaire)
    "client_names": {},
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
    if "groups" not in data and data.get("routes"):
        merged["groups"] = groups_from_routes(data["routes"])   # réglages d'avant la v0.6
    merged["groups"] = clean_groups(merged.get("groups"))
    merged["scan"] = clean_scan(merged.get("scan"))
    merged["sources"] = {n: {"url": str((src.get(n) or {}).get("url", "")),
                             "apikey": str((src.get(n) or {}).get("apikey", ""))} for n in SOURCES}
    return merged


def groups_from_routes(routes) -> list:
    """Anciennes routes (une catégorie → des indexers) : un groupe par route, nommé comme sa catégorie.
    Comportement identique tant que l'utilisateur ne les regroupe pas."""
    out = []
    for r in routes if isinstance(routes, list) else []:
        if isinstance(r, dict) and str(r.get("category", "")).strip():
            cat = str(r["category"]).strip()
            out.append({"name": cat, "categories": [cat], "others": False, "indexers": r.get("indexers") or []})
    return out


def clean_groups(groups) -> list:
    """Groupes valides : un nom unique ; une catégorie n'appartient qu'à un seul groupe (le premier) ;
    un seul groupe « toutes les autres catégories ». Majuscules et minuscules confondues."""
    out, names, cats, others = [], set(), set(), False
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name", "")).strip()[:60]
        if not name or name.lower() in names:
            continue
        names.add(name.lower())
        mine = []
        for c in g.get("categories") or []:
            c = str(c).strip()
            if c.lower() not in cats:   # "" (sans catégorie) est une valeur permise
                cats.add(c.lower())
                mine.append(c)
        oth = bool(g.get("others")) and not others
        others = others or oth
        idx = [str(k).strip() for k in g.get("indexers") or [] if str(k).strip()]
        out.append({"name": name, "categories": mine, "others": oth, "indexers": list(dict.fromkeys(idx))})
    return out


def group_for(category: str, groups: list):
    """Groupe d'une catégorie qBittorrent : celui qui la contient, sinon celui des « autres catégories »."""
    cat = (category or "").strip().lower()
    for g in groups:
        if any(c.lower() == cat for c in g["categories"]):
            return g
    return next((g for g in groups if g["others"]), None)


def _bounded(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def clean_scan(scan) -> dict:
    d = DEFAULT_SETTINGS["scan"]
    scan = scan if isinstance(scan, dict) else {}
    return {"enabled": bool(scan.get("enabled", d["enabled"])),
            "every_hours": _bounded(scan.get("every_hours"), d["every_hours"], 1, 24 * 30),
            "limit": _bounded(scan.get("limit"), d["limit"], 0, 100000),   # 0 : pas de plafond
            "recent_days": _bounded(scan.get("recent_days"), d["recent_days"], 0, 3650)}


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
    if "groups" in data:
        clean["groups"] = clean_groups(data["groups"])
    if "scan" in data:
        clean["scan"] = clean_scan({**clean["scan"], **(data["scan"] if isinstance(data["scan"], dict) else {})})
    if isinstance(data.get("client_names"), dict):
        clean["client_names"] = {str(k): str(v).strip()[:40]
                                 for k, v in data["client_names"].items() if str(v).strip()}
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
