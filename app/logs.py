"""Lecture des logs cross-seed : fin de fichier, suivi en direct, résultat d'une recherche."""
import os
import re
from pathlib import Path

from . import config

ENTRY_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
TYPES = ("info", "verbose", "error")


def log_files(kind: str) -> list:
    """Fichiers datés du plus ancien au plus récent."""
    if not config.LOGS_DIR.is_dir():
        return []
    return sorted(p for p in config.LOGS_DIR.glob(f"{kind}.*.log")
                  if re.search(r"\d{4}-\d{2}-\d{2}", p.name))


def current_file(kind: str):
    cur = config.LOGS_DIR / f"{kind}.current.log"
    if cur.exists():
        return cur
    files = log_files(kind)
    return files[-1] if files else None


def file_id(p: Path):
    try:
        st = os.stat(p)
        return (st.st_ino, st.st_dev)
    except OSError:
        return None


def read_tail(p: Path, max_bytes: int) -> str:
    size = p.stat().st_size
    with open(p, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        return f.read().decode("utf-8", "replace")


def group_entries(text: str) -> list:
    """Recolle les entrées multi-lignes (objets JSON affichés par cross-seed)."""
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if ENTRY_START.match(line) or not entries:
            entries.append(line)
        else:
            entries[-1] += "\n" + line
    return entries


def tail_entries(kind: str, n: int = 300) -> list:
    cur = current_file(kind)
    if not cur:
        return []
    entries = group_entries(read_tail(cur, 4_000_000))
    if len(entries) < n:
        dated = [p for p in log_files(kind) if file_id(p) != file_id(cur)]
        for p in reversed(dated[-3:]):
            entries = group_entries(read_tail(p, 4_000_000)) + entries
            if len(entries) >= n:
                break
    return entries[-n:]


def read_from(kind: str, offset: int, ident) -> tuple:
    """Lit à partir d'un offset ; repart de zéro si le fichier a tourné."""
    cur = current_file(kind)
    if not cur:
        return "", 0, None
    fid = file_id(cur)
    size = cur.stat().st_size
    if fid != ident or size < offset:
        offset = 0
    with open(cur, "rb") as f:
        f.seek(offset)
        data = f.read(8_000_000)
    return data.decode("utf-8", "replace"), offset + len(data), fid


def position(kind: str) -> tuple:
    cur = current_file(kind)
    if not cur:
        return 0, None
    return cur.stat().st_size, file_id(cur)


# --- Analyse du résultat d'une recherche webhook -----------------------------

def _ident_pattern(payload: dict) -> str:
    if "infoHash" in payload:
        return re.escape(payload["infoHash"][:8])
    return re.escape(payload.get("path", ""))


def analyse(text: str, payload: dict):
    """Renvoie un dict résultat si la recherche est terminée dans `text`, sinon None."""
    ident = _ident_pattern(payload)
    if not ident:
        return None
    res = {"found": None, "injected": [], "failed": [], "skipped": None, "refused": None}

    for m in re.finditer(r"Found .+? on (.+?) by \S+ from \S+ \((.*?)\) - injected", text):
        if re.search(ident, m.group(2)):
            res["injected"].append(m.group(1))
    for m in re.finditer(r"Found .+? on (.+?) by \S+ from \S+ \((.*?)\) - failed to inject", text):
        if re.search(ident, m.group(2)):
            res["failed"].append(m.group(1))
    m = re.search(r"Skipped searching on indexers for [^\n]*?" + ident + r"[^\n]*?\(filtered by ([^)]*)\)", text)
    if m:
        res["skipped"] = m.group(1)
    m = re.search(r"Did not search for [^\n]*?" + ident + r"[^\n]*? - ([^\n]+)", text)
    if m:
        res["refused"] = m.group(1).strip()
    m = re.search(r"Found (\d+) torrents? for \{\s*(?:infoHash|path): '" + ident, text)
    if m:
        res["found"] = int(m.group(1))
        return res
    if res["refused"]:
        res["found"] = 0
        return res
    return None


# --- Erreurs liées à un fichier en attente d'injection ------------------------

def errors_for(hash8: str, limit: int = 3) -> list:
    cur = current_file("error")
    if not cur:
        return []
    hits = [e for e in group_entries(read_tail(cur, 2_000_000)) if hash8 in e]
    return hits[-limit:]


# --- Noms et pauses des indexers déduits des logs -----------------------------

def indexer_names_from_logs() -> dict:
    names = {}
    for kind in ("verbose",):
        files = log_files(kind)[-2:]
        cur = current_file(kind)
        if cur and cur not in files:
            files.append(cur)
        for p in files:
            try:
                text = read_tail(p, 3_000_000)
            except OSError:
                continue
            for m in re.finditer(r"Querying (.+?) at (https?://\S+?) with", text):
                names[normalize_url(m.group(2))] = m.group(1)
    return names


def snoozes_from_logs() -> dict:
    out = {}
    cur = current_file("info")
    if not cur:
        return out
    text = read_tail(cur, 3_000_000)
    for m in re.finditer(r"(?:warn|info): (.+?) was rate limited[^\n]*?snoozing until (\S+ \S+)", text):
        out[m.group(1).strip()] = m.group(2)
    return out


def normalize_url(url: str) -> str:
    url = url.split("?")[0].rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    return url
