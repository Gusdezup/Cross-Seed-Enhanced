"""Recherches programmées : une recherche de l'onglet Chercher (critères + cible) répétée toutes les N heures.

Chaque passage crée une demande dans la file, comme un lancement à la main. Remplace l'ancien « scan
planifié », converti au premier démarrage en une recherche programmée « toutes les releases, selon le
groupe ». Stockage : data/schedules.json.
"""
import json
import threading
import time
import uuid

from . import config, scan

_lock = threading.Lock()


def _path():
    return config.DATA_DIR / "schedules.json"


def _migrate() -> list:
    """Premier démarrage : l'ancien scan planifié, s'il était actif, devient une recherche programmée."""
    sc = config.load_settings().get("scan") or {}
    if not sc.get("enabled"):
        return []
    crit = {"targets": [], "fresh": {"mode": "days", "days": sc.get("recent_days") or 7}}
    if sc.get("limit"):
        crit["limit"] = sc["limit"]
    state = scan.load_state()
    return [{"id": uuid.uuid4().hex[:8], "name": "Scan planifié", "criteria": crit,
             "every_hours": sc.get("every_hours") or 24, "enabled": True, "created": time.time(),
             "last_run": state.get("last_run"), "last_added": state.get("last_added"),
             "last_matched": state.get("last_selected"), "last_error": state.get("last_error")}]


def load() -> list:
    with _lock:
        try:
            return json.loads(_path().read_text())
        except FileNotFoundError:
            items = _migrate()
            _write(items)
            return items
        except (OSError, ValueError):
            return []


def _write(items: list) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
        tmp.replace(_path())
    except OSError:
        pass


def save(items: list) -> None:
    with _lock:
        _write(items)


def _hours(v) -> int:
    try:
        return max(1, min(24 * 30, int(v)))
    except (TypeError, ValueError):
        raise ValueError("Fréquence invalide : un nombre d'heures entre 1 et 720") from None


def create(name: str, criteria: dict, every_hours) -> dict:
    if criteria.get("keys"):
        raise ValueError("Une sélection de releases cochées ne se programme pas : utilise des critères")
    item = {"id": uuid.uuid4().hex[:8], "name": (name or "").strip()[:80] or "Recherche programmée",
            "criteria": criteria, "every_hours": _hours(every_hours), "enabled": True, "created": time.time(),
            "last_run": None, "last_added": None, "last_matched": None, "last_error": None}
    items = load()
    items.append(item)
    save(items)
    return item


def update(sid: str, changes: dict) -> dict:
    items = load()
    item = next((i for i in items if i["id"] == sid), None)
    if not item:
        raise KeyError(sid)
    if "enabled" in changes:
        item["enabled"] = bool(changes["enabled"])
    if "every_hours" in changes:
        item["every_hours"] = _hours(changes["every_hours"])
    if "name" in changes and str(changes["name"]).strip():
        item["name"] = str(changes["name"]).strip()[:80]
    for k in ("last_run", "last_added", "last_matched", "last_error"):
        if k in changes:
            item[k] = changes[k]
    save(items)
    return item


def delete(sid: str) -> bool:
    items = load()
    rest = [i for i in items if i["id"] != sid]
    save(rest)
    return len(rest) < len(items)


def next_run(item: dict):
    if not item["enabled"]:
        return None
    # jamais lancée : premier passage N heures après la création (« Programmer et lancer » part tout de suite)
    return (item.get("last_run") or item.get("created") or 0) + item["every_hours"] * 3600


def due() -> list:
    now = time.time()
    return [i for i in load() if i["enabled"] and next_run(i) <= now]
