"""Scan planifié par XSE, pour remplacer (ou compléter) le scan complet de cross-seed (searchCadence).

Toutes les N heures, la file reçoit les releases à chercher : d'abord celles des règles prioritaires
(dans l'ordre des règles), puis les moins cross-seedées, puis celles cherchées il y a le plus longtemps.
Les releases cherchées récemment sont sautées et le nombre par passage est plafonné, comme
excludeRecentSearch et searchLimit dans cross-seed. Les catégories routées passent par leurs indexers.
"""
import json
import threading
import time
from datetime import datetime, timedelta

from . import config

_lock = threading.Lock()


# --- historique des recherches routées (cross-seed ne les enregistre pas) -------------------------

def _history_path():
    return config.DATA_DIR / "routed_history.json"


def routed_history() -> dict:
    """{clé de release: date ISO de la dernière recherche routée}"""
    with _lock:
        try:
            return json.loads(_history_path().read_text())
        except (OSError, ValueError):
            return {}


def record_routed(key: str) -> None:
    with _lock:
        try:
            data = json.loads(_history_path().read_text())
        except (OSError, ValueError):
            data = {}
        data[key] = datetime.now().isoformat(timespec="seconds")
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _history_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(_history_path())
        except OSError:
            pass


def merge_last_search(items: list) -> None:
    """Colonne « Dernière recherche » : la plus récente entre cross-seed et les recherches routées."""
    hist = routed_history()
    for r in items:
        h = hist.get(r["key"])
        if h and (not r.get("last_search") or h > r["last_search"]):
            r["last_search"] = h


# --- état du scan ------------------------------------------------------------------------------------

def _state_path():
    return config.DATA_DIR / "scan.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_state(**changes) -> dict:
    with _lock:
        state = load_state()
        state.update(changes)
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _state_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False))
            tmp.replace(_state_path())
        except OSError:
            pass
        return state


def next_run(settings: dict, state: dict):
    sc = settings["scan"]
    if not sc["enabled"]:
        return None
    last = state.get("last_run")
    return (last or 0) + sc["every_hours"] * 3600


def due(settings: dict) -> bool:
    nr = next_run(settings, load_state())
    return nr is not None and time.time() >= nr


# --- sélection -----------------------------------------------------------------------------------

def candidates(items: list, settings: dict) -> tuple:
    """(releases à mettre en file, nombre d'éligibles, nombre sautées car cherchées récemment).
    Seules les releases dont le torrent d'origine est dans qBittorrent sont retenues : cross-seed
    refuse de chercher à partir d'un torrent qui est lui-même un cross-seed."""
    sc = settings["scan"]
    limit_date = (datetime.now() - timedelta(days=sc["recent_days"])).isoformat(timespec="seconds")
    order = {config.rule_label(r): n for n, r in enumerate(settings["rules"]) if r.get("enabled", True)}
    eligible = [r for r in items if r["has_original"]]
    recent = [r for r in eligible if r.get("last_search") and r["last_search"] > limit_date]
    todo = [r for r in eligible if not (r.get("last_search") and r["last_search"] > limit_date)]
    todo.sort(key=lambda r: (min((order.get(n, 99) for n in r["rules"]), default=99),
                             r["seeds"], r.get("last_search") or "", r["name"].lower()))
    if sc["limit"]:
        todo = todo[: sc["limit"]]
    return todo, len(eligible), len(recent)
