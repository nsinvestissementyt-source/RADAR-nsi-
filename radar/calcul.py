"""Calcul hebdomadaire du Radar NSI.

Étapes : récupère les valeurs liquidatives (Yahoo Finance) et les données de
marché (FRED), applique les règles de data/regles.json et écrit web/radar.json,
que la page du radar affiche.

Usage :
    python radar/calcul.py              # calcul complet (GitHub Actions)
    python radar/calcul.py --offline    # test sur les données de tests/cache, sans réseau ni envoi
"""
from __future__ import annotations

import datetime as dt
import io
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
FRED_IDS = {
    "hy": "BAMLHE00EHYIOAS",           # écart de rendement haut rendement euro (ICE BofA)
    "t10": "IRLTLT01FRM156N",          # taux 10 ans France (mensuel, OCDE)
    "estr": "ECBESTRVOLWGTTRMDMNRT",   # €STR (BCE)
    "hicp": "CP0000EZ19M086NEST",      # indice des prix zone euro (Eurostat)
}


def fr(v: float, d: int = 2) -> str:
    return f"{v:.{d}f}".replace(".", ",")


# ---------------------------------------------------------------- données ---
def weekly(s: pd.Series) -> pd.Series:
    s = s.dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None) if getattr(s.index, "tz", None) else pd.to_datetime(s.index)
    return s.resample("W-FRI").last().ffill().dropna()


def fetch_yahoo(symbol: str) -> pd.Series | None:
    """Historique 10 ans d'un fonds. yfinance d'abord, appel direct en secours."""
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(period="10y", interval="1d", auto_adjust=True)
        if h is not None and len(h) > 20:
            return weekly(h["Close"])
    except Exception as e:  # noqa: BLE001
        print(f"  yfinance {symbol}: {e}")
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                             params={"range": "10y", "interval": "1d"}, headers=UA, timeout=20)
            res = r.json()["chart"]["result"][0]
            close = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or res["indicators"]["quote"][0]["close"]
            s = pd.Series(close, index=pd.to_datetime(res["timestamp"], unit="s"), dtype=float)
            if s.notna().sum() > 20:
                return weekly(s)
        except Exception as e:  # noqa: BLE001
            print(f"  direct {host} {symbol}: {e}")
    return None


def fetch_fred(series_id: str) -> pd.Series:
    """Série FRED (un seul essai court : FRED bloque parfois les serveurs de GitHub)."""
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": series_id, "cosd": "2017-01-01"}, headers=UA, timeout=(10, 40))
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "v"]
    df = df[df.v.astype(str) != "."]
    return pd.Series(df.v.astype(float).values, index=pd.to_datetime(df.date))


# Séries équivalentes publiées par la BCE (API publique, sans clé)
ECB_KEYS = {
    "IRLTLT01FRM156N": "IRS/M.FR.L.L40.CI.0000.EUR.N.Z",       # taux 10 ans France
    "ECBESTRVOLWGTTRMDMNRT": "EST/B.EU000A2X2A25.WT",          # €STR
    "CP0000EZ19M086NEST": "ICP/M.U2.N.000000.4.INX",           # indice des prix zone euro
}


def fetch_ecb(series_id: str) -> pd.Series:
    r = requests.get(f"https://data-api.ecb.europa.eu/service/data/{ECB_KEYS[series_id]}",
                     params={"format": "csvdata", "startPeriod": "2017-01-01"}, headers=UA, timeout=(10, 60))
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df = df[["TIME_PERIOD", "OBS_VALUE"]].dropna()
    return pd.Series(df.OBS_VALUE.astype(float).values, index=pd.to_datetime(df.TIME_PERIOD)).sort_index()


MACRO_CACHE = ROOT / "web" / "macro_cache.json"


def fetch_macro() -> tuple[dict[str, pd.Series], list[str]]:
    """Données de marché : BCE d'abord, FRED ensuite, sinon dernière valeur connue (web/macro_cache.json)."""
    cache = json.loads(MACRO_CACHE.read_text("utf-8")) if MACRO_CACHE.exists() else {}
    out, en_cache, fred_ko = {}, [], False
    for k, sid in FRED_IDS.items():
        sources = ([("BCE", fetch_ecb)] if sid in ECB_KEYS else []) + ([] if fred_ko else [("FRED", fetch_fred)])
        s = None
        for nom, fn in sources:
            try:
                s = fn(sid)
                if len(s) > 10:
                    print(f"  {k} : {nom} OK ({s.index[-1].date()})")
                    break
                s = None
            except Exception as e:  # noqa: BLE001
                print(f"  {k} : {nom} indisponible ({type(e).__name__})")
                if nom == "FRED":
                    fred_ko = True
        if s is not None:
            cache[sid] = [[d.strftime("%Y-%m-%d"), float(v)] for d, v in s.items()]
        elif sid in cache:
            print(f"  {k} : dernière valeur connue utilisée")
            en_cache.append(k)
        else:
            raise RuntimeError(f"Aucune source pour {k}")
        out[k] = pd.Series({pd.Timestamp(a): b for a, b in cache[sid]}).sort_index()
    MACRO_CACHE.write_text(json.dumps(cache, separators=(",", ":")), "utf-8")
    return out, en_cache


# ----------------------------------------------------------------- calcul ---
def compute(fonds: list[dict], regles: dict, familles: list[str],
            prix: dict[str, pd.Series], macro_raw: dict[str, pd.Series]) -> dict:
    S = regles["seuils"]; FM = regles["familles_marche"]; MAN = set(regles["familles_manuelles"]); MK = regles["marche"]

    hy = weekly(macro_raw["hy"]); t10 = macro_raw["t10"].sort_index(); estr = macro_raw["estr"].sort_index()
    hicp = macro_raw["hicp"].sort_index(); infl = (hicp / hicp.shift(12) - 1) * 100
    macro = {
        "t10": {"v": float(t10.iloc[-1]), "d": t10.index[-1].strftime("%Y-%m"), "chg3m": round(float(t10.iloc[-1] - t10.iloc[-4]), 2),
                "hist": [[i.strftime("%Y-%m"), float(v)] for i, v in t10["2021":].items()]},
        "hy": {"v": float(hy.iloc[-1]), "d": hy.index[-1].strftime("%Y-%m-%d"), "chg1w": round(float(hy.iloc[-1] - hy.iloc[-2]), 2),
               "chg4w": round(float(hy.iloc[-1] - hy.iloc[-5]), 2), "hist": [[i.strftime("%Y-%m-%d"), float(v)] for i, v in hy.iloc[-160:].items()]},
        "estr": {"v": float(estr.iloc[-1]), "d": estr.index[-1].strftime("%Y-%m-%d"),
                 "hist": [[i.strftime("%Y-%m-%d"), float(v)] for i, v in weekly(estr)["2021":].items()]},
        "infl": {"v": round(float(infl.dropna().iloc[-1]), 2), "d": infl.dropna().index[-1].strftime("%Y-%m"),
                 "hist": [[i.strftime("%Y-%m"), round(float(v), 2)] for i, v in infl["2021":].dropna().items()]},
    }
    taux_en_hausse = macro["t10"]["chg3m"] > 0

    def taux_hausse_a(date: pd.Timestamp) -> bool:
        s = t10[t10.index <= date]
        return bool(len(s) >= 4 and s.iloc[-1] - s.iloc[-4] > 0)

    def etat_marche(kind: str) -> tuple[str, str]:
        if kind == "taux":
            v = macro["t10"]["v"]
            if v >= MK["taux_10ans_favorable"]:
                return "favorable", f"Taux 10 ans France à {fr(v)} % : niveau de rendement élevé, favorable aux supports obligataires et datés."
            return "neutre", f"Taux 10 ans France à {fr(v)} %."
        if kind == "hy":
            v, c = macro["hy"]["v"], macro["hy"]["chg4w"]
            if v >= MK["hy_renforcement"]:
                return "renfort", f"Écart de rendement à {fr(v)} pt : niveau de crise, point d'entrée fort."
            if c >= MK["hy_elargissement_4sem"]:
                return "zone", f"Écart de rendement passé à {fr(v)} pt (+{fr(c)} pt en 4 semaines) : élargissement rapide, point d'entrée à étudier."
            if v < MK["hy_cher"]:
                return "prudence", f"Écart de rendement à {fr(v)} pt : le haut rendement est cher, ne pas renforcer."
            return "neutre", f"Écart de rendement à {fr(v)} pt."
        e, i = macro["estr"]["v"], macro["infl"]["v"]
        if e < i:
            return "prudence", f"€STR à {fr(e)} % sous l'inflation ({fr(i, 1)} % sur un an) : le monétaire perd du pouvoir d'achat, à limiter à la trésorerie d'attente."
        return "neutre", f"€STR à {fr(e)} %, au-dessus de l'inflation ({fr(i, 1)} %)."

    def r1(x):
        return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 1)

    FUNDS, EVENTS, MANQUANTS = [], [], []
    for f in fonds:
        fam = f["famille"]
        rec = dict(isin=f["isin"], nom=f["nom"], famille=fam, rang=f["rang"], corr=f["correspondance"], note=f.get("note"),
                   sym=f.get("symbole"), sg=f.get("societe"), sri=f.get("sri"))
        p = prix.get(f["isin"])
        if fam in MAN:
            rec["etat"] = "manuel"; FUNDS.append(rec); continue
        if not f.get("symbole"):
            rec["etat"] = "non_suivi"; FUNDS.append(rec); continue
        if p is None or len(p) < 30:
            rec["etat"] = "historique_court" if p is not None else "indisponible"
            if p is None:
                MANQUANTS.append(f["nom"])
            FUNDS.append(rec); continue
        z, r, lab = S.get(fam, (None, None, None))
        hi = p.rolling(52, min_periods=20).max(); dd = (p / hi - 1) * 100
        f13 = (p.shift(-13) / p - 1) * 100; f26 = (p.shift(-26) / p - 1) * 100
        if z:
            inep = renf = False
            for t in range(len(p)):
                d = dd.iloc[t]
                if np.isnan(d):
                    continue
                if not inep and d <= -z:
                    inep, renf = True, False
                    EVENTS.append(dict(isin=f["isin"], nom=f["nom"], famille=fam, type="zone", date=p.index[t].strftime("%Y-%m-%d"),
                                       dd=r1(d), f13=r1(f13.iloc[t]), f26=r1(f26.iloc[t])))
                if inep and not renf and d <= -r:
                    renf = True
                    EVENTS.append(dict(isin=f["isin"], nom=f["nom"], famille=fam, type="renfort", date=p.index[t].strftime("%Y-%m-%d"),
                                       dd=r1(d), f13=r1(f13.iloc[t]), f26=r1(f26.iloc[t])))
                if inep and d > -z / 2:
                    inep = False
        dn = float(dd.iloc[-1])
        etat = ("renfort" if dn <= -r else "zone" if dn <= -z else "surveiller" if dn <= -0.7 * z else "neutre") if z else "macro"
        if fam == "Immobilier coté" and etat in ("zone", "renfort") and taux_en_hausse:
            etat = "suspendu"
        last = p.iloc[-52:]
        rec.update(etat=etat, dd=round(dn, 1), zone=z, renfort=r, regle=lab, last=p.index[-1].strftime("%Y-%m-%d"),
                   perf52=r1((p.iloc[-1] / p.iloc[-53] - 1) * 100) if len(p) > 53 else None,
                   perf13=r1((p.iloc[-1] / p.iloc[-14] - 1) * 100),
                   ext=r1((p.iloc[-1] / p.rolling(40).mean().iloc[-1] - 1) * 100),
                   spark=[round(float(v / last.iloc[-1] * 100), 2) for v in last], hi=round(float(hi.iloc[-1] / p.iloc[-1] * 100), 2))
        FUNDS.append(rec)

    for e in EVENTS:
        if e["famille"] == "Immobilier coté":
            e["suspendu"] = taux_hausse_a(pd.Timestamp(e["date"]))

    prev = False
    for i in range(4, len(hy)):
        c = hy.iloc[i] - hy.iloc[i - 4]; on = c >= MK["hy_elargissement_4sem"]
        if on and not prev:
            EVENTS.append(dict(isin=None, nom="Écart haut rendement européen", famille="Obligations haut rendement", type="zone",
                               date=hy.index[i].strftime("%Y-%m-%d"), dd=None, macro=f"{fr(hy.iloc[i])} pt (+{fr(c)} en 4 sem.)", f13=None, f26=None))
        prev = on

    PRI = {"renfort": 0, "zone": 1, "suspendu": 2, "surveiller": 3, "favorable": 4, "prudence": 5, "neutre": 6, "macro": 7, "manuel": 8,
           "historique_court": 9, "indisponible": 10, "non_suivi": 11}
    FAMS = []
    for fam in familles:
        lst = [f for f in FUNDS if f["famille"] == fam]
        msg = None
        if fam in FM:
            st, msg = etat_marche(FM[fam])
        elif fam in MAN:
            st, msg = "manuel", "Supports non cotés : suivi trimestriel du prix de part et du taux de distribution, à saisir manuellement."
        else:
            sts = [f["etat"] for f in lst if f["etat"] in PRI]
            st = min(sts, key=lambda s: PRI[s]) if sts else "non_suivi"
            if st == "suspendu":
                msg = "Baisse suffisante pour un point d'entrée, mais les taux longs montent encore : signal suspendu."
        z, r, lab = S.get(fam, (None, None, None))
        FAMS.append(dict(famille=fam, etat=st, msg=msg, regle=lab, zone=z, renfort=r))

    EVENTS.sort(key=lambda e: e["date"], reverse=True)
    asof = max(f.get("last", "") for f in FUNDS)
    return dict(asof=asof, macro=macro, fams=FAMS, funds=FUNDS, events=EVENTS, manquants=MANQUANTS,
                calcule_le=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


# -------------------------------------------------------------- résumé ---
def nouvelles_alertes(radar: dict, jours: int = 7) -> list[dict]:
    lim = (pd.Timestamp(radar["asof"]) - pd.Timedelta(days=jours)).strftime("%Y-%m-%d")
    return [e for e in radar["events"] if e["date"] > lim and not e.get("suspendu")]


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    offline = "--offline" in sys.argv
    fonds = json.loads((DATA / "fonds_suivis.json").read_text("utf-8"))
    regles = json.loads((DATA / "regles.json").read_text("utf-8"))
    familles = json.loads((DATA / "familles.json").read_text("utf-8"))

    en_cache = []
    if offline:
        cache = ROOT / "tests" / "cache"
        W = json.loads((cache / "weekly.json").read_text())
        prix = {i: pd.Series(v, pd.date_range(s, periods=len(v), freq="7D"), dtype=float).ffill() for i, (s, v) in W.items()}
        M = json.loads((cache / "macro.json").read_text())
        macro_raw = {k: pd.Series({pd.Timestamp(a): b for a, b in M[sid]}).sort_index() for k, sid in FRED_IDS.items()}
    else:
        prix = {}
        for f in fonds:
            if f.get("symbole"):
                prix[f["isin"]] = fetch_yahoo(f["symbole"]); time.sleep(0.4)
        macro_raw, en_cache = fetch_macro()
        if en_cache:
            print('Données de marché reprises du cache :', ', '.join(en_cache))

    radar = compute(fonds, regles, familles, prix, macro_raw)
    radar["macro_en_cache"] = en_cache
    ok = sum(1 for f in radar["funds"] if f.get("spark"))
    print(f"Données au {radar['asof']} · {ok} fonds calculés · {len(radar['manquants'])} indisponibles")

    if offline:
        out = ROOT / "tests" / "radar_offline.json"
        out.write_text(json.dumps(radar, ensure_ascii=False), "utf-8")
        print("Écrit :", out)
        return

    if ok < 0.6 * sum(1 for f in fonds if f.get("symbole")):
        raise SystemExit("Trop de fonds indisponibles : le radar précédent est conservé (Yahoo a probablement bloqué l'accès).")
    (ROOT / "web" / "radar.json").write_text(json.dumps(radar, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print("Radar publié : web/radar.json")


if __name__ == "__main__":
    main()
