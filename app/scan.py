"""Historique des recherches faites par XSE (que cross-seed n'enregistre pas), et petit état persistant
(ancienne valeur de searchCadence, état de l'ancien scan planifié repris par schedules.py)."""
import json
import threading
from datetime import datetime

from . import config

_lock = threading.Lock()


# --- historique des recherches routées (cross-seed ne les enregistre pas) -------------------------

def _history_path():
    return config.DATA_DIR / "routed_history.json"


def _read_history() -> dict:
    """{clé de release: {"at": date ISO, "idx": {clé d'indexer: date ISO}}}.
    Avant la v0.6, la valeur était la seule date ISO (sans le détail par indexer)."""
    try:
        data = json.loads(_history_path().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v if isinstance(v, dict) else {"at": str(v), "idx": {}} for k, v in data.items()}


def routed_history() -> dict:
    """{clé de release: date ISO de la dernière recherche routée}"""
    with _lock:
        return {k: v.get("at") for k, v in _read_history().items() if v.get("at")}


def routed_by_indexer() -> dict:
    """{clé de release: {clé d'indexer: date ISO de la dernière recherche routée sur cet indexer}}"""
    with _lock:
        return {k: v.get("idx") or {} for k, v in _read_history().items()}


def record_routed(key: str, indexers: list = ()) -> None:
    """Note une recherche routée de la release `key`, et les indexers effectivement interrogés."""
    with _lock:
        data = _read_history()
        now = datetime.now().isoformat(timespec="seconds")
        entry = data.setdefault(key, {"at": now, "idx": {}})
        entry["at"] = now
        for k in indexers:
            entry["idx"][k] = now
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
