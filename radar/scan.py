"""Scan mensuel de tout l'univers Swiss Life.

Chaque mois : cours hebdomadaires des fonds sur 3 ans (Yahoo Finance), note glissante
par famille avec les mêmes critères que le classement annuel.
Le classement combine la note 5 ans (60 %) et la note 3 ans glissants (40 %).
Le top 3 évolue automatiquement, avec des garde-fous (voir classer()).

Usage :
    python radar/scan.py             # scan complet (GitHub Actions, une fois par mois)
    python radar/scan.py --offline   # test sur tests/cache/weekly.json, sans réseau
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
POIDS = {"perf36": 0.35, "regul": 0.25, "mdd": 0.15, "frais": 0.15, "perf12": 0.10}
POIDS_5ANS = 0.6   # note du classement = 60 % note 5 ans + 40 % note 3 ans glissants
ECART = 5          # points d'avance exigés sur le plus faible du top 3
NB_MOIS = 2        # nombre de scans mensuels consécutifs avant l'entrée dans le top 3
NOUVEL_ESSAI = 90  # jours avant de rechercher à nouveau un fonds introuvable


def weekly(s: pd.Series) -> pd.Series:
    s = s.dropna()
    idx = pd.to_datetime(s.index)
    s.index = idx.tz_localize(None) if idx.tz is not None else idx
    return s.resample("W-FRI").last().ffill().dropna()


def yahoo_get(path: str, params: dict) -> dict:
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com{path}", params=params, headers=UA, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
    return {}


def historique(symbol: str, plage: str = "5y") -> pd.Series | None:
    j = yahoo_get(f"/v8/finance/chart/{symbol}", {"range": plage, "interval": "1wk"})
    try:
        res = j["chart"]["result"][0]
        close = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or res["indicators"]["quote"][0]["close"]
        s = pd.Series(close, index=pd.to_datetime(res["timestamp"], unit="s"), dtype=float)
        return weekly(s) if s.notna().sum() >= 20 else None
    except Exception:  # noqa: BLE001
        return None


def chercher_symbole(isin: str) -> str | None:
    """Cotation Yahoo la plus fournie pour un ISIN."""
    j = yahoo_get("/v1/finance/search", {"q": isin, "quotesCount": 8, "newsCount": 0})
    best, best_n = None, 0
    for q in (j.get("quotes") or [])[:6]:
        s = historique(q["symbol"], "1y")
        n = 0 if s is None else len(s)
        if n > best_n:
            best, best_n = q["symbol"], n
        if n >= 40:
            break
        time.sleep(0.2)
    return best if best_n >= 20 else None


def pct_rank(s: pd.Series, asc: bool = True) -> pd.Series:
    return s.rank(pct=True, ascending=asc) * 100 if len(s) > 1 else pd.Series(50.0, index=s.index)


def noter(univers: list[dict], prix: dict[str, pd.Series]) -> dict:
    lignes = []
    for f in univers:
        p = prix.get(f["isin"])
        if p is None or len(p) < 104:          # au moins 2 ans de cotations
            continue
        p = p.iloc[-157:]
        n = len(p) - 1
        perf36 = ((p.iloc[-1] / p.iloc[0]) ** (52 / n) - 1) * 100
        perf12 = (p.iloc[-1] / p.iloc[-53] - 1) * 100
        mdd = float(((p / p.cummax()) - 1).min() * 100)
        trims = [(p.iloc[min(i + 13, n)] / p.iloc[i] - 1) * 100 for i in range(n % 13, n, 13)]
        lignes.append(dict(isin=f["isin"], nom=f["nom"], famille=f["famille"], frais=f["frais"],
                           rang_annuel=f["rang_annuel"], note_annuelle=f["note_annuelle"],
                           perf36=round(float(perf36), 2), perf12=round(float(perf12), 2), mdd=round(mdd, 2),
                           trims=trims, semaines=n))
    df = pd.DataFrame(lignes)
    res: dict[str, list] = {}
    if df.empty:
        return res
    for fam, g in df[df.famille.notna()].groupby("famille"):
        g = g.copy()
        nq = min(len(t) for t in g.trims)
        T = pd.DataFrame([t[-nq:] for t in g.trims], index=g.index)
        g["regul"] = pd.concat([pct_rank(T[c]) for c in T.columns], axis=1).mean(axis=1) if nq else 50.0
        sc = (POIDS["perf36"] * pct_rank(g.perf36) + POIDS["regul"] * g.regul + POIDS["mdd"] * pct_rank(g.mdd)
              + POIDS["frais"] * pct_rank(-g.frais.fillna(g.frais.median())) + POIDS["perf12"] * pct_rank(g.perf12))
        g["note"] = sc.round().astype(int)
        g["rang"] = g.note.rank(ascending=False, method="first").astype(int)
        g = g.sort_values("rang")
        res[fam] = [dict(isin=r.isin, nom=r.nom, note=int(r.note), rang=int(r.rang), n=len(g),
                         perf12=r.perf12, perf36=r.perf36, mdd=r.mdd,
                         rang_annuel=None if pd.isna(r.rang_annuel) else int(r.rang_annuel)) for r in g.itertuples()]
    return res


def demi(x: float) -> int:
    return int(np.floor(x + 0.5))


def classer(univers: list[dict], roll: dict, perfs: dict, etat: dict, mois: str) -> tuple[dict, list, list]:
    """Note du classement = 60 % note 5 ans (annuelle) + 40 % note 3 ans glissants (scan).
    Le top 3 évolue avec hystérésis : un fonds entre s'il dépasse le plus faible du top 3
    d'au moins ECART points, NB_MOIS scans mensuels de suite ; un changement par famille et par mois."""
    n3 = {f["isin"]: f["note"] for lst in roll.values() for f in lst}
    fams: dict[str, list] = {}
    for f in univers:
        if f.get("note_annuelle") is None or not f.get("famille") or f["isin"] not in perfs:
            continue
        net5, p25, pire, n5 = perfs[f["isin"]]
        r3 = n3.get(f["isin"])
        note = demi(POIDS_5ANS * n5 + (1 - POIDS_5ANS) * r3) if r3 is not None else demi(n5)
        fams.setdefault(f["famille"], []).append(dict(isin=f["isin"], nom=f["nom"], sg=f.get("sg"), sri=f.get("sri"), devise=f.get("devise"),
                                                      frais=f.get("frais"), net5=net5, p2025=p25, pire=pire, note=note, n5=demi(n5), n3=r3,
                                                      rang_annuel=f.get("rang_annuel")))
    journal, en_lice = [], []
    E = etat.setdefault("familles", {})
    for fam, lst in fams.items():
        lst.sort(key=lambda x: (-x["note"], -x["n5"]))
        by = {x["isin"]: x for x in lst}
        e = E.setdefault(fam, {"top": [x["isin"] for x in sorted(lst, key=lambda x: x["rang_annuel"] or 99)[:3]], "attente": None})
        e["top"] = [i for i in e["top"] if i in by]
        for x in lst:                                   # famille qui a perdu un fonds : on complète
            if len(e["top"]) >= 3:
                break
            if x["isin"] not in e["top"]:
                e["top"].append(x["isin"])
        top = [by[i] for i in e["top"]]
        if top and len(lst) > len(top):
            faible = min(top, key=lambda x: (x["note"], x["n5"]))
            # seul un fonds dont la dynamique 3 ans est mesurée peut entrer dans le top 3
            cand = next((x for x in lst if x["isin"] not in e["top"] and x["n3"] is not None), None)
            if cand and cand["note"] >= faible["note"] + ECART:
                a = e.get("attente") or {}
                if a.get("isin") == cand["isin"]:
                    n = a["n"] if a.get("mois") == mois else a["n"] + 1 if a.get("mois") == mois_prec(mois) else 1
                else:
                    n = 1
                e["attente"] = {"isin": cand["isin"], "n": n, "mois": mois}
                if n >= NB_MOIS:
                    e["top"][e["top"].index(faible["isin"])] = cand["isin"]
                    e["attente"] = None
                    journal.append(dict(date=dt.date.today().isoformat(), famille=fam, entre=cand["nom"], entre_isin=cand["isin"], note_entre=cand["note"],
                                        sort=faible["nom"], sort_isin=faible["isin"], note_sort=faible["note"]))
                else:
                    en_lice.append(dict(famille=fam, nom=cand["nom"], isin=cand["isin"], note=cand["note"], n5=cand["n5"], n3=cand["n3"],
                                        devance=faible["nom"], note_devance=faible["note"], mois=n, sur=NB_MOIS))
            else:
                e["attente"] = None
        tops = set(e["top"])
        ordre = sorted([x for x in lst if x["isin"] in tops], key=lambda x: (-x["note"], -x["n5"])) + [x for x in lst if x["isin"] not in tops]
        for k, x in enumerate(ordre, 1):
            x["rang"], x["n"], x["top"] = k, len(lst), x["isin"] in tops
        fams[fam] = ordre
    etat["journal"] = (journal + etat.get("journal", []))[:60]
    etat["dernier_scan"] = mois
    return fams, journal, en_lice


def mois_prec(m: str) -> str:
    y, mo = map(int, m.split("-"))
    return f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    offline = "--offline" in sys.argv
    univers = json.loads((DATA / "univers.json").read_text("utf-8"))
    carte: dict = {}
    if offline:
        W = json.loads((ROOT / "tests" / "cache" / "weekly.json").read_text())
        prix = {i: pd.Series(v, pd.date_range(s, periods=len(v), freq="7D"), dtype=float).ffill() for i, (s, v) in W.items()}
        couverts = len(prix)
    else:
        carte_path = DATA / "yahoo_map.json"
        carte = json.loads(carte_path.read_text("utf-8")) if carte_path.exists() else {}
        today = dt.date.today()
        prix, couverts = {}, 0
        for k, f in enumerate(univers, 1):
            e = carte.get(f["isin"])
            if e is None or (e.get("s") is None and (today - dt.date.fromisoformat(e.get("d", "2000-01-01"))).days > NOUVEL_ESSAI):
                sym = chercher_symbole(f["isin"])
                carte[f["isin"]] = e = {"s": sym, "d": today.isoformat()}
            if e.get("s"):
                p = historique(e["s"])
                if p is not None:
                    prix[f["isin"]] = p
                    couverts += 1
                time.sleep(0.25)
            if k % 100 == 0:
                print(f"  {k}/{len(univers)} fonds traités, {couverts} avec cotations")
        carte_path.write_text(json.dumps(carte, ensure_ascii=False, indent=0), "utf-8")

    roll = noter(univers, prix)
    perfs = json.loads((DATA / "perfs.json").read_text("utf-8"))
    etat_path = ROOT / ("tests/top3_etat_offline.json" if offline else "data/top3_etat.json")
    etat = json.loads(etat_path.read_text("utf-8")) if etat_path.exists() else {}
    mois = dt.date.today().strftime("%Y-%m")
    fams, journal, en_lice = classer(univers, roll, perfs, etat, mois)
    notes = sum(len(v) for v in roll.values())
    today = dt.date.today().isoformat()
    sortie = ROOT / ("tests" if offline else "web")
    classement = dict(date=today, poids_5ans=POIDS_5ANS, ecart=ECART, nb_mois=NB_MOIS, familles=fams,
                      journal=etat["journal"], en_lice=en_lice)
    (sortie / ("classement_offline.json" if offline else "classement.json")).write_text(
        json.dumps(classement, ensure_ascii=False, separators=(",", ":")), "utf-8")
    top3 = dict(source=f"Classement NSI du {today} (60 % note 5 ans, 40 % note 3 ans glissants)",
                familles={fam: [dict(isin=x["isin"], nom=x["nom"], sg=x["sg"], sri=x["sri"], devise=x["devise"], rang=x["rang"], note=x["note"],
                                     n=x["n"], net5=x["net5"], p2025=x["p2025"], pire=x["pire"], frais=x["frais"]) for x in lst if x["top"]]
                          for fam, lst in fams.items()})
    (sortie / ("top3_offline.json" if offline else "top3.json")).write_text(json.dumps(top3, ensure_ascii=False, indent=1), "utf-8")
    scan = dict(date=today, total=len(univers), couverts=couverts, notes=notes, changements=journal, en_lice=en_lice)
    (sortie / ("scan_offline.json" if offline else "scan.json")).write_text(json.dumps(scan, ensure_ascii=False, separators=(",", ":")), "utf-8")
    etat_path.write_text(json.dumps(etat, ensure_ascii=False, indent=1), "utf-8")
    if not offline:
        maj_suivis(fams, carte)
    print(f"Scan : {couverts}/{len(univers)} fonds cotés, {notes} notés sur 3 ans, {len(journal)} changement(s) du top 3, {len(en_lice)} fonds en lice")


def maj_suivis(fams: dict, carte: dict) -> None:
    """Le radar du lundi suit le top 3 à jour."""
    path = DATA / "fonds_suivis.json"
    ancien = {f["isin"]: f for f in json.loads(path.read_text("utf-8"))}
    neuf = []
    for fam, lst in fams.items():
        for x in lst:
            if not x["top"]:
                continue
            f = ancien.get(x["isin"]) or dict(isin=x["isin"], nom=x["nom"], famille=fam, symbole=(carte.get(x["isin"]) or {}).get("s"),
                                               correspondance="exacte", societe=x["sg"], sri=x["sri"])
            f.update(rang=float(x["rang"]), note=x["note"])
            neuf.append(f)
    # familles absentes du classement (aucun fonds éligible) : on garde le suivi existant
    vues = set(fams)
    neuf += [f for f in ancien.values() if f["famille"] not in vues]
    path.write_text(json.dumps(neuf, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
