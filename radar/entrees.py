"""Points d'entrée hebdomadaires de tous les fonds classés.

Chaque lundi, après le radar : baisse de chaque fonds depuis son plus haut des 52 dernières
semaines, comparée aux seuils de sa famille (data/regles.json) et à la baisse médiane de la
famille. Écrit web/entrees.json, lu par la page Sélection et par les portefeuilles
(versement initial).

Usage :
    python radar/entrees.py             # calcul complet (GitHub Actions)
    python radar/entrees.py --offline   # test sur tests/cache/weekly.json, sans réseau
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan import historique  # noqa: E402  (même accès Yahoo que le scan mensuel)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SPECIFIQUE = 1.5   # baisse > 1,5 x la médiane de la famille...
ECART_MIN = 5      # ... et au moins 5 points de plus : baisse propre au fonds, pas au marché


def etat(dd: float, z: float, r: float) -> str:
    return "renfort" if dd <= -r else "zone" if dd <= -z else "approche" if dd <= -z / 2 else ""


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    offline = "--offline" in sys.argv
    univers = json.loads((DATA / "univers.json").read_text("utf-8"))
    seuils = json.loads((DATA / "regles.json").read_text("utf-8"))["seuils"]
    fonds = [f for f in univers if f.get("note_annuelle") is not None and f.get("famille") in seuils]

    if offline:
        W = json.loads((ROOT / "tests" / "cache" / "weekly.json").read_text())
        prix = {i: pd.Series(v, pd.date_range(s, periods=len(v), freq="7D"), dtype=float).ffill() for i, (s, v) in W.items()}
    else:
        carte = json.loads((DATA / "yahoo_map.json").read_text("utf-8"))
        prix = {}
        for k, f in enumerate(fonds, 1):
            sym = (carte.get(f["isin"]) or {}).get("s")
            if sym:
                p = historique(sym, "1y")
                if p is not None:
                    prix[f["isin"]] = p
                time.sleep(0.25)
            if k % 100 == 0:
                print(f"  {k}/{len(fonds)} fonds traités, {len(prix)} avec cotations")

    dds: dict[str, float] = {}
    for f in fonds:
        p = prix.get(f["isin"])
        if p is None or len(p) < 26:
            continue
        p = p.iloc[-53:]
        dds[f["isin"]] = round(float((p.iloc[-1] / p.max() - 1) * 100), 1)

    med = {}
    for fam in seuils:
        v = [dds[f["isin"]] for f in fonds if f["famille"] == fam and f["isin"] in dds]
        if v:
            med[fam] = round(float(np.median(v)), 1)

    out = {}
    for f in fonds:
        if f["isin"] not in dds:
            continue
        fam, dd = f["famille"], dds[f["isin"]]
        z, r, _ = seuils[fam]
        e = etat(dd, z, r)
        m = med.get(fam, 0.0)
        propre = bool(e and dd < SPECIFIQUE * m and dd < m - ECART_MIN)
        out[f["isin"]] = {"dd": dd, "e": e, **({"p": 1} if propre else {})}

    if not offline and len(out) < 0.6 * len([f for f in fonds if (carte.get(f["isin"]) or {}).get("s")]):
        raise SystemExit("Trop de fonds indisponibles : les points d'entrée précédents sont conservés.")
    res = dict(date=dt.date.today().isoformat(), seuils={k: v[:2] for k, v in seuils.items()}, mediane=med, fonds=out,
               regle=f"Baisse propre au fonds : plus de {SPECIFIQUE} fois la baisse médiane de la famille et {ECART_MIN} points de plus")
    dest = ROOT / ("tests/entrees_offline.json" if offline else "web/entrees.json")
    dest.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), "utf-8")
    n = {k: sum(1 for v in out.values() if v["e"] == k) for k in ("renfort", "zone", "approche")}
    print(f"Points d'entrée : {len(out)} fonds mesurés · {n['renfort']} renforcement · {n['zone']} point d'entrée · {n['approche']} en approche -> {dest.name}")


if __name__ == "__main__":
    main()
