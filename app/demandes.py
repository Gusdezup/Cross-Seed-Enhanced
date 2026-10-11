"""Demandes de recherche : « cherche-moi des releases (critères) à envoyer vers (tels trackers) ».

Critères (tous facultatifs, combinés) :
  groups      noms de groupes de trackers : releases dont la catégorie relève de ces groupes
  categories  catégories qBittorrent ("" = sans catégorie) ; groupes et catégories s'additionnent
  keys        releases cochées à la main dans la liste : remplacent tous les autres critères
  text        mots présents dans le nom (tous)
  state       "available" (cross-seed trouvé, pas injecté), "noorig" (sans torrent d'origine),
              "dup" (plusieurs copies sur un même tracker)
  on_tracker  nom d'un tracker où la release est présente dans qBittorrent
  rule        étiquette d'une règle prioritaire
  max_seeds   en seed sur au plus N trackers
  fresh       {"mode": "all" | "never" | "days", "days": N} : sur au moins un tracker cible, jamais
              cherchée (never) ou pas cherchée depuis N jours (days)
  targets     clés d'indexers à interroger ; vide = indexers du groupe de chaque release (et, pour une
              catégorie rangée dans aucun groupe, recherche par cross-seed sur tous ses indexers)
  limit       nombre maximal de releases (0 = toutes)

Une release déjà en seed sur tous ses trackers cibles n'est jamais retenue : il n'y a rien à y envoyer.
"""
from datetime import datetime, timedelta

from . import config, releases

FRESH_MODES = ("all", "never", "days")
STATES = ("available", "noorig", "dup")


def _strs(v) -> list:
    return [str(x).strip() for x in v if isinstance(x, (str, int, float))] if isinstance(v, list) else []


def normalize(body: dict) -> dict:
    body = body if isinstance(body, dict) else {}
    fresh = body.get("fresh") if isinstance(body.get("fresh"), dict) else {}
    mode = fresh.get("mode") if fresh.get("mode") in FRESH_MODES else "all"

    def num(v, lo, hi, default):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default
    return {
        "groups": [g for g in _strs(body.get("groups")) if g],
        # "" (sans catégorie) est une valeur permise : on ne filtre pas les chaînes vides
        "categories": list(dict.fromkeys(str(c).strip() for c in body.get("categories") or []
                                         if isinstance(c, str))),
        "keys": [k for k in _strs(body.get("keys")) if k],
        "text": str(body.get("text") or "").strip(),
        "rule": str(body.get("rule") or "").strip(),
        "state": body.get("state") if body.get("state") in STATES else "",
        "on_tracker": str(body.get("on_tracker") or "").strip(),
        "max_seeds": None if body.get("max_seeds") in (None, "") else num(body.get("max_seeds"), 0, 999, None),
        "fresh": {"mode": mode, "days": num(fresh.get("days"), 1, 3650, 30)},
        "targets": list(dict.fromkeys(k for k in _strs(body.get("targets")) if k)),
        "limit": num(body.get("limit"), 0, 100000, 0),
    }


def _in_scope(r: dict, crit: dict, groups: list) -> bool:
    if crit["keys"]:
        return r["key"] in crit["keys"]
    if not crit["groups"] and not crit["categories"]:
        return True
    cat = (r.get("category") or "").strip().lower()
    if any(c.lower() == cat for c in crit["categories"]):
        return True
    if crit["groups"]:
        g = config.group_for(r.get("category"), groups)
        return bool(g) and g["name"].lower() in {n.lower() for n in crit["groups"]}
    return False


def _passes(r: dict, crit: dict, terms: list) -> bool:
    """Filtres « Affiner » qui ne dépendent pas des trackers cibles (mêmes règles que la liste affichée)."""
    if terms and not all(t in r["name"].lower() for t in terms):
        return False
    if crit["rule"] and crit["rule"] not in r["rules"]:
        return False
    if crit["max_seeds"] is not None and r["seeds"] > crit["max_seeds"]:
        return False
    st = crit["state"]
    if (st == "available" and not r.get("available")) or (st == "noorig" and r["has_original"]) \
            or (st == "dup" and len({c["tracker"] for c in r["copies"]}) == len(r["copies"])):
        return False
    if crit["on_tracker"] and not any(c["tracker"] == crit["on_tracker"] for c in r["copies"]):
        return False
    return True


def targets_for(r: dict, crit: dict, groups: list, active: dict):
    """(clés d'indexers cibles, mode) : mode "explicit" (demande), "group" (groupe de la catégorie)
    ou "xs" (aucun groupe : cross-seed interroge tous ses indexers ; les cibles listées sont alors tous
    les indexers actifs, pour juger de l'état de la release)."""
    if crit["targets"]:
        return [k for k in crit["targets"] if k in active], "explicit"
    g = config.group_for(r.get("category"), groups)
    if g and g["indexers"]:
        return [k for k in g["indexers"] if k in active], "group"
    return list(active), "xs"


def select(items: list, crit: dict, settings: dict, active: dict) -> dict:
    """Releases retenues par la demande, dans l'ordre de passage.
    active : {clé d'indexer actif dans config.js: nom affiché (même nom que dans la colonne Trackers)}."""
    groups = settings["groups"]
    terms = crit["text"].lower().split()
    limit_date = None
    if crit["fresh"]["mode"] == "days":
        limit_date = (datetime.now() - timedelta(days=crit["fresh"]["days"])).isoformat(timespec="seconds")
    counts = {"scope": 0, "seeded": 0, "recent": 0, "filtered": 0, "no_target": 0, "xs": 0}
    picked = []
    for r in items:
        if not _in_scope(r, crit, groups):
            continue
        counts["scope"] += 1
        if not crit["keys"] and not _passes(r, crit, terms):
            counts["filtered"] += 1
            continue
        keys, mode = targets_for(r, crit, groups, active)
        if not keys:
            counts["no_target"] += 1
            continue
        states = {k: releases.target_state(r, active[k]) for k in keys}
        open_ = {k: s for k, s in states.items() if s[0] != "seed"}
        if not open_:
            counts["seeded"] += 1
            continue
        fm = "all" if crit["keys"] else crit["fresh"]["mode"]
        if fm == "never" and not any(s == "never" for s, _ in open_.values()):
            counts["recent"] += 1
            continue
        if fm == "days" and not any(s == "never" or not last or last < limit_date for s, last in open_.values()):
            counts["recent"] += 1
            continue
        if mode == "xs":
            counts["xs"] += 1
        picked.append(r)
    order = {config.rule_label(x): n for n, x in enumerate(settings["rules"]) if x.get("enabled", True)}
    picked.sort(key=lambda r: (min((order.get(n, 99) for n in r["rules"]), default=99),
                               r["seeds"], r.get("last_search") or "", r["name"].lower()))
    total = len(picked)
    if crit["limit"]:
        picked = picked[: crit["limit"]]
    return {"releases": picked, "matched": total, "counts": counts}


def label(crit: dict, active: dict) -> tuple:
    """(titre court, cibles lisibles) d'une demande, pour la file."""
    if crit["keys"]:
        return (f"Sélection de {len(crit['keys'])} release{'s' if len(crit['keys']) > 1 else ''}",
                ", ".join(active.get(k, k) for k in crit["targets"]) if crit["targets"] else "selon le groupe")
    else:
        parts = [f"groupe {g}" for g in crit["groups"]] + \
                [c if c else "sans catégorie" for c in crit["categories"]]
        what = ", ".join(parts) or "Toutes les releases"
    extra = []
    if crit["text"]:
        extra.append(f"« {crit['text']} »")
    if crit["rule"]:
        extra.append(f"règle {crit['rule']}")
    if crit["state"]:
        extra.append({"available": "cross-seed trouvé non injecté", "noorig": "sans torrent d'origine",
                      "dup": "copies en double"}[crit["state"]])
    if crit["on_tracker"]:
        extra.append(f"présentes sur {crit['on_tracker']}")
    if crit["max_seeds"] is not None:
        extra.append(f"≤ {crit['max_seeds']} tracker{'s' if crit['max_seeds'] > 1 else ''}")
    if crit["fresh"]["mode"] == "never":
        extra.append("jamais cherchées sur la cible")
    elif crit["fresh"]["mode"] == "days":
        extra.append(f"pas cherchées depuis {crit['fresh']['days']} j")
    if extra:
        what += " · " + ", ".join(extra)
    to = ", ".join(active.get(k, k) for k in crit["targets"]) if crit["targets"] else "selon le groupe"
    return what, to
