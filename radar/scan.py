"""Scan mensuel de tout l'univers Swiss Life.

Chaque mois : cours hebdomadaires des fonds sur 3 ans (Yahoo Finance), note glissante
par famille avec les mêmes critères que le classement annuel, puis repérage :
- des challengers : fonds hors top 3 dont la note glissante dépasse nettement celle du top 3 ;
- des décrochages : fonds du top 3 tombés loin dans le classement glissant.
Le top 3 officiel ne change pas : c'est une alerte pour le comité.

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
MARGE = 15         # points de note glissante d'avance exigés pour un challenger
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


def alertes(familles: dict) -> tuple[list, list]:
    chall, decr = [], []
    for fam, lst in familles.items():
        top = [f for f in lst if f["rang_annuel"] and f["rang_annuel"] <= 3]
        if not top:
            continue
        seuil = min(f["note"] for f in top)
        for f in lst:
            hors_top = not f["rang_annuel"] or f["rang_annuel"] > 3
            if hors_top and f["rang"] <= 2 and f["note"] >= seuil + MARGE:
                chall.append(dict(f, famille=fam, ecart=f["note"] - seuil,
                                  devance=min(top, key=lambda x: x["note"])["nom"]))
                break  # un seul challenger par famille : le mieux classé
        if len(lst) >= 6:
            for f in top:
                if f["rang"] > len(lst) / 2:
                    decr.append(dict(f, famille=fam))
    chall.sort(key=lambda x: -x["ecart"])
    return chall, decr


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    offline = "--offline" in sys.argv
    univers = json.loads((DATA / "univers.json").read_text("utf-8"))
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

    familles = noter(univers, prix)
    chall, decr = alertes(familles)
    notes = sum(len(v) for v in familles.values())
    out = dict(date=dt.date.today().isoformat(), total=len(univers), couverts=couverts, notes=notes,
               marge=MARGE, challengers=chall, decrochages=decr,
               familles={k: v[:10] for k, v in familles.items()})
    dest = ROOT / ("tests/scan_offline.json" if offline else "web/scan.json")
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"Scan : {couverts}/{len(univers)} fonds cotés, {notes} notés, {len(chall)} challengers, {len(decr)} décrochages -> {dest.name}")


if __name__ == "__main__":
    main()
