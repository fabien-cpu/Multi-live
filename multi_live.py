#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi Live — tableau de bord en temps réel pour préparer tes paris hippiques PMU.

Lance :   python multi_live.py      puis ouvre http://localhost:8765
(En ligne, l'hébergeur fournit le port par la variable PORT.)

Ce que fait la page :
  - Tu choisis la course, le type de pari (Simple, Couplé, 2 sur 4, Trio, Tiercé, Quarté+, Quinté+,
    Multi, Pick 5...) et ta mise. Seuls les paris proposés sur la course sont listés.
  - Cotes PMU rafraîchies toutes les 20 s (10 s dans les 5 dernières minutes).
  - Quatre analyses croisées : cotes du marché, mouvement des cotes, forme récente, régularité.
  - Ticket conseillé, chance de gagner, rapport probable (vrai rapport PMU quand il est publié,
    sinon estimation calée sur les rapports des jours passés) et verdict.
  - Bilan des 30 derniers jours avec les vrais rapports définitifs du PMU.

Il ne parie jamais : tu joues toi-même.

Options :  --port 8800     --demo (courses fictives, sans Internet)
Python 3.8+ — rien à installer.
"""

import argparse
import base64
import itertools
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://online.turfinfo.api.pmu.fr/rest/client/1/programme"
HEADERS = {"User-Agent": "Mozilla/5.0 (multi-live perso)", "Accept": "application/json"}

HISTO = {}            # (date, r, c) -> {num: [[ts, cote], ...]}
HISTO_LOCK = threading.Lock()
DEMO = False

# Paris gérés (nom PMU sans le préfixe « E_ »). MINI_MULTI est rangé avec MULTI.
PARIS_CONNUS = ["SIMPLE_GAGNANT", "SIMPLE_PLACE", "COUPLE_GAGNANT", "COUPLE_PLACE", "COUPLE_ORDRE",
                "DEUX_SUR_QUATRE", "TRIO", "TRIO_ORDRE", "TIERCE", "QUARTE_PLUS", "QUINTE_PLUS",
                "MULTI", "SUPER_QUATRE", "PICK5"]
# Paris pour lesquels le PMU publie des rapports probables avant la course
AVEC_PROBABLES = {"SIMPLE_PLACE", "COUPLE_GAGNANT", "COUPLE_PLACE", "COUPLE_ORDRE", "DEUX_SUR_QUATRE",
                  "TRIO", "TRIO_ORDRE"}


# ------------------------------------------------------------------ accès PMU

def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def nom_pari(type_pmu):
    """E_MINI_MULTI -> ("MULTI", True) ; E_TRIO -> ("TRIO", False) ; inconnu -> (None, False)."""
    t = str(type_pmu or "").upper()
    if t.startswith("E_"):
        t = t[2:]
    t = t.replace("TIERCÉ", "TIERCE")
    if t == "MINI_MULTI":
        return "MULTI", True
    return (t, False) if t in PARIS_CONNUS else (None, False)


def paris_course(course):
    out, mini = [], False
    for p in course.get("paris", []) or []:
        t, m = nom_pari(p.get("typePari"))
        if t and not any(x["t"] == t for x in out):
            out.append({"t": t, "base": (p.get("miseBase") or 0) / 100})
            mini = mini or m
    out.sort(key=lambda x: PARIS_CONNUS.index(x["t"]))
    return out, mini


def liste_courses(jour):
    if DEMO:
        return demo_programme(jour)
    data = get_json(f"{API}/{jour}?specialisation=INTERNET")
    out = []
    for reu in data.get("programme", {}).get("reunions", []):
        r = reu.get("numOfficiel")
        hippo = (reu.get("hippodrome") or {}).get("libelleCourt", "")
        for c in reu.get("courses", []):
            paris, mini = paris_course(c)
            out.append({
                "r": r, "c": c.get("numOrdre"), "hippodrome": hippo,
                "libelle": c.get("libelle", ""), "heure": c.get("heureDepart"),
                "discipline": c.get("discipline", ""), "distance": c.get("distance"),
                "partants": c.get("nombreDeclaresPartants"), "paris": paris, "mini": mini,
                "statut": c.get("statut", ""),
            })
    out.sort(key=lambda x: x["heure"] or 0)
    return out


def arrivee_course(jour, r, c):
    """Ordre d'arrivée depuis le programme (liste de numéros), ou []."""
    try:
        data = get_json(f"{API}/{jour}/R{r}/C{c}?specialisation=INTERNET")
    except Exception:
        return []
    ordre = data.get("ordreArrivee") or []
    return [n for groupe in ordre for n in (groupe if isinstance(groupe, list) else [groupe])]


def lire_partants(jour, r, c):
    data = get_json(f"{API}/{jour}/R{r}/C{c}/participants?specialisation=INTERNET")
    partants = []
    for p in data.get("participants", []):
        direct = (p.get("dernierRapportDirect") or {}).get("rapport")
        ref = (p.get("dernierRapportReference") or {}).get("rapport")
        partants.append({
            "num": p.get("numPmu"), "nom": p.get("nom", "?"),
            "partant": str(p.get("statut", "PARTANT")).upper() == "PARTANT",
            "musique": p.get("musique", "") or "",
            "driver": p.get("driver") or p.get("jockey") or "",
            "courses": p.get("nombreCourses") or 0,
            "victoires": p.get("nombreVictoires") or 0,
            "places": p.get("nombrePlaces") or 0,
            "coteMatin": float(ref) if ref else None,
            "coteDirect": float(direct) if direct else None,
            "ordreArrivee": p.get("ordreArrivee"),
        })
    arrivee = sorted((p for p in partants if p.get("ordreArrivee")), key=lambda p: p["ordreArrivee"])
    arrivee = [p["num"] for p in arrivee] or arrivee_course(jour, r, c)
    for p in partants:
        p.pop("ordreArrivee", None)
    return partants, arrivee[:5]


def rapports_definitifs(jour, r, c):
    """Rapports définitifs de tous les paris : {TYPE: [{"l": libellé, "c": [numéros], "d": € pour 1 € misé}]}."""
    try:
        data = get_json(f"{API}/{jour}/R{r}/C{c}/rapports-definitifs?specialisation=INTERNET")
    except Exception:
        return {}
    if isinstance(data, dict):   # selon les versions de l'API, la liste peut être rangée dans une clé
        data = next((v for v in data.values() if isinstance(v, list)), [])
    out = {}
    for pari in data:
        t, _ = nom_pari(pari.get("typePari"))
        if not t or pari.get("rembourse"):
            continue
        for rp in pari.get("rapports", []) or []:
            d = rp.get("dividendePourUnEuro")
            try:
                comb = [int(x) for x in str(rp.get("combinaison", "")).split("-")]
            except ValueError:
                continue
            if d:
                out.setdefault(t, []).append({"l": str(rp.get("libelle", "")), "c": comb, "d": d / 100})
    return out


def rapports_probables(jour, r, c, pari):
    """Rapports probables publiés par le PMU avant la course : [[numéros], direct, mini, maxi] pour 1 €."""
    if pari not in AVEC_PROBABLES:
        return None
    try:
        data = get_json(f"{API}/{jour}/R{r}/C{c}/rapports/E_{pari}?specialisation=INTERNET")
    except Exception:
        return None
    out = []
    for rp in data.get("rapportsParticipant", []) or []:
        nums = rp.get("numerosParticipant") or []
        d, mn, mx = rp.get("rapportDirect"), rp.get("minRapportProbable"), rp.get("maxRapportProbable")
        if nums and (d or mn or mx):
            out.append([nums, d, mn, mx])
    return out or None


DEFINITIFS_CACHE = {}


def details_course(jour, r, c, pari):
    if DEMO:
        partants, arrivee = demo_partants()
        probables = demo_probables(partants, pari)
        definitifs = {}
    else:
        partants, arrivee = lire_partants(jour, r, c)
        probables = rapports_probables(jour, r, c, pari) if len(arrivee) < 3 else None
        definitifs = {}
        if len(arrivee) >= 3:
            cle = (jour, r, c)
            definitifs = DEFINITIFS_CACHE.get(cle) or rapports_definitifs(jour, r, c)
            if definitifs:
                DEFINITIFS_CACHE[cle] = definitifs

    # Historique des cotes (gardé en mémoire tant que le programme tourne)
    cle, now = (jour, r, c), int(time.time() * 1000)
    with HISTO_LOCK:
        h = HISTO.setdefault(cle, {})
        for p in partants:
            cote = p.get("coteDirect")
            if not cote:
                continue
            serie = h.setdefault(p["num"], [])
            if not serie or serie[-1][1] != cote or now - serie[-1][0] > 60000:
                serie.append([now, cote])
                del serie[:-120]
        for p in partants:
            p["histo"] = h.get(p["num"], [])
    return {"partants": partants, "arrivee": arrivee, "probables": probables,
            "rapports": definitifs, "maj": now}


# ------------------------------------------------------------------ bilan des jours passés

JOUR_CACHE = {}        # (jour, r, c) -> données de la course (les jours passés ne changent plus)
PROG_CACHE = {}        # jour -> liste des courses


def course_proche(courses, heure, pari):
    h, m = (int(x) for x in heure.split(":"))
    cible = h * 60 + m

    def ecart(c):
        if not c.get("heure"):
            return 10 ** 6
        d = datetime.fromtimestamp(c["heure"] / 1000)
        return abs(d.hour * 60 + d.minute - cible)
    avec = [c for c in courses if any(p["t"] == pari for p in c.get("paris", []))]
    return min(avec, key=ecart) if avec else None


def bilan_jour(jour, heure, pari):
    try:
        if jour not in PROG_CACHE:
            PROG_CACHE[jour] = liste_courses(jour)
        c = course_proche(PROG_CACHE[jour], heure, pari)
        if not c:
            return None
        cle = (jour, c["r"], c["c"])
        if cle not in JOUR_CACHE:
            partants, arrivee = lire_partants(jour, c["r"], c["c"])
            rap = rapports_definitifs(jour, c["r"], c["c"])
            if not rap or len(arrivee) < 3:
                return None
            JOUR_CACHE[cle] = {"date": jour, "r": c["r"], "c": c["c"], "hippodrome": c["hippodrome"],
                               "libelle": c["libelle"], "heure": c["heure"], "mini": c["mini"],
                               "partants": partants, "arrivee": arrivee, "rapports": rap}
        return JOUR_CACHE[cle]
    except Exception:
        return None          # pas mis en cache : on réessaiera


def bilan(jours, heure, pari):
    if DEMO:
        return demo_bilan(jours)
    dates = [(datetime.now() - timedelta(days=i)).strftime("%d%m%Y") for i in range(1, jours + 1)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda d: bilan_jour(d, heure, pari), dates))
    return [x for x in res if x]


# ------------------------------------------------------------------ mode démo

_demo_state = {}
DEMO_PARIS = [{"t": t, "base": b} for t, b in [
    ("SIMPLE_GAGNANT", 1), ("SIMPLE_PLACE", 1), ("COUPLE_GAGNANT", 1), ("COUPLE_PLACE", 1), ("DEUX_SUR_QUATRE", 3),
    ("TIERCE", 1), ("QUARTE_PLUS", 1.5), ("QUINTE_PLUS", 2), ("MULTI", 3)]]


def demo_programme(jour):
    base = datetime.now().replace(hour=13, minute=55, second=0, microsecond=0)
    if datetime.now() > base + timedelta(minutes=20):
        base = datetime.now() + timedelta(minutes=12)
    ms = lambda d: int(d.timestamp() * 1000)
    petit = [{"t": t, "base": 1} for t in ("SIMPLE_GAGNANT", "SIMPLE_PLACE", "COUPLE_GAGNANT", "COUPLE_PLACE", "TRIO")]
    return [
        {"r": 1, "c": 1, "hippodrome": "VINCENNES", "libelle": "PRIX DE BAZOCHES", "heure": ms(base - timedelta(minutes=65)),
         "discipline": "ATTELE", "distance": 2700, "partants": 12, "paris": petit, "mini": False, "statut": "FIN_COURSE"},
        {"r": 1, "c": 3, "hippodrome": "VINCENNES", "libelle": "PRIX DE RUNGIS (démo)", "heure": ms(base),
         "discipline": "ATTELE", "distance": 2850, "partants": 16, "paris": DEMO_PARIS, "mini": False, "statut": "PROGRAMMEE"},
        {"r": 2, "c": 5, "hippodrome": "LONGCHAMP", "libelle": "PRIX DES ETANGS", "heure": ms(base + timedelta(minutes=80)),
         "discipline": "PLAT", "distance": 1600, "partants": 11, "paris": petit + [{"t": "MULTI", "base": 3}, {"t": "PICK5", "base": 1}],
         "mini": True, "statut": "PROGRAMMEE"},
    ]


DEMO_CHEVAUX = [
    (1, "ETOILE DU BETZ", "1a2a(25)3a1aDa", 4.2, "M. ABRIVARD", 31, 8, 14), (2, "ROI DE BEON", "5a4a3a6a2a", 9.5, "E. RAFFIN", 40, 5, 17),
    (3, "JOLIE CHEVILLY", "2a1a1a4a3a", 3.6, "J-M. BAZIRE", 22, 7, 11), (4, "TONNERRE BLEU", "0a7aDa5a8a", 38.0, "D. THOMAIN", 35, 2, 8),
    (5, "VENT D'OUEST", "3a3a2a5a4a", 7.8, "B. ROCHARD", 28, 4, 15), (6, "PRINCE RUNGIS", "6a5a9a0a4a", 22.0, "A. LAMY", 44, 3, 12),
    (7, "BELLE DE MONTEREAU", "1a4a2a2a6a", 6.1, "F. NIVARD", 25, 6, 13), (8, "SULTAN DORE", "8a6aDa7a5a", 31.0, "Y. LEBOURGEOIS", 38, 3, 9),
    (9, "ECLAIR NOIR", "4a2a5a3a1a", 11.0, "G. GELORMINI", 30, 5, 14), (10, "DUC DE PROVINS", "7a0a6a9aDa", 45.0, "P. VERCRUYSSE", 50, 4, 10),
    (11, "FLEUR DES ETANGS", "2a3a4a1a7a", 8.4, "M. MOTTIER", 19, 4, 10), (12, "CAPITAINE SEINE", "9a8a0a6a7a", 55.0, "C. MARTENS", 47, 2, 7),
    (13, "MISTRAL GAGNANT", "3a5a7a4a2a", 15.0, "T. LE BELLER", 27, 3, 12), (14, "ORAGE D'AVRIL", "Da0a5a8a3a", 28.0, "L. ABRIVARD", 33, 3, 9),
    (15, "BRISE DU MATIN", "5a6a3a7a9a", 19.0, "R. DERIEUX", 29, 2, 10), (16, "GRAND FRISSON", "0a0a8aDa6a", 60.0, "H. GUELPA", 41, 1, 5),
]


def demo_partants():
    st = _demo_state
    if not st:
        st.update({n: c for n, _, _, c, *_ in DEMO_CHEVAUX})
    out = []
    for n, nom, mus, matin, drv, crs, vic, pl in DEMO_CHEVAUX:
        drift = random.uniform(-0.06, 0.06) + (-0.02 if n in (7, 11) else 0)  # 7 et 11 « joués »
        st[n] = round(max(1.5, st[n] * (1 + drift)), 1)
        out.append({"num": n, "nom": nom, "partant": n != 16, "musique": mus, "driver": drv,
                    "courses": crs, "victoires": vic, "places": pl,
                    "coteMatin": matin, "coteDirect": st[n] if n != 16 else None})
    return out, []


def _probas(partants):
    inv = {p["num"]: 1 / p["coteDirect"] for p in partants if p.get("coteDirect")}
    s = sum(inv.values())
    return {k: v / s for k, v in inv.items()}


def _seq(p, seq):
    reste, t = 1.0, 1.0
    for x in seq:
        t *= p[x] / reste
        reste -= p[x]
    return t


def _set(p, s):
    return sum(_seq(p, q) for q in itertools.permutations(s))


def demo_probables(partants, pari):
    if pari not in AVEC_PROBABLES:
        return None
    p = _probas(partants)
    nums = sorted(p)
    if pari == "SIMPLE_PLACE":
        return [[[n], None, round(max(1.1, 0.25 / p[n]), 1), round(max(1.3, 0.45 / p[n]), 1)] for n in nums]
    k = 3 if pari.startswith("TRIO") else 2
    out = []
    for comb in itertools.combinations(nums, k):
        juste = 0.75 / _set(p, comb)
        if pari == "COUPLE_PLACE":
            out.append([list(comb), None, round(juste / 4, 1), round(juste / 2.2, 1)])
        elif pari == "DEUX_SUR_QUATRE":
            out.append([list(comb), round(max(1.1, juste / 9), 1), None, None])
        else:
            out.append([list(comb), round(juste, 1), None, None])
    return out


def demo_definitifs(partants, arr, rnd):
    p = _probas(partants)
    cote = {x["num"]: x["coteDirect"] for x in partants}
    bruit = lambda: rnd.uniform(0.6, 1.5)
    r = lambda lib, comb, d: {"l": lib, "c": list(comb), "d": round(max(1.1, d), 2)}
    a3, a4 = arr[:3], arr[:4]
    en4 = 0.75 / _set(p, a4) * bruit()
    return {
        "SIMPLE_GAGNANT": [r("e-Simple Gagnant", arr[:1], cote[arr[0]])],
        "SIMPLE_PLACE": [r("e-Simple Placé", [x], cote[x] / 3.2) for x in a3],
        "COUPLE_GAGNANT": [r("e-Couplé Gagnant", arr[:2], 0.72 / _set(p, arr[:2]) * bruit())],
        "COUPLE_PLACE": [r("e-Couplé Placé", c, 0.72 / _set(p, c) / 3 * bruit()) for c in itertools.combinations(a3, 2)],
        "DEUX_SUR_QUATRE": [r("e-2sur4", c, 4.2) for c in itertools.combinations(a4, 2)],
        "TRIO": [r("e-Trio", a3, 0.72 / _set(p, a3) * bruit())],
        "TIERCE": [r("e-Tiercé Ordre", a3, 0.7 / _seq(p, a3) * bruit()), r("e-Tiercé Désordre", a3, 0.7 / _set(p, a3) * bruit())],
        "QUARTE_PLUS": [r("e-Quarté+ Ordre", a4, 0.7 / _seq(p, a4) * bruit()), r("e-Quarté+ Désordre", a4, 0.7 / _set(p, a4) * bruit()),
                        r("e-Bonus", a3, 0.15 / _set(p, a3))],
        "QUINTE_PLUS": [r("e-Quinté+ Ordre", arr, 0.65 / _seq(p, arr) * bruit()), r("e-Quinté+ Désordre", arr, 0.65 / _set(p, arr) * bruit()),
                        r("e-Bonus 3", a3, 0.08 / _set(p, a3))],
        "PICK5": [r("e-Pick5", arr, 0.7 / _set(p, arr) * bruit())],
        "MULTI": [r("e-Multi en 4", a4, en4), r("e-Multi en 5", a4, en4 / 5), r("e-Multi en 6", a4, en4 / 15), r("e-Multi en 7", a4, en4 / 35)],
    }


def demo_bilan(jours):
    """Jours passés fictifs : arrivée tirée au sort selon les cotes, rapports ~ 70-75 % du juste prix."""
    rnd = random.Random(42)
    out = []
    for i in range(1, jours + 1):
        d = datetime.now() - timedelta(days=i)
        partants = []
        for n, nom, mus, matin, drv, crs, vic, pl in DEMO_CHEVAUX[:rnd.randint(13, 16)]:
            cote = round(matin * rnd.uniform(0.5, 1.8), 1)
            partants.append({"num": n, "nom": nom, "partant": True, "musique": mus, "driver": drv,
                             "courses": crs, "victoires": vic, "places": pl,
                             "coteMatin": round(cote * rnd.uniform(0.85, 1.15), 1), "coteDirect": cote})
        reste, arr = dict(_probas(partants)), []
        for _ in range(5):
            x, acc = rnd.random() * sum(reste.values()), 0
            for k, v in reste.items():
                acc += v
                if acc >= x:
                    arr.append(k)
                    reste.pop(k)
                    break
        out.append({"date": d.strftime("%d%m%Y"), "r": 1, "c": 3, "hippodrome": "VINCENNES", "libelle": "Course démo",
                    "heure": int(d.replace(hour=13, minute=55).timestamp() * 1000), "mini": False,
                    "partants": partants, "arrivee": arr, "rapports": demo_definitifs(partants, arr, rnd)})
    return out


# ------------------------------------------------------------------ serveur web

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def envoyer(self, code, corps, ctype="application/json; charset=utf-8"):
        b = corps.encode("utf-8") if isinstance(corps, str) else corps
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        jour = q.get("date") or datetime.now().strftime("%d%m%Y")
        pari = q.get("pari", "MULTI").upper()
        if pari not in PARIS_CONNUS:
            pari = "MULTI"
        try:
            if u.path == "/":
                return self.envoyer(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/manifest.webmanifest":
                return self.envoyer(200, MANIFEST, "application/manifest+json")
            if u.path in ("/icon-192.png", "/icon-512.png"):
                data = base64.b64decode(ICON192 if "192" in u.path else ICON512)
                return self.envoyer(200, data, "image/png")
            if u.path == "/sw.js":
                return self.envoyer(200, SW, "text/javascript; charset=utf-8")
            if u.path == "/api/courses":
                return self.envoyer(200, json.dumps({"date": jour, "demo": DEMO, "courses": liste_courses(jour)}))
            if u.path == "/api/bilan":
                jours = max(1, min(60, int(q.get("jours", 30))))
                heure = q.get("heure", "13:55")
                return self.envoyer(200, json.dumps({"jours": jours, "heure": heure, "pari": pari,
                                                     "courses": bilan(jours, heure, pari)}))
            if u.path == "/api/course":
                return self.envoyer(200, json.dumps(details_course(jour, int(q["r"]), int(q["c"]), pari)))
            return self.envoyer(404, json.dumps({"erreur": "introuvable"}))
        except urllib.error.HTTPError as e:
            msg = "Programme pas encore publié par le PMU" if e.code in (204, 404) else f"Le PMU a répondu {e.code}"
            return self.envoyer(502, json.dumps({"erreur": msg}))
        except (urllib.error.URLError, socket.timeout) as e:
            return self.envoyer(502, json.dumps({"erreur": f"PMU injoignable ({getattr(e, 'reason', e)})"}))
        except Exception as e:  # noqa
            return self.envoyer(500, json.dumps({"erreur": f"Erreur : {e}"}))


def ip_locale():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def main():
    global DEMO
    ap = argparse.ArgumentParser(description="Tableau de bord paris hippiques en direct")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--demo", action="store_true", help="courses fictives, sans Internet")
    a = ap.parse_args()
    DEMO = a.demo
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print(f"Multi Live{' (DÉMO)' if DEMO else ''} démarré.")
    print(f"  Sur ce PC        : http://localhost:{a.port}")
    ip = ip_locale()
    if ip:
        print(f"  Sur ton téléphone (même Wi-Fi) : http://{ip}:{a.port}")
    print("Ctrl+C pour arrêter.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêté.")


# ------------------------------------------------------------------ page

PAGE = r"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Multi Live"><meta name="theme-color" content="#1f6b45">
<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon-192.png"><link rel="apple-touch-icon" href="/icon-192.png">
<title>Multi Live</title>
<style>
/* Layout : bandeau course + ticket recommandé en tête, réglages et tableau des partants dessous */
:root{
  --bg:#f3f5f2; --surface:#ffffff; --ink:#18221c; --muted:#5d6b62; --line:#dbe2dc;
  --turf:#1f6b45; --turf-soft:#e2f0e7; --gold:#a6741a; --up:#b4382e; --down:#1f7a4c; --warn:#b06d00;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Consolas,monospace;
  --sans:"Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0f1512; --surface:#18211c; --ink:#e7eee9; --muted:#9aa8a0; --line:#2a3630;
  --turf:#5cc28b; --turf-soft:#1d3428; --gold:#e0b25a; --up:#ef7d72; --down:#6fd39c; --warn:#f0b34a; color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 var(--sans);font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
h1{font-size:18px;margin:0;letter-spacing:.02em}
h1 small{font-weight:400;color:var(--muted);font-size:13px;margin-left:6px}
select,input,textarea,button{font:inherit;color:inherit}
select,textarea,input[type=number]{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
select{max-width:100%}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;background:var(--turf-soft);color:var(--turf);font-size:12px;font-weight:600}
.chip.warn{background:transparent;border:1px solid var(--warn);color:var(--warn)}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.live .dot{animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.live .dot{animation:none}}
.course{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline;margin:14px 0 4px}
.course .nom{font-size:20px;font-weight:700}
.course .meta{color:var(--muted)}
.compte{font-size:28px;font-weight:700;font-family:var(--mono)}
.grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:16px;margin-top:12px}
@media (max-width:860px){.grid{grid-template-columns:minmax(0,1fr)}}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.panel h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px}
.ticket .nums{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 12px}
.ticket .n{width:46px;height:46px;border-radius:8px;display:grid;place-items:center;font-size:20px;font-weight:700;background:var(--turf);color:var(--surface)}
.ticket .n.small{width:34px;height:34px;font-size:15px}
.kpis{display:flex;flex-wrap:wrap;gap:8px 24px}
.kpi b{display:block;font-size:22px}
.kpi span{color:var(--muted);font-size:12px}
.flexi{margin-top:14px;border-top:1px dashed var(--line);padding-top:12px}
.flexi .row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.flexi .row em{font-style:normal;color:var(--muted);font-size:12px;margin-left:6px}
.note{color:var(--muted);font-size:12px;margin:10px 0 0}
.copie{margin-top:10px;border:1px solid var(--turf);background:transparent;color:var(--turf);border-radius:6px;padding:6px 10px;cursor:pointer;font-weight:600}
.copie:focus-visible,button:focus-visible,select:focus-visible{outline:2px solid var(--turf);outline-offset:2px}
.sliders{display:grid;gap:10px}
.sl{display:grid;grid-template-columns:120px minmax(0,1fr) 40px;gap:8px;align-items:center}
.sl input{width:100%;accent-color:var(--turf)}
.sl small{grid-column:1/-1;color:var(--muted);margin-top:-6px;font-size:12px}
details{margin-top:12px}
summary{cursor:pointer;font-weight:600}
textarea{width:100%;min-height:90px;margin-top:6px;font-family:var(--mono);font-size:12px}
.srcs{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.tablewrap{overflow-x:auto;margin-top:16px}
table{width:100%;border-collapse:collapse;min-width:820px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
td.l,th.l{text-align:left}
tr.sel td{background:var(--turf-soft)}
tr.np td{opacity:.45;text-decoration:line-through}
.num{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:6px;border:1px solid var(--line);font-weight:700}
tr.sel .num{background:var(--turf);border-color:var(--turf);color:var(--surface)}
.cheval{font-weight:600}
.sub{color:var(--muted);font-size:12px}
.tr-up{color:var(--up)} .tr-down{color:var(--down)}
.bar{display:inline-block;height:6px;border-radius:3px;background:var(--turf);vertical-align:middle;margin-right:6px}
svg.spark{vertical-align:middle}
.err{margin-top:12px;padding:10px 12px;border:1px solid var(--up);color:var(--up);border-radius:8px}
.res{margin-top:12px;padding:10px 12px;border-radius:8px;border:1px solid var(--gold);color:var(--gold);font-weight:600}
.foot{color:var(--muted);font-size:12px;margin:18px 0 8px}
.ticket .n.small{width:34px;height:34px;font-size:15px;border-radius:7px}
.verdict{margin-top:12px;padding:10px 12px;border-radius:8px;border:1px solid var(--line);display:grid;gap:2px}
.verdict:empty{display:none}
.verdict b{font-size:16px}
.verdict span{font-size:12px;color:var(--muted)}
.verdict.ok{border-color:var(--down);background:color-mix(in srgb,var(--down) 12%,transparent)} .verdict.ok b{color:var(--down)}
.verdict.mid{border-color:var(--warn);background:color-mix(in srgb,var(--warn) 12%,transparent)} .verdict.mid b{color:var(--warn)}
.verdict.ko{border-color:var(--up);background:color-mix(in srgb,var(--up) 10%,transparent)} .verdict.ko b{color:var(--up)}
.bilan{margin-top:16px}
.monpari{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:center;margin-bottom:6px}
.monpari label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.miseBox{display:inline-flex;align-items:center;gap:4px;font-weight:600}
.miseBox input{width:80px;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit;color:inherit}
#miseAide{margin:0 0 10px}
.bcartes{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:10px}
.bcard{border:1px solid var(--line);border-radius:8px;padding:12px;display:grid;gap:4px;min-width:0}
.bcard h3{margin:0;font-size:14px}
.bnet{font-size:24px;font-weight:700}
table.btab{min-width:560px}


[hidden]{display:none!important}
header{align-items:center}
.choix{display:flex;flex-wrap:wrap;gap:10px 12px;margin-top:12px;align-items:flex-end}
.champ{display:grid;gap:4px;min-width:0}
.champ.large{flex:1 1 260px}
.champ label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.champ select{width:100%;padding:8px 8px;font-weight:600}
.champ .miseBox input{padding:8px 8px}
.ticket{margin-top:12px}
.regle{margin:0 0 10px;color:var(--muted)}
.ticket .nums.ord .n{height:56px;align-content:center;line-height:1.1}
.ticket .n i{display:block;font-style:normal;font-size:10px;font-weight:600;opacity:.85}
.reglages{margin-top:16px}
.out{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.out:last-child{border-bottom:0}
.out .num{grid-row:span 2}
.out b{font-weight:600}
.out .dans{color:var(--turf);font-weight:600}
.bcard.actif{border-color:var(--turf);box-shadow:inset 0 0 0 1px var(--turf)}
table.btab{min-width:620px}
.reglages .sliders{margin-top:12px}
.srcNom{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:inherit;font:inherit}
</style></head><body><div class="wrap">

<header>
  <h1>Multi Live <small id="modeDemo"></small></h1>
  <span id="etat" class="chip"><span class="dot"></span><span id="etatTxt">Connexion…</span></span>
</header>

<div class="choix">
  <div class="champ large"><label for="choixCourse">Course</label><select id="choixCourse"></select></div>
  <div class="champ"><label for="pari">Pari</label><select id="pari"></select></div>
  <div class="champ"><label for="style">Style</label>
    <select id="style"><option value="prudent">Prudent</option><option value="equilibre">Équilibré</option><option value="outsiders">Outsiders</option></select></div>
  <div class="champ" id="formuleBox" hidden><label for="formule">Formule</label>
    <select id="formule"><option value="4">en 4</option><option value="5">en 5</option><option value="6" selected>en 6</option><option value="7">en 7</option></select></div>
  <div class="champ"><label for="mise">Ma mise</label>
    <span class="miseBox"><input id="mise" type="number" inputmode="decimal" min="1" max="60" step="0.5" value="5"> €</span></div>
</div>

<div class="course">
  <span class="nom" id="cNom">Chargement du programme…</span>
  <span class="meta" id="cMeta"></span>
  <span class="compte" id="compte"></span>
</div>
<div id="erreur" class="err" hidden></div>
<div id="resultat" class="res" hidden></div>

<section class="panel ticket">
  <h2 id="tTitre">Ticket conseillé</h2>
  <p class="regle" id="tRegle"></p>
  <div class="nums" id="tNums"></div>
  <div class="kpis">
    <div class="kpi"><b id="tProba">–</b><span id="tProbaLab">chance de gagner</span></div>
    <div class="kpi"><b id="tChance">–</b><span id="tChanceLab">soit environ</span></div>
    <div class="kpi"><b id="tRapport">–</b><span id="tRapportLab">gain si le ticket passe</span></div>
  </div>
  <div id="v1" class="verdict"></div>
  <button class="copie" id="copier" type="button">Copier les numéros</button>
  <div class="flexi" id="varBox" hidden>
    <h2 id="fTitre">Variante : ta mise répartie sur 3 tickets</h2>
    <div id="fTickets"></div>
    <p class="note" id="fTotal"></p>
  </div>
  <div class="flexi">
    <h2>Outsiders à surveiller</h2>
    <div id="outListe"></div>
  </div>
  <p class="note" id="tNote"></p>
</section>

<section class="panel bilan">
  <h2 id="bTitre">Bilan des 30 derniers jours</h2>
  <p class="sub" id="bEtat">Chargement…</p>
  <div class="bcartes" id="bCartes"></div>
  <details><summary>Détail jour par jour</summary>
    <div class="tablewrap"><table class="btab"><thead><tr><th class="l">Jour</th><th class="l">Course</th><th class="l">Arrivée</th><th>Prudent</th><th>Équilibré</th><th>Outsiders</th><th>Favoris</th></tr></thead><tbody id="bCorps"></tbody></table></div>
  </details>
</section>

<div class="tablewrap">
<table>
  <thead><tr>
    <th class="l">N°</th><th class="l">Cheval</th><th>Cote matin</th><th>Cote direct</th><th class="l">Évolution</th>
    <th>Forme</th><th>Régularité</th><th>Proba gagner</th><th class="l" id="colTop">Proba dans les 4</th>
  </tr></thead>
  <tbody id="corps"></tbody>
</table>
</div>

<section class="panel reglages">
  <details>
    <summary>Réglages avancés : poids des analyses, cotes d'autres sites</summary>
    <div class="sliders">
      <div class="sl"><label for="wM">Cotes du marché</label><input id="wM" type="range" min="0" max="100" value="55"><span id="vM"></span>
        <small>Ce que pensent les parieurs : la meilleure base, mais elle intègre la marge du PMU.</small></div>
      <div class="sl"><label for="wT">Mouvement des cotes</label><input id="wT" type="range" min="0" max="100" value="15"><span id="vT"></span>
        <small>Cote qui baisse depuis le matin = de l'argent qui rentre sur le cheval.</small></div>
      <div class="sl"><label for="wF">Forme récente</label><input id="wF" type="range" min="0" max="100" value="20"><span id="vF"></span>
        <small>Les 5 dernières places de la musique, la plus récente compte le plus.</small></div>
      <div class="sl"><label for="wR">Régularité</label><input id="wR" type="range" min="0" max="100" value="10"><span id="vR"></span>
        <small>Part des courses finies placé sur toute la carrière.</small></div>
    </div>
    <p class="sub" style="margin-top:14px">Cotes d'un autre site : une ligne par cheval, <code>numéro;cote</code>. Donne un nom au site, puis Ajouter.</p>
    <input id="srcNom" placeholder="Nom du site (ex. Zeturf)" class="srcNom">
    <textarea id="srcTxt" placeholder="3;3,8&#10;1;4,1&#10;7;6,5"></textarea>
    <button class="copie" id="srcAjout" type="button">Ajouter</button>
    <div class="srcs" id="srcListe"></div>
  </details>
</section>

<p class="foot">Estimations à partir des cotes, de la musique et des statistiques du PMU. Elles aident à choisir, elles ne garantissent rien : le PMU prélève une part des mises, donc sur la durée aucune méthode ne gagne à coup sûr. Joue seulement ce que tu acceptes de perdre.</p>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const HEURE_CIBLE = "13:55";
const NB_GROUPES = {4: 1, 5: 5, 6: 15, 7: 35};   // Multi : le rapport en k = rapport en 4 ÷ nombre de groupes de 4 couverts

// kind : seq = ordre exact · set = les k premiers, ordre indifférent · in = dans les m premiers
//        od = ordre ou désordre (deux rapports) · multi = les 4 premiers parmi mes chevaux
const PARIS = {
  SIMPLE_GAGNANT:  {nom: "Simple gagnant", k: 1, kind: "seq", top: 3, regle: "Ton cheval doit gagner la course."},
  SIMPLE_PLACE:    {nom: "Simple placé",   k: 1, kind: "in", m: 3, top: 3, regle: "Ton cheval doit finir dans les 3 premiers (les 2 premiers s'il y a moins de 8 partants)."},
  COUPLE_GAGNANT:  {nom: "Couplé gagnant", k: 2, kind: "set", top: 2, regle: "Tes 2 chevaux doivent être les 2 premiers, dans n'importe quel ordre."},
  COUPLE_PLACE:    {nom: "Couplé placé",   k: 2, kind: "in", m: 3, top: 3, regle: "Tes 2 chevaux doivent finir tous les deux dans les 3 premiers."},
  COUPLE_ORDRE:    {nom: "Couplé ordre",   k: 2, kind: "seq", top: 2, regle: "Tes 2 chevaux doivent finir 1er et 2e, dans cet ordre."},
  DEUX_SUR_QUATRE: {nom: "2 sur 4",        k: 2, kind: "in", m: 4, top: 4, regle: "Tes 2 chevaux doivent finir tous les deux dans les 4 premiers."},
  TRIO:            {nom: "Trio",           k: 3, kind: "set", top: 3, regle: "Tes 3 chevaux doivent être les 3 premiers, dans n'importe quel ordre."},
  TRIO_ORDRE:      {nom: "Trio ordre",     k: 3, kind: "seq", top: 3, regle: "Tes 3 chevaux doivent être les 3 premiers, dans cet ordre."},
  TIERCE:          {nom: "Tiercé",         k: 3, kind: "od", top: 3, regle: "Tes 3 chevaux doivent être les 3 premiers. Dans l'ordre exact, le gain est bien plus gros."},
  QUARTE_PLUS:     {nom: "Quarté+",        k: 4, kind: "od", top: 4, regle: "Tes 4 chevaux doivent être les 4 premiers. Dans l'ordre exact, le gain est bien plus gros. Un petit bonus est payé si tu as les 3 premiers."},
  QUINTE_PLUS:     {nom: "Quinté+",        k: 5, kind: "od", top: 5, regle: "Tes 5 chevaux doivent être les 5 premiers. Dans l'ordre exact, le gain est bien plus gros. De petits bonus sont payés si tu as 3 ou 4 des premiers."},
  MULTI:           {nom: "Multi",          k: 6, kind: "multi", top: 4, regle: "Les 4 premiers de la course doivent tous être parmi tes chevaux, dans n'importe quel ordre."},
  SUPER_QUATRE:    {nom: "Super 4",        k: 4, kind: "seq", top: 4, regle: "Tes 4 chevaux doivent être les 4 premiers, dans cet ordre."},
  PICK5:           {nom: "Pick 5",         k: 5, kind: "set", top: 5, regle: "Tes 5 chevaux doivent être les 5 premiers, dans n'importe quel ordre."},
};
const ORDONNES = new Set(["COUPLE_ORDRE", "TRIO_ORDRE", "SUPER_QUATRE"]);
// Style de ticket : combien d'outsiders l'appli fait entrer dans le ticket
const STYLES = {
  prudent:   {nom: "Prudent",   txt: "le ticket qui a le plus de chances de passer, donc proche des favoris."},
  equilibre: {nom: "Équilibré", txt: "une base de favoris, plus le ou les outsiders les mieux notés par l'appli."},
  outsiders: {nom: "Outsiders", txt: "la moitié du ticket en outsiders bien notés. Passe moins souvent, paie plus."},
};
let style = "prudent";

let courses = [], courant = null, donnees = null, timer = null, sources = {}, pari = "MULTI", prefPari = "MULTI";
try { sources = JSON.parse(localStorage.getItem("ml_sources") || "{}"); } catch (e) {}
try {
  for (const k of ["wM", "wT", "wF", "wR"]) { const v = localStorage.getItem("ml_" + k); if (v !== null) $(k).value = v; }
  const f = localStorage.getItem("ml_formule"); if (f) $("formule").value = f;
  const mi = localStorage.getItem("ml_mise"); if (mi) $("mise").value = mi;
  const pp = localStorage.getItem("ml_pari"); if (pp && PARIS[pp]) prefPari = pp;
  const sy = localStorage.getItem("ml_style"); if (sy && STYLES[sy]) { style = sy; $("style").value = sy; }
} catch (e) {}
pari = prefPari;

const pct = x => (100 * x).toFixed(x < 0.01 ? 2 : 1).replace(".", ",") + " %";
const euro = x => (x >= 1000 ? Math.round(x).toLocaleString("fr-FR") : x.toFixed(2).replace(".", ",")) + " €";
const hhmm = ms => new Date(ms).toLocaleTimeString("fr-FR", {hour: "2-digit", minute: "2-digit"});
const signe = x => (x >= 0 ? "+" : "−") + euro(Math.abs(x));
const sur = p => p > 0 ? "1 chance sur " + Math.max(1, Math.round(1 / p)).toLocaleString("fr-FR") : "–";
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])); }
function formule() { return +$("formule").value; }
function maMise() { let v = parseFloat(String($("mise").value).replace(",", ".")); if (!isFinite(v)) v = 3; return Math.min(60, Math.max(1, v)); }
const poids = () => ({M: +$("wM").value, T: +$("wT").value, F: +$("wF").value, R: +$("wR").value});
const offre = (c, t) => (c.paris || []).some(p => p.t === t);
const tailleTicket = t => t === "MULTI" ? formule() : PARIS[t].k;
const nomPari = (t, c) => t === "MULTI" && c && c.mini ? "Mini Multi" : PARIS[t].nom;

// ---------- programme
async function chargerProgramme() {
  const r = await fetch("/api/courses"); const j = await r.json();
  if (!r.ok) throw new Error(j.erreur || "Programme indisponible");
  $("modeDemo").textContent = j.demo ? "— démo, courses fictives" : "";
  courses = j.courses.filter(c => (c.paris || []).length);
  remplirCourses();
  // course par défaut : celle qui propose mon pari habituel, la plus proche de 13h55
  const [h, m] = HEURE_CIBLE.split(":").map(Number);
  const cible = new Date(); cible.setHours(h, m, 0, 0);
  const avec = courses.filter(c => offre(c, prefPari));
  const pool = avec.length ? avec : courses;
  let choix = pool[0];
  for (const c of pool) if (Math.abs((c.heure || 0) - cible) < Math.abs((choix.heure || 0) - cible)) choix = c;
  if (choix) choisir(choix);
}
function remplirCourses() {
  const sel = $("choixCourse"); sel.innerHTML = "";
  for (const c of courses) {
    const o = document.createElement("option");
    o.value = c.r + "-" + c.c;
    o.textContent = `${c.heure ? hhmm(c.heure) : "--:--"}  R${c.r}C${c.c} ${c.hippodrome}${offre(c, prefPari) ? "  · " + nomPari(prefPari, c) : ""}`;
    sel.appendChild(o);
  }
  if (courant) sel.value = courant.r + "-" + courant.c;
}
$("choixCourse").addEventListener("change", e => {
  const [r, c] = e.target.value.split("-").map(Number);
  choisir(courses.find(x => x.r === r && x.c === c));
});

function choisir(c) {
  courant = c; donnees = null;
  $("choixCourse").value = c.r + "-" + c.c;
  // paris proposés sur cette course
  const sel = $("pari"); sel.innerHTML = "";
  for (const p of c.paris) {
    const o = document.createElement("option"); o.value = p.t; o.textContent = nomPari(p.t, c); sel.appendChild(o);
  }
  pari = offre(c, prefPari) ? prefPari : offre(c, pari) ? pari : offre(c, "MULTI") ? "MULTI" : c.paris[0].t;
  sel.value = pari;
  $("cNom").textContent = `R${c.r}C${c.c} — ${c.libelle || c.hippodrome}`;
  const bits = [c.hippodrome, c.discipline && c.discipline.toLowerCase(), c.distance && c.distance + " m",
                c.partants && c.partants + " partants"].filter(Boolean);
  $("cMeta").textContent = (c.heure ? "Départ " + hhmm(c.heure) + " · " : "") + bits.join(" · ");
  majFormule(); rafraichir(); chargerBilan();
}
function majFormule() {
  $("formuleBox").hidden = pari !== "MULTI";
  const o7 = $("formule").querySelector('option[value="7"]');
  o7.disabled = !!(courant && courant.mini);
  if (o7.disabled && $("formule").value === "7") $("formule").value = "6";
}
$("pari").addEventListener("change", e => {
  pari = prefPari = e.target.value;
  try { localStorage.setItem("ml_pari", pari); } catch (_) {}
  remplirCourses(); majFormule(); rafraichir(); chargerBilan();
});

// ---------- boucle temps réel
async function rafraichir() {
  clearTimeout(timer);
  if (!courant) return;
  const demande = courant.r + "-" + courant.c + "-" + pari;
  try {
    const r = await fetch(`/api/course?r=${courant.r}&c=${courant.c}&pari=${pari}`); const j = await r.json();
    if (!r.ok) throw new Error(j.erreur || "Données indisponibles");
    if (demande !== courant.r + "-" + courant.c + "-" + pari) return;   // l'utilisateur a changé entre-temps
    donnees = j; $("erreur").hidden = true;
    etat(true, "Cotes en direct · " + new Date(j.maj).toLocaleTimeString("fr-FR"));
    calculer();
  } catch (e) {
    $("erreur").hidden = false; $("erreur").textContent = e.message + ". Nouvel essai dans 20 s.";
    etat(false, "Hors ligne");
  }
  const reste = (courant.heure || 0) - Date.now();
  timer = setTimeout(rafraichir, reste > 0 && reste < 5 * 60000 ? 10000 : 20000);
}
// Sur téléphone, le navigateur met la page en pause en arrière-plan : on relance dès qu'elle revient
document.addEventListener("visibilitychange", () => { if (!document.hidden) rafraichir(); });
function etat(ok, txt) { $("etat").className = "chip " + (ok ? "live" : "warn"); $("etatTxt").textContent = txt; }

setInterval(() => {
  if (!courant || !courant.heure) { $("compte").textContent = ""; return; }
  const d = courant.heure - Date.now();
  if (d <= 0) { $("compte").textContent = "Partie"; return; }
  const h = Math.floor(d / 3600000), m = Math.floor(d / 60000) % 60, s = Math.floor(d / 1000) % 60;
  $("compte").textContent = (h ? h + " h " : "") + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}, 1000);

// ---------- probabilités (modèle de Harville : on retire chaque cheval arrivé et on renormalise)
function normaliser(vals) { const s = vals.reduce((a, b) => a + b, 0) || 1; return vals.map(v => v / s); }
function seqP(pv, seq) {            // ces chevaux arrivent premiers, dans cet ordre exact
  let t = 1, reste = 1;
  for (const i of seq) { if (reste <= 1e-12) return 0; t *= pv[i] / reste; reste -= pv[i]; }
  return t;
}
function permutations(a) {
  if (a.length <= 1) return [a.slice()];
  const out = [];
  a.forEach((x, i) => { for (const p of permutations(a.filter((_, j) => j !== i))) out.push([x].concat(p)); });
  return out;
}
function setP(pv, S) { let t = 0; for (const q of permutations(S)) t += seqP(pv, q); return t; }   // les |S| premiers, ordre indifférent
function inTopP(pv, S, m) {         // tous les chevaux de S finissent dans les m premiers
  const n = pv.length, need = new Set(S), used = new Array(n).fill(false); let tot = 0;
  (function rec(depth, prob, reste, found) {
    if (found === S.length) { tot += prob; return; }
    if (depth === m || S.length - found > m - depth || reste <= 1e-12) return;
    for (let i = 0; i < n; i++) if (!used[i]) {
      used[i] = true; rec(depth + 1, prob * pv[i] / reste, reste - pv[i], found + (need.has(i) ? 1 : 0)); used[i] = false; }
  })(0, 1, 1, 0);
  return tot;
}
function topDist(pv, m) {           // pour chaque cheval, proba de finir dans les m premiers
  const n = pv.length, out = new Array(n).fill(0), used = new Array(n).fill(false);
  (function rec(depth, prob, reste) {
    if (depth === m || reste <= 1e-12) return;
    for (let i = 0; i < n; i++) if (!used[i]) {
      const q = prob * pv[i] / reste; out[i] += q;
      used[i] = true; rec(depth + 1, q, reste - pv[i]); used[i] = false; }
  })(0, 1, 1);
  return out;
}
function combinaisons(arr, k) {
  const out = [];
  (function rec(s, cur) { if (cur.length === k) { out.push(new Set(cur)); return; }
    for (let i = s; i < arr.length; i++) { cur.push(arr[i]); rec(i + 1, cur); cur.pop(); } })(0, []);
  return out;
}
const placesPlace = (t, n) => t === "SIMPLE_PLACE" && n < 8 ? 2 : PARIS[t].m;
// Proba de gagner pour un ticket T (indices, dans l'ordre joué) : {win, ordre}
function probasTicket(t, pv, T) {
  const kind = PARIS[t].kind;
  if (kind === "seq") return {win: seqP(pv, T)};
  if (kind === "set") return {win: setP(pv, T)};
  if (kind === "in") return {win: inTopP(pv, T, placesPlace(t, pv.length))};
  return {win: setP(pv, T), ordre: seqP(pv, T)};      // od
}

// ---------- analyses
function scoreMusique(mus) {
  const m = (mus || "").replace(/\(\d+\)/g, "");
  const places = [...m.matchAll(/([0-9DATRN])[a-z]/gi)].map(x => x[1].toUpperCase()).slice(0, 5);
  if (!places.length) return null;
  const bar = {"1": 1, "2": .8, "3": .65, "4": .5, "5": .4, "6": .25, "7": .2, "8": .15, "9": .1, "0": .05};
  const w = [1, .85, .7, .55, .4];
  let t = 0, s = 0; places.forEach((p, i) => { t += (bar[p] || 0) * w[i]; s += w[i]; });
  return t / s;
}
function marchePMU(P, srcs) {
  const parSource = [P.map(c => c.coteDirect || c.coteMatin || null)];
  for (const cotes of Object.values(srcs || {})) parSource.push(P.map(c => cotes[c.num] || null));
  const pm = P.map(() => []);
  for (const src of parSource) {
    const inv = src.map(v => v && v > 1 ? 1 / v : 0); const s = inv.reduce((a, b) => a + b, 0);
    if (!s) continue; inv.forEach((v, i) => { if (v) pm[i].push(v / s); });
  }
  const m = pm.map(a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
  const connus = m.filter(x => x !== null);
  const plancher = (connus.length ? Math.min(...connus) : 1 / P.length) / 2;
  return normaliser(m.map(x => x === null ? plancher : x));
}
function modele(P, w, srcs) {       // probabilité de gagner de chaque cheval, en croisant les 4 analyses
  const marche = marchePMU(P, srcs);
  const mouv = normaliser(P.map((c, i) => {
    const f = c.coteMatin && c.coteDirect ? Math.min(2, Math.max(.5, c.coteMatin / c.coteDirect)) : 1;
    return marche[i] * f; }));
  const fr = P.map(c => scoreMusique(c.musique));
  const ok = fr.filter(x => x !== null);
  const moyF = ok.length ? ok.reduce((a, b) => a + b, 0) / ok.length : .3;
  const forme = normaliser(fr.map(x => Math.pow(x === null ? moyF : x, 2)));
  const regul = normaliser(P.map(c => { const r = (c.places + .9) / ((c.courses || 0) + 3); return r * r; }));
  const W = (w.M + w.T + w.F + w.R) || 1;
  const p = normaliser(P.map((_, i) => (w.M * marche[i] + w.T * mouv[i] + w.F * forme[i] + w.R * regul[i]) / W));
  return {p, marche, fr};
}
// Choix du ticket selon le style. Un « outsider » est un cheval hors des favoris des parieurs ;
// on prend ceux que l'appli note le mieux par rapport à leur cote (proba × avantage sur le marché).
function choisirTicket(mod, k, sty) {
  const p = mod.p, m = mod.marche, n = p.length, idx = p.map((_, i) => i);
  const parP = idx.slice().sort((a, b) => p[b] - p[a]);
  if (sty === "prudent") return parP.slice(0, k);
  const nOut = Math.min(k, sty === "equilibre" ? Math.ceil(k / 4) : Math.ceil(k / 2));
  const r0 = k === 1 ? (sty === "equilibre" ? 1 : 3) : k;       // rang chez les parieurs au-delà duquel on parle d'outsider
  const rangMarche = idx.slice().sort((a, b) => m[b] - m[a]);
  const coeur = parP.slice(0, k - nOut);
  const valeur = i => p[i] * Math.pow(p[i] / Math.max(m[i], 1e-6), 1.5);
  const pool = rangMarche.slice(Math.min(r0, n - 1)).filter(i => !coeur.includes(i)).sort((a, b) => valeur(b) - valeur(a));
  const T = coeur.concat(pool.slice(0, nOut));
  for (const i of parP) { if (T.length >= k) break; if (!T.includes(i)) T.push(i); }
  return T.sort((a, b) => p[b] - p[a]);          // du plus probable au moins probable (utile pour les paris dans l'ordre)
}
function quartets(T) { return combinaisons(T, 4).map(s => [...s]); }
function couvertureMulti(p, T) { let t = 0; for (const q of quartets(T)) t += setP(p, q); return t; }
// Variante Multi : le ticket principal, plus 2 tickets qui couvrent le mieux ce qu'il laisse de côté
function troisTickets(p, k, T1) {
  const n = p.length, idx = p.map((_, i) => i);
  const cand = [...new Set(idx.slice().sort((x, y) => p[y] - p[x]).slice(0, Math.min(11, n)).concat(T1))];
  const Q = combinaisons(cand, 4).map(e => { const q = [...e]; return {q, key: q.slice().sort((a, b) => a - b).join(","), p: setP(p, q)}; });
  const ensembles = combinaisons(cand, Math.min(k, n)), couvre = (e, q) => q.every(i => e.has(i));
  const couverts = new Set(), exclus = new Set(), out = [];
  const ajouter = e => { let g = 0; for (const x of Q) if (!couverts.has(x.key) && couvre(e, x.q)) { g += x.p; couverts.add(x.key); }
    exclus.add([...e].sort((a, b) => a - b).join(",")); out.push({set: e, gain: g}); };
  ajouter(new Set(T1));
  for (let t = 0; t < 2; t++) {
    let best = null, g = -1;
    for (const e of ensembles) { if (exclus.has([...e].sort((a, b) => a - b).join(","))) continue;
      let v = 0; for (const x of Q) if (!couverts.has(x.key) && couvre(e, x.q)) v += x.p;
      if (v > g) { g = v; best = e; } }
    if (!best) break; ajouter(best);
  }
  return out;
}

// ---------- rapports : vrais rapports probables du PMU, sinon estimation calée sur les jours passés
// En pari mutuel, rapport ≈ K ÷ (proba que les parieurs donnent à cette arrivée). K se mesure sur les vrais
// rapports définitifs des 30 derniers jours : rapport réel × proba du marché, valeur médiane.
const Kcal = {};                    // pari -> {a: K principal, o: K « ordre »}
function mediane(a) { if (a.length < 5) return null; a.sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; }
const estDesordre = l => /d[ée]sordre/i.test(l);
const estOrdre = (t, l) => !estDesordre(l) && (/ordre/i.test(l) || ORDONNES.has(t));
function calerK(t) {
  const A = [], O = [], kind = PARIS[t].kind;
  for (const j of bilanCache[t] || []) {
    const P = j.partants.filter(c => c.partant), mk = marchePMU(P, {});
    for (const rap of j.rapports[t] || []) {
      const lib = rap.l.toLowerCase(); if (lib.includes("bonus")) continue;
      const idx = rap.c.map(nm => P.findIndex(c => c.num === nm)); if (idx.some(x => x < 0)) continue;
      if (kind === "multi") { if (/en 4$/.test(lib)) A.push(rap.d * setP(mk, idx)); }
      else if (kind === "od") { if (estDesordre(lib)) A.push(rap.d * setP(mk, idx)); else if (estOrdre(t, lib)) O.push(rap.d * seqP(mk, idx)); }
      else if (kind === "seq") A.push(rap.d * seqP(mk, idx));
      else if (kind === "set") A.push(rap.d * setP(mk, idx));
      else A.push(rap.d * inTopP(mk, idx, placesPlace(t, P.length)));
    }
  }
  Kcal[t] = {a: mediane(A), o: mediane(O)};
}
let probMap = {};
function chargerProbables() {
  probMap = {};
  for (const [nums, d, mn, mx] of (donnees && donnees.probables) || []) {
    const key = (ORDONNES.has(pari) ? nums : nums.slice().sort((a, b) => a - b)).join("-");
    probMap[key] = {d, mn, mx};
  }
}
// Évalue un ticket : proba de gagner, rapport pour 1 €, part de la mise rendue en moyenne
function evaluer(t, P, mod, T) {
  const pr = probasTicket(t, mod.p, T), pm = probasTicket(t, mod.marche, T), K = Kcal[t] || {};
  const nums = T.map(i => P[i].num);
  const res = {win: pr.win, ordre: pr.ordre, ratio: null, source: null};
  if (PARIS[t].kind === "od") {
    if (K.a && pm.win > 0) res.rDes = K.a / pm.win;
    if (K.o && pm.ordre > 0) res.rOrd = K.o / pm.ordre;
    if (res.rDes && res.rOrd) { res.ratio = pr.ordre * res.rOrd + (pr.win - pr.ordre) * res.rDes; res.source = "estime"; }
    return res;
  }
  let reel = null;
  if (t === "SIMPLE_GAGNANT") { const c = P[T[0]].coteDirect; if (c) reel = {d: c}; }
  else reel = probMap[(ORDONNES.has(t) ? nums : nums.slice().sort((a, b) => a - b)).join("-")] || null;
  if (reel && (reel.d || (reel.mn && reel.mx))) {
    res.r = reel.d || (reel.mn + reel.mx) / 2; res.rMin = reel.d ? null : reel.mn; res.rMax = reel.d ? null : reel.mx; res.source = "pmu";
  } else if (K.a && pm.win > 0) { res.r = K.a / pm.win; res.source = "estime"; }
  if (res.r) res.ratio = pr.win * res.r;
  return res;
}
function esperanceMulti(mod, T, k) {     // pour 1 € misé sur ce ticket (T = indices des chevaux joués)
  const K = (Kcal.MULTI || {}).a; if (!K) return null;
  let ret = 0, pw = 0, mini = Infinity, maxi = 0;
  for (const q of quartets(T)) {
    const pq = setP(mod.p, q), r = K / (NB_GROUPES[k] * Math.max(setP(mod.marche, q), 1e-9)); ret += pq * r; pw += pq;
    if (pq > 0.001) { mini = Math.min(mini, r); maxi = Math.max(maxi, r); } }
  return {ratio: ret, siGagne: pw ? ret / pw : 0, mini, maxi};
}
// Gain réel (pour 1 €) d'un ticket d'après les rapports définitifs du PMU ; 0 si perdant
function gainReel(t, k, nums, raps) {
  let best = 0; const set = new Set(nums);
  for (const rap of raps || []) {
    const lib = rap.l.toLowerCase();
    if (t === "MULTI") { if (!new RegExp("en " + k + "$").test(lib) || !rap.c.every(x => set.has(x))) continue; }
    else if (estOrdre(t, lib)) { if (rap.c.length !== nums.length || !rap.c.every((x, i) => x === nums[i])) continue; }
    else if (!rap.c.every(x => set.has(x))) continue;
    best = Math.max(best, rap.d);
  }
  return best;
}

// ---------- affichage de la course en direct
function calculer() {
  if (!donnees || !courant) return;
  const tous = donnees.partants, P = tous.filter(c => c.partant), n = P.length;
  const t = pari, info = PARIS[t], k = tailleTicket(t), mise = maMise();
  const base = ((courant.paris || []).find(p => p.t === t) || {}).base;
  const w = poids(); for (const kk in w) $("v" + kk).textContent = w[kk];
  $("tTitre").textContent = `Ticket conseillé — ${nomPari(t, courant)}${t === "MULTI" ? " en " + k : ""}, mise ${euro(mise)}`;
  $("tRegle").textContent = "Pour gagner : " + info.regle + " Style " + STYLES[style].nom + " : " + STYLES[style].txt;
  if (n < k + 1) { $("tNums").textContent = "Pas assez de partants pour ce pari."; return; }
  chargerProbables();
  const mod = modele(P, w, sources), p = mod.p;
  const ordonne = info.kind === "seq" && k > 1 || info.kind === "od";
  let T, selSet, res, gainTxt = "–", gainLab = "gain si le ticket passe";

  if (t === "MULTI") {
    T = choisirTicket(mod, k, style); selSet = new Set(T);
    const e = esperanceMulti(mod, T, k);
    res = {win: couvertureMulti(p, T), ratio: e ? e.ratio : null, source: e ? "estime" : null};
    if (e) { gainTxt = "≈ " + euro(e.siGagne * mise); gainLab = `gain estimé si le ticket passe (de ${euro(e.mini * mise)} à ${euro(e.maxi * mise)} selon l'arrivée)`; }
    // variante : la mise répartie sur 3 tickets qui se complètent
    const miseT = Math.floor(mise / 3 * 100) / 100; let total = 0, retF = 0, html = "";
    $("varBox").hidden = false;
    $("fTitre").textContent = miseT >= 1.5 ? `Variante : 3 tickets à ${euro(miseT)} = ${euro(3 * miseT)}` : "Variante 3 tickets : il faut au moins 4,50 € (1,50 € par ticket)";
    for (const x of (miseT >= 1.5 ? troisTickets(p, k, T) : [])) {
      total += x.gain; const ex = esperanceMulti(mod, [...x.set], k); if (ex) retF += ex.ratio * miseT;
      const o = [...x.set].sort((a, b) => p[b] - p[a]);
      html += `<div class="row">${o.map(i => `<span class="n small">${P[i].num}</span>`).join("")}<em>+${pct(x.gain)}</em></div>`;
    }
    $("fTickets").innerHTML = html;
    $("fTotal").textContent = total ? `Ensemble : ${pct(total)} de chances d'avoir au moins un ticket gagnant (${sur(total)}).` + (retF ? ` Gain moyen attendu : ${signe(retF - 3 * miseT)} pour ${euro(3 * miseT)}.` : "") : "";
  } else {
    $("varBox").hidden = true;
    T = choisirTicket(mod, k, style); selSet = new Set(T);
    res = evaluer(t, P, mod, T);
    if (info.kind === "od") {
      if (res.rDes && res.rOrd) { gainTxt = "≈ " + euro(res.rDes * mise); gainLab = `gain estimé dans le désordre · ≈ ${euro(res.rOrd * mise)} dans l'ordre`; }
    } else if (res.r) {
      gainTxt = (res.rMin ? "" : "≈ ") + (res.rMin ? `${euro(res.rMin * mise)} à ${euro(res.rMax * mise)}` : euro(res.r * mise));
      gainLab = res.source === "pmu" ? "gain si le ticket passe (rapport probable PMU)" : "gain estimé si le ticket passe";
    }
  }
  $("tNums").innerHTML = T.map((i, r) => `<span class="n">${P[i].num}${ordonne ? `<i>${r + 1}${r ? "e" : "er"}</i>` : ""}</span>`).join("");
  $("tNums").className = "nums" + (ordonne ? " ord" : "");
  $("tProba").textContent = pct(res.win);
  $("tProbaLab").textContent = info.kind === "od" ? `chance de gagner (dont ${pct(res.ordre)} dans l'ordre exact)` : "chance de gagner";
  $("tChance").textContent = sur(res.win);
  $("tRapport").textContent = gainTxt; $("tRapportLab").textContent = gainLab;
  verdict(res, mise);
  $("copier").dataset.txt = T.map(i => P[i].num).join(" - ");
  $("tNote").textContent = (base ? `Mise de base PMU pour ce pari : ${euro(base)}. ` : "") +
    (info.kind === "od" ? "Les bonus ne sont pas comptés dans le verdict. " : "") +
    "Le verdict ne vaut que si le bilan ci-dessous montre que l'appli fait mieux que les favoris.";

  // Outsiders à surveiller : hors des favoris des parieurs, mais mieux notés par l'appli ou cote en nette baisse
  const rang = p.map((_, i) => i).sort((x, y) => mod.marche[y] - mod.marche[x]);
  const seuil = Math.max(k, 4), outs = [];
  rang.slice(seuil).forEach(i => {
    const c = P[i], val = p[i] / Math.max(mod.marche[i], 1e-6), raisons = [];
    const baisse = c.coteMatin && c.coteDirect ? 1 - c.coteDirect / c.coteMatin : 0;
    if (mod.fr[i] !== null && mod.fr[i] >= 0.55) raisons.push(`bonne forme récente (${Math.round(mod.fr[i] * 100)}/100)`);
    if (c.courses >= 5 && c.places / c.courses >= 0.45) raisons.push(`régulier (${Math.round(100 * c.places / c.courses)} % de places)`);
    if (baisse >= 0.12) raisons.push(`cote en baisse de ${Math.round(baisse * 100)} %`);
    if (raisons.length && (val >= 1.1 || baisse >= 0.12)) outs.push({i, val, raisons});
  });
  outs.sort((a, b) => b.val - a.val);
  $("outListe").innerHTML = outs.length ? outs.slice(0, 4).map(o => { const c = P[o.i];
    return `<div class="out"><span class="num">${c.num}</span><div><b>${esc(c.nom)}</b> · cote ${c.coteDirect ? c.coteDirect.toFixed(1).replace(".", ",") : "–"}${selSet.has(o.i) ? ' · <span class="dans">dans le ticket</span>' : ""}</div>
      <div class="sub">${o.raisons.join(" · ")} · ${pct(p[o.i])} de chances de gagner</div></div>`; }).join("")
    : '<p class="sub">Aucun outsider ne se détache sur cette course pour le moment.</p>';

  // Tableau des partants
  const topN = t === "SIMPLE_PLACE" && n < 8 ? 2 : info.top;
  const top = topDist(p, Math.min(topN, n - 1)), maxT = Math.max(...top);
  $("colTop").textContent = `Proba dans les ${topN}`;
  const lignes = p.map((_, i) => i).sort((x, y) => p[y] - p[x]).map(i => {
    const c = P[i]; let evo = "", cls = "";
    if (c.coteMatin && c.coteDirect) { const d = (c.coteDirect - c.coteMatin) / c.coteMatin;
      cls = d < -0.03 ? "tr-down" : d > 0.03 ? "tr-up" : ""; evo = (d < 0 ? "▼ " : d > 0 ? "▲ " : "") + Math.abs(d * 100).toFixed(0) + " %"; }
    return `<tr class="${selSet.has(i) ? "sel" : ""}">
      <td class="l"><span class="num">${c.num}</span></td>
      <td class="l"><div class="cheval">${esc(c.nom)}</div><div class="sub">${esc(c.driver || "")} · ${esc(c.musique || "")}</div></td>
      <td>${c.coteMatin ? c.coteMatin.toFixed(1).replace(".", ",") : "–"}</td>
      <td><b>${c.coteDirect ? c.coteDirect.toFixed(1).replace(".", ",") : "–"}</b></td>
      <td class="l ${cls}">${spark(c.histo)} ${evo}</td>
      <td>${mod.fr[i] === null ? "–" : Math.round(mod.fr[i] * 100)}</td>
      <td>${c.courses ? Math.round(100 * c.places / c.courses) + " %" : "–"}</td>
      <td>${pct(p[i])}</td>
      <td class="l"><span class="bar" style="width:${Math.round(70 * top[i] / maxT)}px"></span>${pct(top[i])}</td></tr>`;
  });
  const np = tous.filter(c => !c.partant).map(c => `<tr class="np"><td class="l"><span class="num">${c.num}</span></td><td class="l" colspan="8">${esc(c.nom)} — non partant</td></tr>`);
  $("corps").innerHTML = lignes.join("") + np.join("");

  // Arrivée
  const arr = donnees.arrivee || [], raps = (donnees.rapports || {})[t];
  if (arr.length >= 3) {
    const g = raps ? gainReel(t, k, T.map(i => P[i].num), raps) : null;
    $("resultat").hidden = false;
    $("resultat").textContent = `Arrivée : ${arr.join(" - ")}. ` + (g === null ? "Rapports de ce pari pas encore publiés."
      : g > 0 ? `Le ticket conseillé était GAGNANT : ${euro(g * mise)} pour ${euro(mise)} misés.` : "Le ticket conseillé était perdant.");
  } else $("resultat").hidden = true;
}

function verdict(e, mise) {
  const el = $("v1");
  if (e.ratio === null || !isFinite(e.ratio)) {
    el.className = "verdict"; el.innerHTML = `<b>Verdict en attente</b><span>Le rapport de ce pari n'est pas encore connu : il sera estimé dès que le bilan des jours passés sera chargé.</span>`; return; }
  let cls, titre;
  if (e.ratio >= 1.0) { cls = "ok"; titre = "Pari favorable"; }
  else if (e.ratio >= 0.85) { cls = "mid"; titre = "Pari limite"; }
  else { cls = "ko"; titre = "Pari défavorable : passe ton tour"; }
  el.className = "verdict " + cls;
  el.innerHTML = `<b>${titre}</b><span>Gain moyen attendu : ${signe((e.ratio - 1) * mise)} pour ${euro(mise)} misés (${Math.round(e.ratio * 100)} % de la mise rendue en moyenne) · ${e.source === "pmu" ? "d'après le rapport probable du PMU" : "rapport estimé d'après les 30 derniers jours"}</span>`;
}

// ---------- bilan des jours passés
const bilanCache = {}; let bilanTimer = null;
async function chargerBilan() {
  const t = pari;
  $("bTitre").textContent = `Bilan des 30 derniers jours — ${PARIS[t].nom}`;
  if (bilanCache[t]) { calculerBilan(); calculer(); return; }
  $("bCartes").innerHTML = ""; $("bCorps").innerHTML = "";
  $("bEtat").textContent = "Calcul du bilan sur les 30 derniers jours… (jusqu'à 30 secondes la première fois)";
  try {
    const r = await fetch(`/api/bilan?jours=30&heure=${HEURE_CIBLE}&pari=${t}`); const j = await r.json();
    if (!r.ok) throw new Error(j.erreur || "Bilan indisponible");
    bilanCache[t] = j.courses; calerK(t);
    if (t === pari) { calculerBilan(); calculer(); }
  } catch (e) { if (t === pari) $("bEtat").textContent = "Bilan indisponible : " + e.message + ". Il sera recalculé à la prochaine ouverture."; }
}
function calculerBilan() {
  const t = pari, data = bilanCache[t]; if (!data) return;
  const w = poids(), k = tailleTicket(t), mise = maMise(), multi = t === "MULTI";
  const cles = ["prudent", "equilibre", "outsiders", "fav"], st = {};
  for (const c of cles) st[c] = {n: 0, g: 0, mise: 0, ret: 0};
  const lignes = [];
  for (const j of data) {
    const P = j.partants.filter(c => c.partant), raps = j.rapports[t];
    if (P.length < k + 1 || !raps || !raps.length) continue;
    if (multi && !raps.some(r => new RegExp("en " + k + "$").test(r.l.toLowerCase()))) continue;
    const mod = modele(P, w, {}), nums = idx => idx.map(i => P[i].num), gains = {};
    for (const c of ["prudent", "equilibre", "outsiders"]) gains[c] = gainReel(t, k, nums(choisirTicket(mod, k, c)), raps) * mise;
    const fav = P.map((_, i) => i).sort((x, y) => (P[x].coteDirect || 999) - (P[y].coteDirect || 999)).slice(0, k);
    gains.fav = gainReel(t, k, nums(fav), raps) * mise;
    for (const c of cles) { st[c].n++; st[c].mise += mise; if (gains[c]) { st[c].g++; st[c].ret += gains[c]; } }
    const d = j.date, cell = g => `<td class="${g ? "tr-down" : ""}">${g ? "✓ +" + euro(g) : "✗"}</td>`;
    lignes.push(`<tr><td class="l">${d.slice(0, 2)}/${d.slice(2, 4)}</td><td class="l">${esc(j.hippodrome)} R${j.r}C${j.c}</td>
      <td class="l">${j.arrivee.join("-")}</td>${cles.map(c => cell(gains[c])).join("")}</tr>`);
  }
  const carte = (titre, s, sous, actif) => `<div class="bcard${actif ? " actif" : ""}"><h3>${titre}</h3><div class="sub">${sous}</div>
      <div class="bnet ${s.ret - s.mise >= 0 ? "tr-down" : "tr-up"}">${signe(s.ret - s.mise)}</div>
      <div class="sub">${s.g} ticket${s.g > 1 ? "s" : ""} payé${s.g > 1 ? "s" : ""} sur ${s.n} · misé ${euro(s.mise)} · récupéré ${euro(s.ret)}</div></div>`;
  $("bCartes").innerHTML = st.fav.n ? ["prudent", "equilibre", "outsiders"].map(c =>
      carte("Style " + STYLES[c].nom, st[c], c === style ? "ton style actuel" : "ticket de l'appli", c === style)).join("") +
    carte(k > 1 ? `Les ${k} favoris` : "Le favori", st.fav, k > 1 ? "les plus petites cotes, sans analyse" : "la plus petite cote, sans analyse") : "";
  $("bCorps").innerHTML = lignes.join("");
  $("bEtat").textContent = st.fav.n
    ? `${st.fav.n} courses analysées, ${PARIS[t].nom}${multi ? " en " + k : ""} à ${euro(mise)} par jour : chaque jour, la course proposant ce pari la plus proche de ${HEURE_CIBLE.replace(":", "h")}. Gains calculés avec les vrais rapports du PMU. Calcul fait avec les cotes finales : en vrai, quelques minutes avant le départ, c'est un peu moins bon.`
    : "Aucune course exploitable pour ce pari sur la période.";
}

function spark(h) {
  if (!h || h.length < 2) return '<svg class="spark" width="60" height="18"></svg>';
  const v = h.map(x => x[1]); const mn = Math.min(...v), mx = Math.max(...v), rg = (mx - mn) || 1;
  const pts = v.map((y, i) => `${(i / (v.length - 1) * 58 + 1).toFixed(1)},${(1 + 16 * (y - mn) / rg).toFixed(1)}`).join(" ");
  const last = pts.split(" ").pop().split(",");
  return `<svg class="spark" width="60" height="18" viewBox="0 0 60 18"><polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts}"/><circle cx="${last[0]}" cy="${last[1]}" r="2" fill="currentColor"/></svg>`;
}

// ---------- réglages
function recalcul() { calculer(); clearTimeout(bilanTimer); bilanTimer = setTimeout(calculerBilan, 400); }
for (const k of ["wM", "wT", "wF", "wR"]) $(k).addEventListener("input", e => {
  try { localStorage.setItem("ml_" + k, e.target.value); } catch (_) {} recalcul(); });
$("style").addEventListener("change", e => {
  style = e.target.value; try { localStorage.setItem("ml_style", style); } catch (_) {} recalcul(); });
for (const id of ["formule", "mise"]) $(id).addEventListener(id === "mise" ? "input" : "change", () => {
  try { localStorage.setItem("ml_" + id, $(id).value); } catch (_) {} recalcul(); });

$("copier").addEventListener("click", async e => {
  const t = e.target.dataset.txt || "";
  try { await navigator.clipboard.writeText(t); e.target.textContent = "Copié : " + t; }
  catch (_) { e.target.textContent = t; }
  setTimeout(() => e.target.textContent = "Copier les numéros", 2500);
});

function afficherSources() {
  $("srcListe").innerHTML = Object.entries(sources).map(([n, c]) =>
    `<span class="chip">${esc(n)} · ${Object.keys(c).length} cotes <button type="button" data-n="${esc(n)}" style="border:0;background:none;color:inherit;cursor:pointer" aria-label="Retirer ${esc(n)}">✕</button></span>`).join("");
  $("srcListe").querySelectorAll("button").forEach(b => b.onclick = () => {
    delete sources[b.dataset.n]; sauverSources(); afficherSources(); calculer(); });
}
function sauverSources() { try { localStorage.setItem("ml_sources", JSON.stringify(sources)); } catch (_) {} }
$("srcAjout").addEventListener("click", () => {
  const nom = $("srcNom").value.trim() || "Site " + (Object.keys(sources).length + 1);
  const cotes = {};
  for (const l of $("srcTxt").value.split(/\n/)) {
    const m = l.trim().match(/^(\d+)\s*[;:,\t ]\s*(\d+(?:[.,]\d+)?)/);
    if (m) cotes[+m[1]] = parseFloat(m[2].replace(",", "."));
  }
  if (!Object.keys(cotes).length) return;
  sources[nom] = cotes; sauverSources(); afficherSources(); calculer();
  $("srcTxt").value = ""; $("srcNom").value = "";
});
afficherSources();

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
chargerProgramme().catch(e => {
  $("erreur").hidden = false; $("erreur").textContent = e.message + ". Rechargement dans 30 s.";
  etat(false, "Hors ligne"); $("cNom").textContent = "Programme indisponible";
  setTimeout(() => location.reload(), 30000);
});
</script>
</body></html>"""


# ------------------------------------------------------------------ appli installable (Android)

MANIFEST = json.dumps({
    "name": "Multi Live", "short_name": "Multi Live", "lang": "fr",
    "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
    "background_color": "#0f1512", "theme_color": "#1f6b45",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
})

# Service worker minimal : rend l'appli installable, sans rien mettre en cache (les cotes doivent rester fraîches)
SW = """self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
"""

ICON192='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'
ICON512='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'


if __name__ == "__main__":
    main()
