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

# Le PMU coupe l'accès (erreur 504) quand on lui envoie trop de demandes d'un coup : on espace donc les demandes.
# Deux files : les cotes en direct passent tout de suite ; le chargement des courses passées (« fond »)
# est plus lent et s'efface dès qu'une demande en direct arrive, pour ne jamais la faire attendre.
PMU_LOCK = threading.Lock()
PMU_ETAT = {"direct": 0.0, "fond": 0.0, "dernier_direct": 0.0, "pause": 0.0}
ESPACE_DIRECT = 0.05              # secondes entre deux demandes en direct
ESPACE_FOND = 0.25                # secondes entre deux demandes de courses passées
SATURE = (429, 500, 502, 503, 504)
CONTEXTE = threading.local()      # CONTEXTE.fond = True pendant le chargement des courses passées


def get_json(url, essais=2):
    fond = getattr(CONTEXTE, "fond", False)
    for essai in range(essais):
        while True:
            with PMU_LOCK:
                maintenant = time.time()
                if fond:
                    libre = max(PMU_ETAT["fond"], PMU_ETAT["dernier_direct"] + 0.35)
                    if libre <= maintenant:
                        PMU_ETAT["fond"] = maintenant + ESPACE_FOND
                        break
                    attente = libre - maintenant
                else:
                    attente = PMU_ETAT["direct"] - maintenant
                    PMU_ETAT["direct"] = max(maintenant, PMU_ETAT["direct"]) + ESPACE_DIRECT
                    PMU_ETAT["dernier_direct"] = max(maintenant, PMU_ETAT["direct"])
                    if attente <= 0:
                        break
            time.sleep(min(max(attente, 0.01), 1.0))
            if not fond:
                break
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in SATURE:
                PMU_ETAT["pause"] = time.time() + 180
                if essai + 1 < essais:
                    time.sleep(2)
                    continue
            raise


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


def est_handicap(c):
    """Course à handicap, d'après la catégorie du PMU (HANDICAP, HANDICAP_DIVISE…) ou, à défaut, le texte des conditions."""
    return "HANDICAP" in str(c.get("categorieParticularite") or "").upper() or "handicap" in str(c.get("conditions") or "").lower()[:200]


PROG_DIRECT = {}        # jour -> (heure de lecture, courses) : le programme du jour est gardé 30 secondes


def liste_courses(jour):
    if DEMO:
        return demo_programme(jour)
    vu = PROG_DIRECT.get(jour)
    if vu and time.time() - vu[0] < 30:
        return vu[1]
    data = get_json(f"{API}/{jour}?specialisation=INTERNET")
    out = []
    for reu in data.get("programme", {}).get("reunions", []):
        r = reu.get("numOfficiel")
        hippo = (reu.get("hippodrome") or {}).get("libelleCourt", "")
        pays = (reu.get("pays") or {}).get("code", "")
        for c in reu.get("courses", []):
            paris, mini = paris_course(c)
            out.append({
                "r": r, "c": c.get("numOrdre"), "hippodrome": hippo,
                "libelle": c.get("libelle", ""), "heure": c.get("heureDepart"),
                "discipline": c.get("discipline", ""), "distance": c.get("distance"),
                "partants": c.get("nombreDeclaresPartants"), "paris": paris, "mini": mini,
                "statut": c.get("statut", ""), "handicap": est_handicap(c), "pays": pays,
                "arrivee": [n for g in (c.get("ordreArrivee") or []) for n in (g if isinstance(g, list) else [g])][:5],
            })
    out.sort(key=lambda x: x["heure"] or 0)
    PROG_DIRECT.clear()
    PROG_DIRECT[jour] = (time.time(), out)
    return out


def arrivee_course(jour, r, c):
    """Ordre d'arrivée depuis le programme (liste de numéros), ou []."""
    try:
        data = get_json(f"{API}/{jour}/R{r}/C{c}?specialisation=INTERNET")
    except Exception:
        return []
    ordre = data.get("ordreArrivee") or []
    return [n for groupe in ordre for n in (groupe if isinstance(groupe, list) else [groupe])]


def lire_partants(jour, r, c, arrivee_connue=None, chercher_arrivee=True):
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
            "deferre": p.get("deferre") or "", "avis": p.get("avisEntraineur") or "",
            "oeilleres": p.get("oeilleres") or "", "driverChange": bool(p.get("driverChange")),
        })
    arrivee = sorted((p for p in partants if p.get("ordreArrivee")), key=lambda p: p["ordreArrivee"])
    arrivee = [p["num"] for p in arrivee] or arrivee_connue or (arrivee_course(jour, r, c) if chercher_arrivee else [])
    for p in partants:
        p.pop("ordreArrivee", None)
    return partants, arrivee[:5]


PERF_CACHE = {}


def lire_performances(jour, r, c):
    """Dernières courses de chaque cheval : {numPmu: [{"d": date ms, "h": hippodrome, "disc": discipline,
    "dist": mètres, "n": partants, "pl": place (0 = non classé), "j": jockey/driver, "rk": réduction km}]}.
    On ne garde que les courses d'avant le jour de la course étudiée."""
    cle = (jour, r, c)
    if cle in PERF_CACHE:
        return PERF_CACHE[cle]
    try:
        data = get_json(f"{API}/{jour}/R{r}/C{c}/performances-detaillees/pretty?specialisation=INTERNET")
    except Exception:
        return {}
    limite = datetime.strptime(jour, "%d%m%Y").timestamp() * 1000
    out = {}
    for part in data.get("participants", []) or []:
        runs = []
        for cc in part.get("coursesCourues", []) or []:
            d = cc.get("date") or 0
            if not d or d >= limite:
                continue
            lui = next((x for x in cc.get("participants", []) or [] if x.get("itsHim")), None)
            if not lui:
                continue
            place = (lui.get("place") or {}).get("place") or 0
            runs.append({"d": d, "h": cc.get("hippodrome") or "", "disc": cc.get("discipline") or "",
                         "dist": cc.get("distance") or 0, "n": cc.get("nbParticipants") or 0,
                         "pl": place if isinstance(place, int) else 0,
                         "j": lui.get("nomJockey") or "", "rk": lui.get("reductionKilometrique") or 0})
        runs.sort(key=lambda x: -x["d"])
        out[part.get("numPmu")] = runs[:6]
    if out:
        PERF_CACHE[cle] = out
        if len(PERF_CACHE) > 400:
            PERF_CACHE.pop(next(iter(PERF_CACHE)))
    return out


def ajouter_performances(partants, jour, r, c):
    perfs = lire_performances(jour, r, c)
    for p in partants:
        p["perfs"] = perfs.get(p["num"], [])


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
COTES_DIRECT = {}       # (jour, r, c, partie) -> (heure de lecture, données) : absorbe les demandes rapprochées
POOL = ThreadPoolExecutor(max_workers=4)


def details_course(jour, r, c, pari, partie=True):
    if DEMO:
        partants, arrivee = demo_partants()
        probables = demo_probables(partants, pari)
        definitifs = {}
    else:
        # les rapports probables sont demandés en même temps que les cotes, pas l'un après l'autre
        fut = POOL.submit(rapports_probables, jour, r, c, pari) if pari in AVEC_PROBABLES else None
        vu = COTES_DIRECT.get((jour, r, c, partie))
        if vu and time.time() - vu[0] < 3:
            partants, arrivee = json.loads(vu[1])
        else:
            partants, arrivee = lire_partants(jour, r, c, chercher_arrivee=partie)   # pas d'arrivée à chercher avant le départ
            COTES_DIRECT.clear()
            COTES_DIRECT[(jour, r, c, partie)] = (time.time(), json.dumps([partants, arrivee]))
        ajouter_performances(partants, jour, r, c)
        probables = fut.result() if fut else None
        if len(arrivee) >= 3:
            probables = None
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
    """Renvoie (fait, course). fait = False quand il faudra redemander ce jour plus tard (PMU saturé, résultats pas encore publiés)."""
    recent = (datetime.now() - datetime.strptime(jour, "%d%m%Y")).days <= 2
    try:
        if jour not in PROG_CACHE:
            PROG_CACHE[jour] = liste_courses(jour)
        c = course_proche(PROG_CACHE[jour], heure, pari)
        if not c:
            return True, None
        cle = (jour, c["r"], c["c"])
        if cle not in JOUR_CACHE:
            partants, arrivee = lire_partants(jour, c["r"], c["c"], arrivee_connue=c.get("arrivee"))
            rap = rapports_definitifs(jour, c["r"], c["c"])
            if not rap or len(arrivee) < 3:
                return (not recent), None
            ajouter_performances(partants, jour, c["r"], c["c"])
            JOUR_CACHE[cle] = {"date": jour, "r": c["r"], "c": c["c"], "hippodrome": c["hippodrome"],
                               "libelle": c["libelle"], "heure": c["heure"], "mini": c["mini"],
                               "distance": c.get("distance"), "discipline": c.get("discipline"),
                               "handicap": bool(c.get("handicap")),
                               "partants": partants, "arrivee": arrivee, "rapports": rap}
            if len(JOUR_CACHE) > 600:
                JOUR_CACHE.pop(next(iter(JOUR_CACHE)))
        return True, JOUR_CACHE[cle]
    except urllib.error.HTTPError as e:
        return (e.code == 404 and not recent), None      # 404 : programme introuvable pour ce jour
    except Exception:
        return False, None


def bilan(dates, heure, pari):
    """Courses passées pour une liste de dates JJMMAAAA : {"resultats": {date: course ou None}, "sature": bool}.
    Seuls les jours traités figurent dans « resultats » ; les autres seront redemandés."""
    resultats = {}
    CONTEXTE.fond = True
    for d in dates:
        if DEMO:
            resultats[d] = demo_jour((datetime.now() - datetime.strptime(d, "%d%m%Y")).days, heure)
            continue
        if time.time() < PMU_ETAT["pause"]:
            return {"resultats": resultats, "sature": True}
        fait, course = bilan_jour(d, heure, pari)
        if fait:
            resultats[d] = course
    return {"resultats": resultats, "sature": time.time() < PMU_ETAT["pause"]}


def types_jours(dates):
    """Handicap ou non, pour toutes les courses de chaque date : {"resultats": {date: {"r-c": bool}}, "sature": bool}.
    Une seule demande au PMU par jour (le programme). Sert à compléter les courses déjà gardées sur le téléphone."""
    resultats = {}
    CONTEXTE.fond = True
    for d in dates:
        if DEMO:
            resultats[d] = {f"1-{c}": (int(d[:2]) + c) % 3 == 0 for c in (3, 11, 15, 17)}
            continue
        if time.time() < PMU_ETAT["pause"]:
            break
        try:
            if d not in PROG_CACHE:
                PROG_CACHE[d] = liste_courses(d)
            resultats[d] = {f"{c['r']}-{c['c']}": bool(c.get("handicap")) for c in PROG_CACHE[d]}
        except Exception:
            pass
    return {"resultats": resultats, "sature": time.time() < PMU_ETAT["pause"]}


# ------------------------------------------------------------------ collecte complète : toutes les courses d'un jour
def type_brut(t):
    """E_SIMPLE_PLACE_INTERNATIONAL -> (SIMPLE_PLACE, False) ; E_MINI_MULTI -> (MULTI, True)."""
    t = str(t or "").upper()
    t = t[2:] if t.startswith("E_") else t
    t = t.replace("_INTERNATIONAL", "").replace("TIERCÉ", "TIERCE")
    return ("MULTI", True) if t == "MINI_MULTI" else (t, False)


def tous_rapports(jour, r, c):
    """Tous les rapports définitifs, y compris les paris étrangers : {TYPE: [[combinaison], € pour 1 €, libellé]}.
    Les erreurs PMU remontent (pour reconnaître la saturation)."""
    try:
        data = get_json(f"{API}/{jour}/R{r}/C{c}/rapports-definitifs?specialisation=INTERNET")
    except urllib.error.HTTPError as e:
        if e.code in (204, 404):
            return {}
        raise
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    out = {}
    for pari in data or []:
        t, _ = type_brut(pari.get("typePari"))
        if not t or pari.get("rembourse"):
            continue
        for rp in pari.get("rapports", []) or []:
            d = rp.get("dividendePourUnEuro")
            try:
                comb = [int(x) for x in str(rp.get("combinaison", "")).split("-")]
            except ValueError:
                continue
            if d:
                out.setdefault(t, []).append([comb, d / 100, str(rp.get("libelle", ""))])
    return out


COMPLET_PAR_APPEL = 6


def collecte_jour(jour, debut):
    """Courses terminées du jour, par paquets : {"courses": [...], "suite": index suivant ou None, "total": n, "sature": bool}.
    Format compact par course ; partants = [num, cote matin, cote finale, place (0 = hors des 5 premiers), partant 1/0]."""
    CONTEXTE.fond = True
    if DEMO:
        if debut:
            return {"courses": [], "suite": None, "total": 0, "sature": False}
        i = (datetime.now() - datetime.strptime(jour, "%d%m%Y")).days
        out = []
        for h in ("11:30", "13:55", "15:45", "17:45"):
            j = demo_jour(i, h)
            out.append({"date": jour, "r": j["r"], "c": j["c"], "hippodrome": j["hippodrome"], "pays": "FRA", "heure": j["heure"],
                        "discipline": j["discipline"], "distance": j["distance"], "handicap": (int(jour[:2]) + j["c"]) % 3 == 0,
                        "mini": False, "arrivee": j["arrivee"],
                        "partants": [[p["num"], p.get("coteMatin"), p.get("coteDirect"), (j["arrivee"].index(p["num"]) + 1) if p["num"] in j["arrivee"] else 0,
                                      1 if p["partant"] else 0] for p in j["partants"]],
                        "rapports": {t: [[x["c"], x["d"], x["l"]] for x in v] for t, v in j["rapports"].items()}})
        return {"courses": out, "suite": None, "total": len(out), "sature": False}
    if time.time() < PMU_ETAT["pause"]:
        return {"courses": [], "suite": debut, "total": 0, "sature": True}
    if jour not in PROG_CACHE:
        PROG_CACHE[jour] = liste_courses(jour)
    toutes = [c for c in PROG_CACHE[jour] if len(c.get("arrivee") or []) >= 3]
    out = []
    i = debut
    try:
        while i < len(toutes) and i < debut + COMPLET_PAR_APPEL:
            c = toutes[i]
            partants, arrivee = lire_partants(jour, c["r"], c["c"], arrivee_connue=c.get("arrivee"), chercher_arrivee=False)
            out.append({"date": jour, "r": c["r"], "c": c["c"], "hippodrome": c["hippodrome"], "pays": c.get("pays", ""), "heure": c["heure"],
                        "discipline": c.get("discipline"), "distance": c.get("distance"), "handicap": bool(c.get("handicap")),
                        "mini": c.get("mini"), "libelle": c.get("libelle"), "arrivee": arrivee,
                        "partants": [[p["num"], p["coteMatin"], p["coteDirect"], (arrivee.index(p["num"]) + 1) if p["num"] in arrivee else 0,
                                      1 if p["partant"] else 0] for p in partants],
                        "rapports": tous_rapports(jour, c["r"], c["c"])})
            i += 1
    except urllib.error.HTTPError as e:
        if e.code not in SATURE:
            raise
        return {"courses": out, "suite": i, "total": len(toutes), "sature": True}
    return {"courses": out, "suite": i if i < len(toutes) else None, "total": len(toutes), "sature": False}


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
         "discipline": "ATTELE", "distance": 2700, "partants": 12, "paris": petit, "mini": False, "statut": "FIN_COURSE", "handicap": False},
        {"r": 1, "c": 3, "hippodrome": "VINCENNES", "libelle": "PRIX DE RUNGIS (démo)", "heure": ms(base),
         "discipline": "ATTELE", "distance": 2850, "partants": 16, "paris": DEMO_PARIS, "mini": False, "statut": "PROGRAMMEE", "handicap": True},
        {"r": 2, "c": 5, "hippodrome": "LONGCHAMP", "libelle": "PRIX DES ETANGS", "heure": ms(base + timedelta(minutes=80)),
         "discipline": "PLAT", "distance": 1600, "partants": 11, "paris": petit + [{"t": "MULTI", "base": 3}, {"t": "PICK5", "base": 1}],
         "mini": True, "statut": "PROGRAMMEE", "handicap": True},
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


def demo_extras(partants, graine):
    """Ferrage, avis de l'entraîneur et dernières courses fictifs, stables d'un appel à l'autre."""
    rnd = random.Random(graine)
    jour0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for p in partants:
        p["deferre"] = rnd.choice(["", "", "DEFERRE_ANTERIEURS_POSTERIEURS", "DEFERRE_ANTERIEURS", "PROTEGE_ANTERIEURS"])
        p["avis"] = rnd.choice(["NEUTRE", "NEUTRE", "POSITIF", "NEGATIF"])
        p["oeilleres"], p["driverChange"] = "SANS_OEILLERES", False
        places = [int(ch) if ch.isdigit() else 0 for ch in p["musique"].replace("(25)", "")[::2]][:5]
        age = rnd.randint(9, 30)
        p["perfs"] = []
        for pl in places:
            p["perfs"].append({"d": int((jour0 - timedelta(days=age)).timestamp() * 1000),
                               "h": rnd.choice(["VINCENNES", "ENGHIEN", "CABOURG"]), "disc": "ATTELE",
                               "dist": rnd.choice([2100, 2700, 2850]), "n": 14, "pl": pl,
                               "j": p["driver"] if rnd.random() < 0.6 else "X. AUTRE", "rk": rnd.randint(72000, 77000)})
            age += rnd.randint(12, 40)
    return partants


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
    return demo_extras(out, 7), []


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


def demo_jour(i, heure="13:55"):
    """Course passée fictive (il y a i jours, vers l'heure demandée) : arrivée tirée au sort selon les cotes,
    rapports ~ 70-75 % du juste prix."""
    hh, mm = (int(x) for x in heure.split(":"))
    rnd = random.Random(1000 + i + 100000 * hh)
    d = datetime.now() - timedelta(days=i)
    partants = []
    for n, nom, mus, matin, drv, crs, vic, pl in DEMO_CHEVAUX[:rnd.randint(13, 16)]:
        cote = round(matin * rnd.uniform(0.5, 1.8), 1)
        partants.append({"num": n, "nom": nom, "partant": True, "musique": mus, "driver": drv,
                         "courses": crs, "victoires": vic, "places": pl,
                         "coteMatin": round(cote * rnd.uniform(0.85, 1.15), 1), "coteDirect": cote})
    demo_extras(partants, i)
    reste, arr = dict(_probas(partants)), []
    for _ in range(5):
        x, acc = rnd.random() * sum(reste.values()), 0
        for k, v in reste.items():
            acc += v
            if acc >= x:
                arr.append(k)
                reste.pop(k)
                break
    return {"date": d.strftime("%d%m%Y"), "r": 1, "c": 3 if hh == 13 else hh, "hippodrome": "VINCENNES", "libelle": "Course démo",
            "heure": int(d.replace(hour=hh, minute=mm).timestamp() * 1000), "mini": False,
            "distance": 2850, "discipline": "ATTELE" if hh in (13, 17) else "PLAT",
            "partants": partants, "arrivee": arr, "rapports": demo_definitifs(partants, arr, rnd)}


def demo_pmu(chemin):
    """Réponses au format exact du PMU, fabriquées à partir des données de démo (test de la connexion directe)."""
    morceaux = [m for m in chemin.split("/") if m]
    if len(morceaux) == 1:
        reunions = {}
        for c in demo_programme(morceaux[0]):
            reu = reunions.setdefault(c["r"], {"numOfficiel": c["r"], "hippodrome": {"libelleCourt": c["hippodrome"]}, "courses": []})
            reu["courses"].append({"numOrdre": c["c"], "libelle": c["libelle"], "heureDepart": c["heure"],
                                   "discipline": c["discipline"], "distance": c["distance"],
                                   "nombreDeclaresPartants": c["partants"], "statut": c["statut"],
                                   "categorieParticularite": "HANDICAP_DIVISE" if c["handicap"] else "COURSE_A_CONDITIONS",
                                   "paris": [{"typePari": "E_" + ("MINI_MULTI" if p["t"] == "MULTI" and c["mini"] else p["t"]),
                                              "miseBase": int(p["base"] * 100)} for p in c["paris"]]})
        return {"programme": {"reunions": list(reunions.values())}}
    partants, _ = demo_partants()
    if morceaux[-1] == "participants":
        return {"participants": [{"numPmu": p["num"], "nom": p["nom"], "statut": "PARTANT" if p["partant"] else "NON_PARTANT",
                                  "musique": p["musique"], "driver": p["driver"], "nombreCourses": p["courses"],
                                  "nombreVictoires": p["victoires"], "nombrePlaces": p["places"],
                                  "dernierRapportDirect": {"rapport": p["coteDirect"]} if p["coteDirect"] else None,
                                  "dernierRapportReference": {"rapport": p["coteMatin"]},
                                  "deferre": p["deferre"], "avisEntraineur": p["avis"], "oeilleres": p["oeilleres"],
                                  "driverChange": False} for p in partants]}
    if "performances-detaillees" in morceaux:
        return {"participants": [{"numPmu": p["num"], "coursesCourues": [
            {"date": r["d"], "hippodrome": r["h"], "discipline": r["disc"], "distance": r["dist"], "nbParticipants": r["n"],
             "participants": [{"itsHim": False, "place": {"place": 1}, "nomJockey": "X"},
                              {"itsHim": True, "place": {"place": r["pl"]}, "nomJockey": r["j"], "reductionKilometrique": r["rk"]}]}
            for r in p["perfs"]]} for p in partants]}
    if "rapports" in morceaux:
        prob = demo_probables(partants, morceaux[-1].replace("E_", "")) or []
        return {"rapportsParticipant": [{"numerosParticipant": x[0], "rapportDirect": x[1],
                                         "minRapportProbable": x[2], "maxRapportProbable": x[3]} for x in prob]}
    return {}


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
            if u.path == "/ping":
                return self.envoyer(200, "ok", "text/plain; charset=utf-8")
            if u.path == "/manifest.webmanifest":
                return self.envoyer(200, MANIFEST, "application/manifest+json")
            if u.path in ("/icon-192.png", "/icon-512.png"):
                data = base64.b64decode(ICON192 if "192" in u.path else ICON512)
                return self.envoyer(200, data, "image/png")
            if u.path == "/sw.js":
                return self.envoyer(200, SW, "text/javascript; charset=utf-8")
            if DEMO and u.path.startswith("/pmu/"):
                return self.envoyer(200, json.dumps(demo_pmu(u.path[5:])))
            if DEMO and u.path == "/api/panne":          # test : simule un PMU qui bloque le serveur
                PMU_ETAT["panne"] = q.get("on", "1") == "1"
                return self.envoyer(200, "{}")
            if DEMO and PMU_ETAT.get("panne") and u.path in ("/api/courses", "/api/course"):
                return self.envoyer(502, json.dumps({"erreur": "Le PMU ne répond pas pour le moment (erreur 504)"}))
            if u.path == "/api/courses":
                return self.envoyer(200, json.dumps({"date": jour, "demo": DEMO, "courses": liste_courses(jour)}))
            if u.path == "/api/bilan":
                dates = [d for d in q.get("dates", "").split(",") if len(d) == 8 and d.isdigit()][:12]
                return self.envoyer(200, json.dumps(bilan(dates, q.get("heure", "13:55"), pari)))
            if u.path == "/api/complet":
                d = q.get("date", "")
                if not (len(d) == 8 and d.isdigit()):
                    return self.envoyer(400, json.dumps({"erreur": "date invalide"}))
                return self.envoyer(200, json.dumps(collecte_jour(d, int(q.get("debut", "0") or 0))))
            if u.path == "/api/types":
                dates = [d for d in q.get("dates", "").split(",") if len(d) == 8 and d.isdigit()][:20]
                return self.envoyer(200, json.dumps(types_jours(dates)))
            if u.path == "/api/course":
                return self.envoyer(200, json.dumps(details_course(jour, int(q["r"]), int(q["c"]), pari, q.get("partie", "1") == "1")))
            return self.envoyer(404, json.dumps({"erreur": "introuvable"}))
        except urllib.error.HTTPError as e:
            msg = ("Programme pas encore publié par le PMU" if e.code in (204, 404)
                   else f"Le PMU ne répond pas pour le moment (erreur {e.code}), sans doute trop de demandes d'affilée. Ça revient tout seul"
                   if e.code in SATURE else f"Le PMU a répondu {e.code}")
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
.repere{margin-top:12px;padding:12px 14px;border-radius:10px;border:2px solid var(--gold);background:color-mix(in srgb,var(--gold) 10%,transparent)}
.repere h3{margin:0 0 6px;font-size:15px;color:var(--gold)} .repere .n{width:40px;height:40px;border-radius:8px;display:grid;place-items:center;font-size:17px;font-weight:700;background:var(--gold);color:var(--surface)}
.repere .nums{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px} .repere p{margin:4px 0;font-size:13px}
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
.optim{margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:8px;display:grid;gap:6px}
.optim b.t{font-size:15px}
.optim ul{margin:0;padding-left:18px}
.optim .copie{justify-self:start;margin-top:4px}
.bilan details{margin-top:12px}
table.btab.ttab{min-width:0}
table.ttab td.l,table.ttab th.l{white-space:normal}
table.ttab th,table.ttab td{padding:7px 6px}
tr.grp td{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding-top:12px}
.tk{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.tk:last-child{border-bottom:0}
.tk .lab{flex:0 0 100%;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.tk .lab b{color:var(--ink);text-transform:none;letter-spacing:0;font-size:12px;margin-left:6px}
.ticket .nums.plusieurs{display:block;margin:0 0 10px}
.ticket .nums.plusieurs .n{width:40px;height:40px;font-size:17px}
.ticket .nums.plusieurs.ord .n{height:50px}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:3px;width:230px;max-width:60vw}
.tag{font-size:11px;padding:1px 6px;border-radius:99px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.tag.plus{border-color:var(--down);color:var(--down)} .tag.moins{border-color:var(--up);color:var(--up)}
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
  <div class="champ"><label for="typeC">Type de course</label>
    <select id="typeC"><option value="tous">Toutes les courses</option><option value="attele">Trot attelé</option><option value="monte">Trot monté</option>
      <option value="plat">Plat</option><option value="obstacle">Obstacles</option><option value="handicap">Handicaps</option><option value="sanshandicap">Sans handicap</option><option value="repere">★ Courses repérées</option></select></div>
  <div class="champ large"><label for="choixCourse">Course</label><select id="choixCourse"></select></div>
  <div class="champ"><label for="pari">Pari</label><select id="pari"></select></div>
  <div class="champ"><label for="style">Style</label>
    <select id="style"><option value="prudent">Prudent</option><option value="equilibre">Équilibré</option><option value="outsiders">Outsiders</option></select></div>
  <div class="champ" id="formuleBox" hidden><label for="formule">Formule</label>
    <select id="formule"><option value="4">en 4</option><option value="5">en 5</option><option value="6" selected>en 6</option><option value="7">en 7</option></select></div>
  <div class="champ"><label for="nbt">Tickets</label>
    <select id="nbt"><option value="1">1 ticket</option><option value="2">2 tickets</option><option value="3">3 tickets</option><option value="4">4 tickets</option><option value="5">5 tickets</option></select></div>
  <div class="champ"><label for="mise">Mise par ticket</label>
    <span class="miseBox"><input id="mise" type="number" inputmode="decimal" min="1" max="60" step="0.5" value="5"> €</span></div>
</div>

<div class="course">
  <span class="nom" id="cNom">Chargement du programme…</span>
  <span class="meta" id="cMeta"></span>
  <span class="compte" id="compte"></span>
</div>
<div id="erreur" class="err" hidden></div>
<div id="resultat" class="res" hidden></div>
<div id="repere" class="repere" hidden></div>

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
  <div class="flexi">
    <h2>Outsiders à surveiller</h2>
    <div id="outListe"></div>
  </div>
  <p class="note" id="tNote"></p>
</section>

<section class="panel bilan">
  <h2 id="bTitre">Test sur les courses passées</h2>
  <div class="choix" style="margin:0 0 8px">
    <div class="champ"><label for="periode">Période testée</label>
      <select id="periode"><option value="30">30 derniers jours</option><option value="90">3 derniers mois</option><option value="180">6 derniers mois</option></select></div>
    <div class="champ"><label for="parjour">Courses par jour</label>
      <select id="parjour"><option value="1">1, vers 13h55</option><option value="4">4, du matin au soir</option></select></div>
    <div class="champ"><label for="joues">Courses jouées</label>
      <select id="joues"><option value="tous">Toutes</option><option value="jouable">Sauf verdict défavorable</option><option value="favorable">Seulement verdict favorable</option></select></div>
  </div>
  <p class="sub" id="bEtat">Chargement…</p>
  <div class="bcartes" id="bCartes"></div>
  <button class="copie" id="optim" type="button">Chercher le meilleur réglage</button>
  <div id="optimRes" class="optim" hidden></div>
  <details><summary>Comparer les paris</summary>
    <p class="sub">Ce que chaque pari aurait rendu sur ces mêmes courses en jouant simplement les favoris, pour 100 € misés. Ceux du haut sont ceux qui perdent le moins. Un chiffre un peu au-dessus de 100 € ne prouve pas un pari gagnant : sur quelques centaines de courses, le hasard fait bouger le résultat de 10 à 15 €.</p>
    <div class="tablewrap"><table class="btab ttab"><thead><tr><th class="l">Pari joué à chaque course</th><th>Courses</th><th>Gagné</th><th>Rendu</th></tr></thead><tbody id="bParis"></tbody></table></div>
  </details>
  <details><summary>Résultats par type de course</summary>
    <p class="sub">Part de la mise récupérée avec ton style actuel, comparée aux favoris. Moins il y a de courses dans une ligne, moins le chiffre est fiable.</p>
    <div class="tablewrap"><table class="btab ttab"><thead><tr><th class="l">Type de course</th><th>Courses</th><th>Appli</th><th>Favoris</th></tr></thead><tbody id="bTypes"></tbody></table></div>
  </details>
  <details><summary>Détail jour par jour</summary>
    <div class="tablewrap"><table class="btab"><thead><tr><th class="l">Jour</th><th class="l">Course</th><th class="l">Arrivée</th><th>Prudent</th><th>Équilibré</th><th>Outsiders</th><th>Favoris</th></tr></thead><tbody id="bCorps"></tbody></table></div>
  </details>
  <button class="copie" id="exporter" type="button">Exporter les courses du test</button>
  <p class="sub" id="exportEtat" hidden></p>
  <details id="complet"><summary>Collecte complète : toutes les courses de chaque jour, sur 6 mois</summary>
    <p class="sub">Charge toutes les courses de chaque journée (une cinquantaine par jour, réunions étrangères comprises), avec la cote du matin, la cote finale, l'arrivée et tous les rapports du PMU. Compte environ 1 h 30 pour 6 mois : garde l'appli ouverte, écran allumé. Tu peux l'arrêter et la reprendre quand tu veux, elle repart où elle s'était arrêtée.</p>
    <button class="copie" id="cLancer" type="button">Lancer la collecte</button>
    <button class="copie" id="cExport" type="button">Exporter le fichier complet</button>
    <p class="sub" id="cEtat"></p>
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

<p class="sub" style="margin:8px 0 0">Étiquettes : <b>distance ✓</b>, <b>piste ✓</b>, <b>driver ✓</b> ou <b>jockey ✓</b> = le cheval a déjà bien couru sur cette distance, sur cette piste ou avec lui · <b>chrono ✓</b> = parmi les 3 meilleurs chronos récents (trot) · <b>avis + / −</b> = avis de l'entraîneur · <b>rentrée</b> = 2 mois ou plus sans courir · <b>non classé</b> = pas classé à sa dernière course (disqualifié, arrêté, tombé…). Sur 373 vraies courses, ces deux-là ont fini un peu moins souvent dans les 4 premiers que leur cote ne le laissait prévoir : c'est une indication, pas une règle.</p>

<section class="panel reglages">
  <details>
    <summary>Réglages avancés : poids des analyses, cotes d'autres sites</summary>
    <div class="sliders">
      <div class="sl"><label for="wM">Cotes du marché</label><input id="wM" type="range" min="0" max="100" value="100"><span id="vM"></span>
        <small>Ce que pensent les parieurs : la meilleure base, mais elle intègre la marge du PMU.</small></div>
      <div class="sl"><label for="wT">Mouvement des cotes</label><input id="wT" type="range" min="0" max="100" value="0"><span id="vT"></span>
        <small>Cote qui baisse depuis le matin = de l'argent qui rentre sur le cheval.</small></div>
      <div class="sl"><label for="wF">Forme récente</label><input id="wF" type="range" min="0" max="100" value="0"><span id="vF"></span>
        <small>Les 5 dernières places de la musique, la plus récente compte le plus.</small></div>
      <div class="sl"><label for="wR">Régularité</label><input id="wR" type="range" min="0" max="100" value="0"><span id="vR"></span>
        <small>Part des courses finies placé sur toute la carrière.</small></div>
      <div class="sl"><label for="wA">Aptitudes</label><input id="wA" type="range" min="0" max="100" value="0"><span id="vA"></span>
        <small>Résultats du cheval sur cette distance, sur cette piste et avec ce jockey ou driver, plus son chrono au trot.</small></div>
      <div class="sl"><label for="wS">Signaux du jour</label><input id="wS" type="range" min="0" max="100" value="0"><span id="vS"></span>
        <small>Ferrage (déferré), avis de l'entraîneur, fraîcheur depuis la dernière course.</small></div>
    </div>
    <p class="sub" style="margin-top:14px">Cotes d'un autre site : une ligne par cheval, <code>numéro;cote</code>. Donne un nom au site, puis Ajouter.</p>
    <input id="srcNom" placeholder="Nom du site (ex. Zeturf)" class="srcNom">
    <textarea id="srcTxt" placeholder="3;3,8&#10;1;4,1&#10;7;6,5"></textarea>
    <button class="copie" id="srcAjout" type="button">Ajouter</button>
    <div class="srcs" id="srcListe"></div>
  </details>
</section>

<p class="foot">Estimations à partir des données du PMU : cotes, musique, statistiques de carrière, dernières courses (distance, piste, jockey ou driver, chrono), ferrage et avis de l'entraîneur. Le terrain n'est pas pris en compte : le PMU ne le publie pas pour les courses passées. Elles aident à choisir, elles ne garantissent rien : le PMU prélève une part des mises, donc sur la durée aucune méthode ne gagne à coup sûr. Joue seulement ce que tu acceptes de perdre.</p>
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
  for (const k of ["wM", "wT", "wF", "wR", "wA", "wS"]) { const v = localStorage.getItem("ml_" + k); if (v !== null) $(k).value = v; }
  const f = localStorage.getItem("ml_formule"); if (f) $("formule").value = f;
  const mi = localStorage.getItem("ml_mise"); if (mi) $("mise").value = mi;
  const pp = localStorage.getItem("ml_pari"); if (pp && PARIS[pp]) prefPari = pp;
  const nt = localStorage.getItem("ml_nbt"); if (nt) $("nbt").value = nt;
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
function nbTickets() { return Math.max(1, Math.min(5, +$("nbt").value || 1)); }
function maMise() { let v = parseFloat(String($("mise").value).replace(",", ".")); if (!isFinite(v)) v = 3; return Math.min(60, Math.max(1, v)); }
const CURSEURS = ["wM", "wT", "wF", "wR", "wA", "wS"];
const poids = () => ({M: +$("wM").value, T: +$("wT").value, F: +$("wF").value, R: +$("wR").value, A: +$("wA").value, S: +$("wS").value});
const offre = (c, t) => (c.paris || []).some(p => p.t === t);
const tailleTicket = t => t === "MULTI" ? formule() : PARIS[t].k;
const nomPari = (t, c) => t === "MULTI" && c && c.mini ? "Mini Multi" : PARIS[t].nom;

// ---------- secours : si le serveur n'obtient plus rien du PMU, le téléphone l'interroge lui-même
const PMU = /[?&]pmutest/.test(location.search) ? "/pmu" : "https://online.turfinfo.api.pmu.fr/rest/client/1/programme";
const SUFFIXE = PMU === "/pmu" ? "" : "?specialisation=INTERNET";
const AVEC_PROBABLES = new Set(["SIMPLE_PLACE", "COUPLE_GAGNANT", "COUPLE_PLACE", "COUPLE_ORDRE", "DEUX_SUR_QUATRE", "TRIO", "TRIO_ORDRE"]);
let direct = false;                         // true = le téléphone interroge le PMU sans passer par le serveur
const directMem = {perfs: {}, histo: {}, definitifs: {}};
async function pmuJson(chemin) {
  const r = await fetch(PMU + "/" + chemin + SUFFIXE);
  if (!r.ok) throw new Error("PMU " + r.status);
  return r.json();
}
function typePMU(tp) {                      // "E_MINI_MULTI" -> ["MULTI", true]
  let t = String(tp || "").toUpperCase().replace(/^E_/, "").replace("TIERCÉ", "TIERCE");
  if (t === "MINI_MULTI") return ["MULTI", true];
  return [PARIS[t] ? t : null, false];
}
const aplatir = o => (o || []).reduce((a, g) => a.concat(g), []).slice(0, 5);
async function directCourses() {
  const jour = jjmmaaaa(new Date()), data = await pmuJson(jour), ordre = Object.keys(PARIS), out = [];
  for (const reu of (data.programme || {}).reunions || []) for (const c of reu.courses || []) {
    const paris = []; let mini = false;
    for (const p of c.paris || []) { const [t, m] = typePMU(p.typePari);
      if (t && !paris.some(x => x.t === t)) { paris.push({t, base: (p.miseBase || 0) / 100}); mini = mini || m; } }
    paris.sort((a, b) => ordre.indexOf(a.t) - ordre.indexOf(b.t));
    out.push({r: reu.numOfficiel, c: c.numOrdre, hippodrome: (reu.hippodrome || {}).libelleCourt || "", libelle: c.libelle || "",
              heure: c.heureDepart, discipline: c.discipline || "", distance: c.distance, partants: c.nombreDeclaresPartants,
              handicap: /HANDICAP/i.test(c.categorieParticularite || "") || /handicap/i.test(String(c.conditions || "").slice(0, 200)),
              paris, mini, statut: c.statut || "", arrivee: aplatir(c.ordreArrivee)});
  }
  out.sort((a, b) => (a.heure || 0) - (b.heure || 0));
  return {date: jour, demo: false, courses: out};
}
async function directCourse(co, t, partie) {
  const jour = jjmmaaaa(new Date()), base = `${jour}/R${co.r}/C${co.c}`, cle = base;
  const data = await pmuJson(base + "/participants");
  const partants = (data.participants || []).map(p => ({
    num: p.numPmu, nom: p.nom || "?", partant: String(p.statut || "PARTANT").toUpperCase() === "PARTANT",
    musique: p.musique || "", driver: p.driver || p.jockey || "",
    courses: p.nombreCourses || 0, victoires: p.nombreVictoires || 0, places: p.nombrePlaces || 0,
    coteMatin: (p.dernierRapportReference || {}).rapport || null, coteDirect: (p.dernierRapportDirect || {}).rapport || null,
    deferre: p.deferre || "", avis: p.avisEntraineur || "", oeilleres: p.oeilleres || "", driverChange: !!p.driverChange}));
  // dernières courses de chaque cheval : demandées une seule fois par course
  if (!directMem.perfs[cle]) {
    try {
      const pd = await pmuJson(base + "/performances-detaillees/pretty"), lim = new Date(); lim.setHours(0, 0, 0, 0);
      const m = {};
      for (const part of pd.participants || []) m[part.numPmu] = (part.coursesCourues || []).filter(cc => cc.date && cc.date < +lim).map(cc => {
        const lui = (cc.participants || []).find(x => x.itsHim); if (!lui) return null;
        const pl = (lui.place || {}).place;
        return {d: cc.date, h: cc.hippodrome || "", disc: cc.discipline || "", dist: cc.distance || 0, n: cc.nbParticipants || 0,
                pl: Number.isInteger(pl) ? pl : 0, j: lui.nomJockey || "", rk: lui.reductionKilometrique || 0};
      }).filter(Boolean).sort((a, b) => b.d - a.d).slice(0, 6);
      directMem.perfs[cle] = m;
    } catch (e) {}
  }
  const perfs = directMem.perfs[cle] || {};
  let arrivee = [];
  if (partie) { try { arrivee = aplatir((await pmuJson(base)).ordreArrivee); } catch (e) {} }
  let probables = null, rapports = {};
  if (arrivee.length < 3 && AVEC_PROBABLES.has(t)) {
    try { probables = ((await pmuJson(base + "/rapports/E_" + t)).rapportsParticipant || [])
      .filter(x => (x.numerosParticipant || []).length && (x.rapportDirect || x.minRapportProbable || x.maxRapportProbable))
      .map(x => [x.numerosParticipant, x.rapportDirect, x.minRapportProbable, x.maxRapportProbable]);
      if (!probables.length) probables = null; } catch (e) {}
  }
  if (arrivee.length >= 3) {
    if (!directMem.definitifs[cle]) { try {
      let d = await pmuJson(base + "/rapports-definitifs"); if (!Array.isArray(d)) d = Object.values(d).find(Array.isArray) || [];
      const out = {};
      for (const pa of d) { const [tt] = typePMU(pa.typePari); if (!tt || pa.rembourse) continue;
        for (const rp of pa.rapports || []) { const comb = String(rp.combinaison || "").split("-").map(Number);
          if (rp.dividendePourUnEuro && comb.every(Number.isInteger)) (out[tt] = out[tt] || []).push({l: String(rp.libelle || ""), c: comb, d: rp.dividendePourUnEuro / 100}); } }
      if (Object.keys(out).length) directMem.definitifs[cle] = out; } catch (e) {} }
    rapports = directMem.definitifs[cle] || {};
  }
  const now = Date.now(), h = directMem.histo[cle] || (directMem.histo[cle] = {});
  for (const p of partants) { p.perfs = perfs[p.num] || [];
    if (p.coteDirect) { const s = h[p.num] || (h[p.num] = []); const d = s[s.length - 1];
      if (!d || d[1] !== p.coteDirect || now - d[0] > 60000) { s.push([now, p.coteDirect]); if (s.length > 120) s.shift(); } }
    p.histo = h[p.num] || []; }
  return {partants, arrivee, probables, rapports, maj: now};
}
// Demande au serveur ; s'il échoue, essaie en direct, et y reste pour le reste de la session
async function avecSecours(viaServeur, enDirect) {
  if (!direct && !/[?&]direct/.test(location.search)) {
    try { return await viaServeur(); }
    catch (e) { try { const d = await enDirect(); direct = true; return d; } catch (_) { throw e; } }
  }
  return enDirect();
}
async function viaServeur(url, defaut) {
  const r = await fetch(url); const j = await r.json();
  if (!r.ok) throw new Error(j.erreur || defaut);
  return j;
}

// ---------- type de course : un seul choix, qui trie à la fois les courses du jour et le test sur les courses passées
const TYPES_C = {repere: "★ Courses repérées", tous: "Toutes les courses", attele: "Trot attelé", monte: "Trot monté", plat: "Plat", obstacle: "Obstacles", handicap: "Handicaps", sanshandicap: "Sans handicap"};
try { const ty = localStorage.getItem("ml_type"); if (ty && TYPES_C[ty]) $("typeC").value = ty; } catch (e) {}
const nomDisc = d => { d = (d || "").toUpperCase(); return /ATTELE/.test(d) ? "Trot attelé" : /MONTE/.test(d) ? "Trot monté" : /PLAT/.test(d) ? "Plat" : /HAIE|STEEPLE|CROSS/.test(d) ? "Obstacles" : "Autre"; };
function typeOk(c) {                 // c = course du jour ou course passée
  const v = $("typeC").value;
  if (v === "tous") return true;
  if (v === "repere") return repereProg(c);
  if (v === "handicap") return c.handicap === true;
  if (v === "sanshandicap") return c.handicap === false;
  return nomDisc(c.discipline) === TYPES_C[v];
}
// ---------- courses repérées : la piste la plus solide du test sur 6 mois de vraies courses
// Multi (14 partants ou plus, pas le Mini Multi), course à handicap ou course ouverte (favori à moins de 20 % de chances)
// → Multi en 5, champ réduit : les 3 favoris en base, les favoris n°4 à 7 en associés (6 combinaisons).
const multiVrai = c => (c.paris || []).some(p => p.t === "MULTI") && !c.mini;
const repereProg = c => !!c && multiVrai(c) && c.handicap === true;        // visible dès le programme (sans les cotes)
function majRepere(P) {
  const box = $("repere"), optM = $("pari").querySelector('option[value="MULTI"]');
  const avec = P.filter(c => c.coteDirect > 1).sort((a, b) => a.coteDirect - b.coteDirect);
  const s = avec.reduce((x, c) => x + 1 / c.coteDirect, 0), favP = avec.length ? (1 / avec[0].coteDirect) / s : 1;
  const ouverte = favP < 0.2, ok = multiVrai(courant) && (courant.handicap === true || ouverte) && avec.length >= 8;
  if (optM) optM.textContent = (ok ? "★ " : "") + nomPari("MULTI", courant);        // étoile sur le pari à jouer
  if (!ok) { box.hidden = true; return; }
  const bases = avec.slice(0, 3).map(c => c.num), asso = avec.slice(3, 7).map(c => c.num);
  const pq = n => `<span class="n">${n}</span>`;
  box.innerHTML = `<h3>★ Course repérée : ${courant.handicap === true ? "handicap" : "course ouverte"}${courant.handicap === true && ouverte ? " et course ouverte" : ""}</h3>
    <p><b>★ Combinaison conseillée : Multi en 5, champ réduit</b></p>
    <p>Bases (les 3 favoris) :</p><div class="nums">${bases.map(pq).join("")}</div>
    <p>Associés (favoris n°4 à 7) :</p><div class="nums">${asso.map(pq).join("")}</div>
    <p>6 combinaisons : ${euro(18)} en mise de base, ${euro(4.5)} en Flexi 25 %, ${euro(9)} en Flexi 50 %.</p>
    <p class="sub">Sur toutes les courses de ce type pendant 6 mois (738 courses), cette combinaison a gagné 130 fois, environ 1 course sur 6, et a rendu à peu près la mise (98 € pour 100 €). Dans les autres courses de Multi, la même combinaison ne rend que 64 € : c'est pour ça que ces courses sont repérées. Ce n'est pas un pari gagnant, c'est celui qui perd le moins, et seulement joué dans les toutes dernières minutes : choisie avec les cotes du matin, la même combinaison ne rend que 54 €.</p>
    <p class="sub">Plus risqué : Multi en 4 avec les mêmes bases et associés (4 combinaisons, ${euro(12)}) : gagné 45 fois sur 736, environ 1,07 fois la mise, avec de très grands écarts d'un mois à l'autre.</p>
    <p class="sub">Les numéros suivent les cotes en direct : vérifie-les juste avant le départ.</p>`;
  box.hidden = false;
}
function procheDe1355(pool) {
  const [h, m] = HEURE_CIBLE.split(":").map(Number), cible = new Date(); cible.setHours(h, m, 0, 0);
  let choix = pool[0];
  for (const c of pool) if (Math.abs((c.heure || 0) - cible) < Math.abs((choix.heure || 0) - cible)) choix = c;
  return choix;
}
$("typeC").addEventListener("change", e => {
  try { localStorage.setItem("ml_type", e.target.value); } catch (_) {}
  const ok = courses.filter(typeOk);
  if (ok.length && courant && !typeOk(courant)) { const avec = ok.filter(c => offre(c, prefPari)); remplirCourses(); choisir(procheDe1355(avec.length ? avec : ok)); }
  else { remplirCourses(); calculerBilan(); }
});

// ---------- programme
async function chargerProgramme() {
  const j = await avecSecours(() => viaServeur("/api/courses", "Programme indisponible"), directCourses);
  $("modeDemo").textContent = j.demo ? "— démo, courses fictives" : "";
  courses = j.courses.filter(c => (c.paris || []).length);
  remplirCourses();
  // course par défaut : celle qui propose mon pari habituel, la plus proche de 13h55
  const ok = courses.filter(typeOk), base = ok.length ? ok : courses;
  const avec = base.filter(c => offre(c, prefPari));
  const choix = procheDe1355(avec.length ? avec : base);
  if (choix) choisir(choix);
}
function remplirCourses() {
  const sel = $("choixCourse"); sel.innerHTML = "";
  const ok = courses.filter(typeOk);
  if (!ok.length && courses.length) {       // rien de ce type aujourd'hui : on le dit, et on laisse tout le programme
    const o = document.createElement("option"); o.disabled = true; o.value = "";
    o.textContent = `Aucune course « ${TYPES_C[$("typeC").value]} » aujourd'hui : voici toutes les courses`; sel.appendChild(o);
  }
  for (const c of (ok.length ? ok : courses)) {
    const o = document.createElement("option");
    o.value = c.r + "-" + c.c;
    o.textContent = `${repereProg(c) ? "★ " : ""}${c.heure ? hhmm(c.heure) : "--:--"}  R${c.r}C${c.c} ${c.hippodrome}${offre(c, prefPari) ? "  · " + nomPari(prefPari, c) : ""}`;
    sel.appendChild(o);
  }
  if (courant) sel.value = courant.r + "-" + courant.c;
}
$("choixCourse").addEventListener("change", e => {
  const [r, c] = e.target.value.split("-").map(Number);
  choisir(courses.find(x => x.r === r && x.c === c));
});

function choisir(c) {
  courant = c; donnees = null; $("repere").hidden = true;
  $("choixCourse").value = c.r + "-" + c.c;
  // paris proposés sur cette course
  const sel = $("pari"); sel.innerHTML = "";
  for (const p of c.paris) {
    const o = document.createElement("option"); o.value = p.t; o.textContent = nomPari(p.t, c); sel.appendChild(o);
  }
  pari = offre(c, prefPari) ? prefPari : offre(c, pari) ? pari : offre(c, "MULTI") ? "MULTI" : c.paris[0].t;
  sel.value = pari;
  $("cNom").textContent = `R${c.r}C${c.c} — ${c.libelle || c.hippodrome}`;
  const bits = [c.hippodrome, c.discipline && nomDisc(c.discipline).toLowerCase(), c.handicap && "handicap", c.distance && c.distance + " m",
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
  majFormule();
  calculer();                        // tout de suite, avec les cotes déjà reçues
  setTimeout(() => { remplirCourses(); rafraichir(); chargerBilan(); }, 0);   // le reste suit en arrière-plan
});

// ---------- boucle temps réel
async function rafraichir() {
  clearTimeout(timer);
  if (!courant) return;
  const demande = courant.r + "-" + courant.c + "-" + pari;
  try {
    const partie = courant.heure && courant.heure <= Date.now() ? 1 : 0;
    const co = courant, tp = pari;
    const j = await avecSecours(() => viaServeur(`/api/course?r=${co.r}&c=${co.c}&pari=${tp}&partie=${partie}`, "Données indisponibles"),
                                () => directCourse(co, tp, partie));
    if (demande !== courant.r + "-" + courant.c + "-" + pari) return;   // l'utilisateur a changé entre-temps
    j.pariProb = tp; donnees = j; $("erreur").hidden = true;
    etat(true, (direct ? "Connexion directe au PMU · " : "Cotes en direct · ") + new Date(j.maj).toLocaleTimeString("fr-FR"));
    calculer();
  } catch (e) {
    $("erreur").hidden = false; $("erreur").textContent = e.message + ". Nouvel essai dans 20 s.";
    etat(false, "Hors ligne");
  }
  // cadence : 20 s loin du départ, 10 s dans la dernière demi-heure, 5 s dans les 5 dernières minutes
  const reste = (courant.heure || 0) - Date.now(), finie = donnees && (donnees.arrivee || []).length >= 3;
  timer = setTimeout(rafraichir, finie ? 60000 : reste <= 0 ? 15000 : reste < 5 * 60000 ? 5000 : reste < 30 * 60000 ? 10000 : 20000);
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
const PERMS = {};                   // ordres possibles de k éléments, calculés une seule fois
function setP(pv, S) {              // les |S| premiers, ordre indifférent
  const k = S.length; if (k === 1) return pv[S[0]];
  const perms = PERMS[k] || (PERMS[k] = permutations([...Array(k).keys()]));
  let t = 0;
  for (let a = 0; a < perms.length; a++) {
    const pm = perms[a]; let x = 1, reste = 1;
    for (let b = 0; b < k; b++) { if (reste <= 1e-12) { x = 0; break; } const v = pv[S[pm[b]]]; x *= v / reste; reste -= v; }
    t += x;
  }
  return t;
}
// Paris « placés » : proba qu'un cheval, ou une paire, finisse dans les m premiers.
// Un seul parcours calcule toutes les paires à la fois ; le résultat est gardé tant que les probabilités ne changent pas.
const TABLES = new WeakMap();
function tableTop(pv, m, paires) {
  let parM = TABLES.get(pv); if (!parM) TABLES.set(pv, parM = {});
  const cle = m + (paires ? "p" : "");
  if (parM[cle]) return parM[cle];
  if (!paires && parM[m + "p"]) return parM[m + "p"];
  const n = pv.length, seul = new Float64Array(n), paire = paires ? new Float64Array(n * n) : null, used = new Uint8Array(n), chemin = [];
  (function rec(depth, prob, reste) {
    if (depth === m || reste <= 1e-12) return;
    for (let i = 0; i < n; i++) if (!used[i]) {
      const q = prob * pv[i] / reste; seul[i] += q;
      if (paires) for (let c = 0; c < depth; c++) { const j = chemin[c]; paire[i < j ? i * n + j : j * n + i] += q; }
      used[i] = 1; chemin[depth] = i; rec(depth + 1, q, reste - pv[i]); used[i] = 0; }
  })(0, 1, 1);
  return parM[cle] = {seul, paire, n};
}
function inTopP(pv, S, m) {         // tous les chevaux de S finissent dans les m premiers
  m = Math.min(m, pv.length - 1);
  if (S.length === 1) return tableTop(pv, m).seul[S[0]];
  if (S.length === 2) { const tb = tableTop(pv, m, true), i = Math.min(S[0], S[1]), j = Math.max(S[0], S[1]); return tb.paire[i * tb.n + j]; }
  const n = pv.length, need = new Set(S), used = new Array(n).fill(false); let tot = 0;
  (function rec(depth, prob, reste, found) {
    if (found === S.length) { tot += prob; return; }
    if (depth === m || S.length - found > m - depth || reste <= 1e-12) return;
    for (let i = 0; i < n; i++) if (!used[i]) {
      used[i] = true; rec(depth + 1, prob * pv[i] / reste, reste - pv[i], found + (need.has(i) ? 1 : 0)); used[i] = false; }
  })(0, 1, 1, 0);
  return tot;
}
function topDist(pv, m) { return Array.from(tableTop(pv, m).seul); }   // pour chaque cheval, proba de finir dans les m premiers
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
// --- Aptitudes et signaux du jour, d'après les dernières courses et les déclarations du jour
const notePlace = pl => pl === 1 ? 1 : pl === 2 ? .8 : pl === 3 ? .65 : pl === 4 ? .5 : pl === 5 ? .4 : pl >= 6 && pl <= 7 ? .2 : .05;
const moyenne = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
const cleNom = s => String(s || "").toUpperCase().replace(/[^A-Z]/g, "");
const TROT = d => /ATTELE|MONTE/i.test(d || "");
function atouts(P, ctx) {            // ctx = {distance, hippodrome, discipline, jour (ms)}
  ctx = ctx || {};
  const hip = cleNom(ctx.hippodrome), trot = TROT(ctx.discipline);
  const A = P.map(c => {
    const runs = c.perfs || [], o = {dist: null, piste: null, jockey: null, chrono: null, jours: null};
    if (!runs.length) return o;
    const memeDisc = runs.filter(r => !ctx.discipline || r.disc === ctx.discipline);
    if (ctx.distance) o.dist = moyenne(memeDisc.filter(r => r.dist && Math.abs(r.dist - ctx.distance) / ctx.distance <= 0.08).map(r => notePlace(r.pl)));
    if (hip) o.piste = moyenne(runs.filter(r => { const h = cleNom(r.h); return h && (h.startsWith(hip) || hip.startsWith(h)); }).map(r => notePlace(r.pl)));
    const j = cleNom(c.driver);
    if (j) o.jockey = moyenne(runs.filter(r => cleNom(r.j) === j).map(r => notePlace(r.pl)));
    if (trot) { const rk = memeDisc.map(r => r.rk).filter(x => x > 0); if (rk.length) o.rk = Math.min(...rk); }
    if (ctx.jour) o.jours = Math.round((ctx.jour - runs[0].d) / 86400000);
    return o;
  });
  // chrono : comparé aux autres partants (plus petit = plus rapide)
  const rks = A.map(o => o.rk).filter(Boolean);
  if (rks.length >= 4) {
    const m = moyenne(rks), sd = Math.sqrt(moyenne(rks.map(x => (x - m) * (x - m)))) || 1;
    const tri = rks.slice().sort((a, b) => a - b), seuil3 = tri[Math.min(2, tri.length - 1)];
    A.forEach(o => { if (o.rk) { o.chrono = Math.min(1, Math.max(0, .5 + .22 * (m - o.rk) / sd)); o.topChrono = o.rk <= seuil3; } });
  }
  return A.map((o, i) => {
    const c = P[i], comp = [o.dist, o.piste, o.jockey, o.chrono].filter(x => x !== null && x !== undefined);
    o.apt = comp.length ? moyenne(comp) : null;
    let s = .5; const def = c.deferre || "";
    if (/DEFERRE_ANTERIEURS_POSTERIEURS/.test(def)) s += .2; else if (/^DEFERRE/.test(def)) s += .1;
    if (c.avis === "POSITIF") s += .2; else if (c.avis === "NEGATIF") s -= .2;
    if (o.jours !== null) { if (o.jours > 120) s -= .15; else if (o.jours >= 10 && o.jours <= 45) s += .05; }
    o.signal = Math.min(1, Math.max(.05, s));
    // étiquettes : [couleur, texte court pour le tableau, texte long pour les explications]
    const t = [];
    if (o.dist !== null && o.dist >= .55) t.push(["plus", "distance ✓", "à l'aise sur la distance"]);
    if (o.piste !== null && o.piste >= .55) t.push(["plus", "piste ✓", "réussit sur cette piste"]);
    if (o.jockey !== null && o.jockey >= .55) t.push(["plus", (trot ? "driver" : "jockey") + " ✓", "réussit avec " + (trot ? "ce driver" : "ce jockey")]);
    if (o.topChrono) t.push(["plus", "chrono ✓", "bon chrono"]);
    if (/DEFERRE_ANTERIEURS_POSTERIEURS/.test(def)) t.push(["plus", "déferré des 4", "déferré des 4"]);
    else if (/^DEFERRE_ANTERIEURS/.test(def)) t.push(["", "déferré ant.", "déferré des antérieurs"]);
    else if (/^DEFERRE_POSTERIEURS/.test(def)) t.push(["", "déferré post.", "déferré des postérieurs"]);
    if (c.avis === "POSITIF") t.push(["plus", "avis +", "avis entraîneur positif"]); else if (c.avis === "NEGATIF") t.push(["moins", "avis −", "avis entraîneur négatif"]);
    if (o.jours !== null && o.jours >= 60) t.push(["moins", "rentrée", "rentrée après " + o.jours + " jours"]);
    if ((c.perfs || []).length && !(c.perfs[0].pl > 0)) t.push(["moins", "non classé", "non classé à sa dernière course"]);
    o.tags = t;
    return o;
  });
}
// Les 6 analyses d'une course, chacune ramenée à une répartition de chances entre les chevaux.
// Elles ne dépendent pas du dosage : on les calcule une fois, puis on les mélange selon les poids.
function composantes(P, srcs, ctx) {
  const marche = marchePMU(P, srcs);
  const mouv = normaliser(P.map((c, i) => {
    const f = c.coteMatin && c.coteDirect ? Math.min(2, Math.max(.5, c.coteMatin / c.coteDirect)) : 1;
    return marche[i] * f; }));
  const fr = P.map(c => scoreMusique(c.musique));
  const ok = fr.filter(x => x !== null);
  const moyF = ok.length ? ok.reduce((a, b) => a + b, 0) / ok.length : .3;
  const forme = normaliser(fr.map(x => Math.pow(x === null ? moyF : x, 2)));
  const regul = normaliser(P.map(c => { const r = (c.places + .9) / ((c.courses || 0) + 3); return r * r; }));
  const at = atouts(P, ctx);
  const moyA = moyenne(at.map(o => o.apt).filter(x => x !== null));
  const aucuneApt = moyA === null;                // pas de courses passées connues : l'analyse est mise de côté
  const apt = normaliser(at.map(o => Math.pow(o.apt === null ? (moyA || .3) : o.apt, 2)));
  const sig = normaliser(at.map(o => o.signal * o.signal));
  return {marche, mouv, forme, regul, apt, sig, aucuneApt, fr, at};
}
function doser(c, w) {                // probabilité de gagner de chaque cheval pour un dosage donné
  const wA = c.aucuneApt ? 0 : (w.A || 0), wS = w.S || 0;
  let W = w.M + w.T + w.F + w.R + wA + wS, M = w.M;
  if (!W) { W = 1; M = 1; }           // tous les poids à zéro : on suit les cotes
  const n = c.marche.length, p = new Array(n); let s = 0;
  for (let i = 0; i < n; i++) { p[i] = (M * c.marche[i] + w.T * c.mouv[i] + w.F * c.forme[i] + w.R * c.regul[i] + wA * c.apt[i] + wS * c.sig[i]) / W; s += p[i]; }
  for (let i = 0; i < n; i++) p[i] /= s || 1;
  return {p, marche: c.marche, fr: c.fr, at: c.at};
}
function modele(P, w, srcs, ctx) { return doser(composantes(P, srcs, ctx), w); }
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
// Multi : le ticket principal, puis les tickets qui couvrent le mieux ce que les précédents laissent de côté
function ticketsMulti(p, k, T1, N) {
  const n = p.length, parP = (a, b) => p[b] - p[a];
  if (N <= 1) return [{T: T1.slice().sort(parP), plus: couvertureMulti(p, T1)}];
  const cand = [...new Set(p.map((_, i) => i).sort(parP).slice(0, Math.min(11, n)).concat(T1))];
  const bit = new Map(cand.map((h, i) => [h, 1 << i])), masque = e => { let m = 0; for (const h of e) m |= bit.get(h); return m; };
  // chaque groupe de 4 et chaque ticket possible devient un nombre : « le ticket contient le groupe » se teste en une opération
  const Q = combinaisons(cand, 4).map(e => ({m: masque(e), p: setP(p, [...e]), fait: false}));
  const ensembles = combinaisons(cand, Math.min(k, n)).map(e => ({m: masque(e), e, pris: false}));
  const out = [];
  const ajouter = (m, e) => { let g = 0; for (const x of Q) if (!x.fait && (x.m & m) === x.m) { g += x.p; x.fait = true; }
    out.push({T: [...e].sort(parP), plus: g}); };
  const m1 = masque(T1); for (const x of ensembles) if (x.m === m1) x.pris = true;
  ajouter(m1, T1);
  while (out.length < N) {
    let best = null, g = -1;
    for (const x of ensembles) { if (x.pris) continue;
      let v = 0; for (const q of Q) if (!q.fait && (q.m & x.m) === q.m) v += q.p;
      if (v > g) { g = v; best = x; } }
    if (!best) break; best.pris = true; ajouter(best.m, best.e);
  }
  return out;
}
function auMoinsUn(pv, tickets, m) {    // paris « placés » : proba qu'au moins un des tickets soit en entier dans les m premiers
  const n = pv.length, used = new Array(n).fill(false), pos = []; let tot = 0;
  (function rec(depth, prob, reste) {
    if (depth === m || reste <= 1e-12) { if (tickets.some(T => T.every(i => used[i]))) tot += prob; return; }
    for (let i = 0; i < n; i++) if (!used[i]) { used[i] = true; rec(depth + 1, prob * pv[i] / reste, reste - pv[i]); used[i] = false; }
  })(0, 1, 1);
  return tot;
}
// Liste de N tickets : le premier suit le style choisi, les suivants sont les combinaisons les plus probables restantes.
// Renvoie [{T: indices, plus: chance ajoutée par ce ticket}] et la chance totale d'avoir au moins un ticket gagnant.
function construireTickets(t, k, mod, sty, N, sansTotal) {
  const p = mod.p, T1 = choisirTicket(mod, k, sty), kind = PARIS[t].kind;
  if (kind === "multi") { const L = ticketsMulti(p, k, T1, N); return {liste: L, total: L.reduce((s, x) => s + x.plus, 0)}; }
  const liste = [{T: T1, plus: probasTicket(t, p, T1).win}];
  if (N > 1) {
    const ordre = kind === "seq" && k > 1;
    const cle = T => (ordre ? T : T.slice().sort((a, b) => a - b)).join(",");
    const vivier = [...new Set(p.map((_, i) => i).sort((a, b) => p[b] - p[a]).slice(0, Math.min(p.length, k + 3)).concat(T1))];
    const cands = [];
    for (const s of combinaisons(vivier, k)) {
      const base = [...s].sort((a, b) => p[b] - p[a]);
      for (const T of (ordre ? permutations(base) : [base])) if (cle(T) !== cle(T1)) cands.push({T, plus: probasTicket(t, p, T).win});
    }
    cands.sort((a, b) => b.plus - a.plus);
    liste.push(...cands.slice(0, N - 1));
  }
  // les tickets « ordre exact » ou « les k premiers » ne peuvent pas gagner en même temps : les chances s'additionnent
  const total = sansTotal ? 0 : kind === "in" && liste.length > 1 ? auMoinsUn(p, liste.map(x => x.T), placesPlace(t, p.length)) : liste.reduce((s, x) => s + x.plus, 0);
  return {liste, total};
}

// ---------- rapports : vrais rapports probables du PMU, sinon estimation calée sur les jours passés
// En pari mutuel, rapport ≈ K ÷ (proba que les parieurs donnent à cette arrivée). K se mesure sur les vrais
// rapports définitifs des 30 derniers jours : rapport réel × proba du marché, valeur médiane.
const Kcal = {};                    // pari -> {a: K principal, o: K « ordre »}
function mediane(a) { if (a.length < 5) return null; a.sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; }
const estDesordre = l => /d[ée]sordre/i.test(l);
const estOrdre = (t, l) => !estDesordre(l) && (/ordre/i.test(l) || ORDONNES.has(t));
function calerK(t) {                // chaque jour n'est calculé qu'une fois
  const A = [], O = [], kind = PARIS[t].kind, c = bilanCache[t]; if (!c || !c.jours) return;
  const memo = c.kjours || (c.kjours = new Map());
  const vues = new Set();
  for (const [date, j] of c.jours) {
    if (!j) continue;
    const cleC = j.date + "-" + j.r + "-" + j.c; if (vues.has(cleC)) continue; vues.add(cleC);
    let r = memo.get(date);
    if (!r) {
      r = {A: [], O: []};
      const P = j.partants.filter(x => x.partant), mk = marchePMU(P, {});
      for (const rap of j.rapports[t] || []) {
        const lib = rap.l.toLowerCase(); if (lib.includes("bonus")) continue;
        const idx = rap.c.map(nm => P.findIndex(x => x.num === nm)); if (idx.some(x => x < 0)) continue;
        if (kind === "multi") { if (/en 4$/.test(lib)) r.A.push(rap.d * setP(mk, idx)); }
        else if (kind === "od") { if (estDesordre(lib)) r.A.push(rap.d * setP(mk, idx)); else if (estOrdre(t, lib)) r.O.push(rap.d * seqP(mk, idx)); }
        else if (kind === "seq") r.A.push(rap.d * seqP(mk, idx));
        else if (kind === "set") r.A.push(rap.d * setP(mk, idx));
        else r.A.push(rap.d * inTopP(mk, idx, placesPlace(t, P.length)));
      }
      memo.set(date, r);
    }
    for (const x of r.A) A.push(x); for (const x of r.O) O.push(x);
  }
  Kcal[t] = {a: mediane(A), o: mediane(O)};
}
let probMap = {};
function chargerProbables() {
  probMap = {};
  if (!donnees || donnees.pariProb !== pari) return;       // rapports probables d'un autre pari : on attend les bons
  for (const [nums, d, mn, mx] of donnees.probables || []) {
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
  try { majRepere(P); } catch (e) { $("repere").hidden = true; }
  const t = pari, info = PARIS[t], k = tailleTicket(t), mise = maMise();
  const base = ((courant.paris || []).find(p => p.t === t) || {}).base;
  const w = poids(); for (const kk in w) $("v" + kk).textContent = w[kk];
  $("tRegle").textContent = "Pour gagner : " + info.regle + " Style " + STYLES[style].nom + " : " + STYLES[style].txt;
  if (n < k + 1) { $("tNums").textContent = "Pas assez de partants pour ce pari."; return; }
  chargerProbables();
  const jour0 = new Date(); jour0.setHours(0, 0, 0, 0);
  const mod = modele(P, w, sources, {distance: courant.distance, hippodrome: courant.hippodrome, discipline: courant.discipline, jour: +jour0}), p = mod.p;
  const ordonne = info.kind === "seq" && k > 1 || info.kind === "od";
  const N = nbTickets(), tk = construireTickets(t, k, mod, style, N), nb = tk.liste.length, totalMise = nb * mise;
  const T = tk.liste[0].T, selSet = new Set(T);
  $("tTitre").textContent = (nb > 1 ? `${nb} tickets conseillés` : "Ticket conseillé") + ` — ${nomPari(t, courant)}${t === "MULTI" ? " en " + k : ""}, ` +
    (nb > 1 ? `${nb} × ${euro(mise)} = ${euro(totalMise)}` : `mise ${euro(mise)}`);
  // évaluation de chaque ticket : rapport pour 1 € et part de la mise rendue en moyenne
  let ratioTot = 0, ratioOk = true, source = null, gPond = 0, pPond = 0, ordreTot = 0;
  const lignesT = tk.liste.map((x, r) => {
    let e, gain = "";
    if (t === "MULTI") { const m = esperanceMulti(mod, x.T, k);
      e = {ratio: m ? m.ratio : null, source: m ? "estime" : null, win: couvertureMulti(p, x.T), r: m ? m.siGagne : null, mini: m && m.mini, maxi: m && m.maxi};
      if (m) gain = `≈ ${euro(m.siGagne * mise)} (de ${euro(m.mini * mise)} à ${euro(m.maxi * mise)})`;
    } else { e = evaluer(t, P, mod, x.T);
      if (info.kind === "od") { if (e.rDes && e.rOrd) { gain = `≈ ${euro(e.rDes * mise)} dans le désordre · ≈ ${euro(e.rOrd * mise)} dans l'ordre`; e.r = e.rDes; } ordreTot += e.ordre || 0; }
      else if (e.r) gain = e.rMin ? `${euro(e.rMin * mise)} à ${euro(e.rMax * mise)}` : "≈ " + euro(e.r * mise);
    }
    if (e.ratio === null || !isFinite(e.ratio)) ratioOk = false; else { ratioTot += e.ratio; source = source === "estime" ? source : e.source; }
    if (e.r) { gPond += e.win * e.r; pPond += e.win; }
    x.gainTxt = gain;
    const chips = x.T.map((i, q) => `<span class="n">${P[i].num}${ordonne ? `<i>${q + 1}${q ? "e" : "er"}</i>` : ""}</span>`).join("");
    if (nb === 1) return chips;
    return `<div class="tk"><span class="lab">Ticket ${r + 1}<b>${r ? "+" : ""}${pct(x.plus)} de chances${gain ? " · gain " + gain : ""}</b></span>${chips}</div>`;
  });
  $("tNums").innerHTML = lignesT.join("");
  $("tNums").className = "nums" + (ordonne ? " ord" : "") + (nb > 1 ? " plusieurs" : "");
  $("tProba").textContent = pct(tk.total);
  $("tProbaLab").textContent = (nb > 1 ? "chance qu'au moins un ticket passe" : "chance de gagner") + (info.kind === "od" ? ` (dont ${pct(ordreTot)} dans l'ordre exact)` : "");
  $("tChance").textContent = sur(tk.total);
  if (nb === 1) {
    $("tRapport").textContent = tk.liste[0].gainTxt ? tk.liste[0].gainTxt.split(" (")[0].split(" dans le")[0] : "–";
    $("tRapportLab").textContent = !tk.liste[0].gainTxt ? "gain si le ticket passe"
      : t === "MULTI" ? "gain estimé si le ticket passe " + "(" + tk.liste[0].gainTxt.split(" (")[1]
      : info.kind === "od" ? "gain estimé dans le désordre · " + tk.liste[0].gainTxt.split(" · ")[1]
      : source === "pmu" ? "gain si le ticket passe (rapport probable PMU)" : "gain estimé si le ticket passe";
  } else {
    $("tRapport").textContent = pPond ? "≈ " + euro(gPond / pPond * mise) : "–";
    $("tRapportLab").textContent = `gain moyen quand un ticket passe, pour ${euro(totalMise)} misés en tout`;
  }
  verdict({ratio: ratioOk ? ratioTot / nb : null, source}, totalMise);
  $("copier").dataset.txt = tk.liste.map(x => x.T.map(i => P[i].num).join(" - ")).join("\n");
  $("copier").textContent = nb > 1 ? "Copier les tickets" : "Copier les numéros";
  $("tNote").textContent = (base ? `Mise de base PMU pour ce pari : ${euro(base)}. ` : "") +
    (nb > 1 ? "Le ticket 1 suit ton style ; les suivants sont les combinaisons les plus probables qu'il ne couvre pas. Chaque ticket se joue séparément. " : "") +
    (info.kind === "od" ? "Les bonus ne sont pas comptés dans le verdict. " : "") +
    "Le verdict ne vaut que si le test ci-dessous montre que l'appli fait mieux que les favoris.";

  // Outsiders à surveiller : hors des favoris des parieurs, mais mieux notés par l'appli ou cote en nette baisse
  const rang = p.map((_, i) => i).sort((x, y) => mod.marche[y] - mod.marche[x]);
  const seuil = Math.max(k, 4), outs = [];
  rang.slice(seuil).forEach(i => {
    const c = P[i], val = p[i] / Math.max(mod.marche[i], 1e-6), raisons = [];
    const baisse = c.coteMatin && c.coteDirect ? 1 - c.coteDirect / c.coteMatin : 0;
    if (mod.fr[i] !== null && mod.fr[i] >= 0.55) raisons.push(`bonne forme récente (${Math.round(mod.fr[i] * 100)}/100)`);
    if (c.courses >= 5 && c.places / c.courses >= 0.45) raisons.push(`régulier (${Math.round(100 * c.places / c.courses)} % de places)`);
    if (baisse >= 0.12) raisons.push(`cote en baisse de ${Math.round(baisse * 100)} %`);
    for (const x of mod.at[i].tags) if (x[0] === "plus") raisons.push(x[2]);
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
      <td class="l"><div class="cheval">${esc(c.nom)}</div><div class="sub">${esc(c.driver || "")} · ${esc(c.musique || "")}</div>${mod.at[i].tags.length ? `<div class="tags">${mod.at[i].tags.map(x => `<span class="tag ${x[0]}" title="${x[2]}">${x[1]}</span>`).join("")}</div>` : ""}</td>
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
    const g = raps ? tk.liste.reduce((s, x) => s + gainReel(t, k, x.T.map(i => P[i].num), raps), 0) : null;
    $("resultat").hidden = false;
    $("resultat").textContent = `Arrivée : ${arr.join(" - ")}. ` + (g === null ? "Rapports de ce pari pas encore publiés."
      : g > 0 ? `${nb > 1 ? "Tes tickets conseillés étaient GAGNANTS" : "Le ticket conseillé était GAGNANT"} : ${euro(g * mise)} pour ${euro(totalMise)} misés.`
      : nb > 1 ? "Aucun des tickets conseillés n'est passé." : "Le ticket conseillé était perdant.");
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
  el.innerHTML = `<b>${titre}</b><span>Gain moyen attendu : ${signe((e.ratio - 1) * mise)} pour ${euro(mise)} misés (${Math.round(e.ratio * 100)} % de la mise rendue en moyenne) · ${e.source === "pmu" ? "d'après le rapport probable du PMU" : "rapport estimé d'après les courses passées"}</span>`;
}

// ---------- bilan des jours passés
const bilanCache = {}; let bilanTimer = null, bilanAvance = "";
const periode = () => +$("periode").value;
try { const pe = localStorage.getItem("ml_periode"); if (pe) $("periode").value = pe; } catch (e) {}

// Les courses passées sont gardées dans le téléphone : chaque jour n'est demandé au PMU qu'une seule fois.
const memoire = {
  db: null,
  ouvrir() {
    if (this.db !== null) return this.db;
    this.db = new Promise(res => {
      try {
        const rq = indexedDB.open("multilive-v2", 1);
        rq.onupgradeneeded = () => rq.result.createObjectStore("jours");
        rq.onsuccess = () => res(rq.result); rq.onerror = () => res(null); rq.onblocked = () => res(null);
      } catch (e) { res(null); }
    });
    return this.db;
  },
  async lire(t) {                       // -> Map(date -> course ou null)
    const db = await this.ouvrir(), out = new Map(); if (!db) return out;
    return new Promise(res => {
      try {
        const rq = db.transaction("jours").objectStore("jours").openCursor(IDBKeyRange.bound(t + "|", t + "|￿"));
        rq.onsuccess = () => { const c = rq.result; if (!c) return res(out); out.set(String(c.key).split("|")[1], c.value); c.continue(); };
        rq.onerror = () => res(out);
      } catch (e) { res(out); }
    });
  },
  async ecrire(t, resultats, garder) {  // enregistre les nouveaux jours, efface ceux qui sortent de la période la plus longue
    const db = await this.ouvrir(); if (!db) return;
    try {
      const st = db.transaction("jours", "readwrite").objectStore("jours");
      for (const [d, v] of Object.entries(resultats)) st.put(v, t + "|" + d);
      st.openCursor(IDBKeyRange.bound(t + "|", t + "|￿")).onsuccess = e => { const c = e.target.result; if (!c) return;
        if (!garder.has(String(c.key).split("|")[1])) c.delete(); c.continue(); };
    } catch (e) {}
  },
};
const jjmmaaaa = d => String(d.getDate()).padStart(2, "0") + String(d.getMonth() + 1).padStart(2, "0") + d.getFullYear();
// Le test peut rejouer 1 course par jour (la plus proche de 13h55) ou 4, réparties dans la journée.
const HEURES_4 = ["11:30", HEURE_CIBLE, "15:45", "17:45"];
try { const pj = localStorage.getItem("ml_parjour"); if (pj) $("parjour").value = pj; } catch (e) {}
const heuresTest = () => $("parjour").value === "4" ? HEURES_4 : [HEURE_CIBLE];
const idJour = (d, h) => h === HEURE_CIBLE ? d : d + "@" + h;        // identifiant d'une course testée (jour + heure visée)
function idsPasses(n, heures) { const out = []; for (const d of datesPassees(n)) for (const h of heures) out.push({id: idJour(d, h), d, h}); return out; }
// Courses chargées pour la période, sans doublon : deux heures visées peuvent tomber sur la même course
function coursesTest(cache, n, heures) {
  const out = [], vues = new Set();
  for (const x of idsPasses(n, heures)) { const j = cache.jours.get(x.id); if (!j) continue;
    const cle = j.date + "-" + j.r + "-" + j.c; if (vues.has(cle)) continue; vues.add(cle); out.push({id: x.id, d: x.d, j}); }
  return out;
}
function datesPassees(n) { const out = []; for (let i = 1; i <= n; i++) { const d = new Date(); d.setDate(d.getDate() - i); out.push(jjmmaaaa(d)); } return out; }
function coursesChargees(t) { const c = bilanCache[t]; return c ? [...c.jours.values()].filter(Boolean) : []; }

// Chargement par tranches de 10 jours, résultats affichés au fur et à mesure
async function chargerBilan() {
  const t = pari;
  $("bTitre").textContent = `Test sur les courses passées — ${PARIS[t].nom}`;
  $("optimRes").hidden = true;       // un résultat de recherche ne vaut que pour le pari et la période où il a été calculé
  const c = bilanCache[t] || (bilanCache[t] = {jours: null, enCours: false});
  const afficher = () => { if (t === pari) { calerK(t); calculer(); calculerBilan(); } };
  if (c.enCours) return;
  c.enCours = true;
  try {
    if (!c.jours) { $("bCartes").innerHTML = ""; $("bCorps").innerHTML = ""; $("bEtat").textContent = "Chargement…"; c.jours = await memoire.lire(t); }
    let arret = ""; const essayes = new Set();
    const garder = new Set(idsPasses(190, HEURES_4).map(x => x.id));
    await completerTypes(t, c, garder, afficher);      // d'abord : rapide (une demande par jour), et utile tout de suite
    while (t === pari) {
      const voulues = idsPasses(periode(), heuresTest()), manque = voulues.filter(x => !c.jours.has(x.id) && !essayes.has(x.id));
      if (!manque.length) break;
      bilanAvance = `Chargement des courses passées : ${voulues.length - manque.length} sur ${voulues.length}… `;
      afficher();
      const h = manque[0].h, lot = manque.filter(x => x.h === h).slice(0, 10);
      const r = await fetch(`/api/bilan?dates=${lot.map(x => x.d).join(",")}&heure=${h}&pari=${t}`); const j = await r.json();
      if (!r.ok) throw new Error(j.erreur || "Test indisponible");
      const nouveaux = {};
      for (const x of lot) { essayes.add(x.id); if (x.d in j.resultats) { c.jours.set(x.id, j.resultats[x.d]); nouveaux[x.id] = j.resultats[x.d]; } }
      memoire.ecrire(t, nouveaux, garder);
      if (j.sature) { arret = "Le PMU limite les demandes pour le moment : le chargement reprendra à la prochaine ouverture de l'appli. "; break; }
    }
    if (!arret) await completerTypes(t, c, garder, afficher);
    bilanAvance = arret; afficher();
  } catch (e) {
    bilanAvance = "Chargement interrompu (" + e.message + "). Il reprendra à la prochaine ouverture. "; afficher();
  } finally { c.enCours = false; }
}
// Les courses gardées sur le téléphone avant l'arrivée du choix « Handicaps » ne disent pas si elles en étaient un :
// on le demande au serveur, une seule fois (une demande au PMU par jour, pas par course), puis c'est gardé.
async function completerTypes(t, c, garder, afficher) {
  const essayes = new Set(), total = [...c.jours.values()].filter(j => j && j.handicap === undefined).length;
  try {
    while (t === pari) {
      const manque = [...c.jours].filter(([, j]) => j && j.handicap === undefined && !essayes.has(j.date));
      if (!manque.length) break;
      const dates = [...new Set(manque.map(([, j]) => j.date))].slice(0, 15);
      dates.forEach(d => essayes.add(d));
      bilanAvance = `Lecture du type des courses (handicap ou non) : encore ${manque.length} sur ${total}… `; afficher();
      const r = await fetch(`/api/types?dates=${dates.join(",")}`), j = await r.json(); if (!r.ok) break;
      const maj = {};
      for (const [id, x] of manque) { const h = (j.resultats[x.date] || {})[x.r + "-" + x.c]; if (h !== undefined) { x.handicap = !!h; maj[id] = x; } }
      memoire.ecrire(t, maj, garder);
      if (j.sature) break;
    }
  } catch (e) {}
}
// Chaque jour passé n'est rejoué qu'une fois par réglage (pari, formule, nombre de tickets, poids des analyses) :
// le résultat est gardé pour 1 € de mise, donc changer la mise ou le style ne recalcule rien.
const bilanMemo = {cle: "", jours: new Map()};
let bilanTour = 0;
const ctxJour = j => ({distance: j.distance, hippodrome: j.hippodrome, discipline: j.discipline,
                       jour: +new Date(+j.date.slice(4), +j.date.slice(2, 4) - 1, +j.date.slice(0, 2))});
// Verdict d'un ticket passé, rangé de façon à pouvoir appliquer plus tard le calage K :
// part de la mise rendue = c + K.a × a + K.o × o
function coefVerdict(t, k, P, mod, T) {
  const kind = PARIS[t].kind;
  if (kind === "multi") { let a = 0; for (const q of quartets(T)) a += setP(mod.p, q) / (NB_GROUPES[k] * Math.max(setP(mod.marche, q), 1e-9)); return {c: 0, a, o: 0}; }
  const pr = probasTicket(t, mod.p, T), pm = probasTicket(t, mod.marche, T);
  if (kind === "od") return {c: 0, a: pm.win > 0 ? (pr.win - pr.ordre) / pm.win : 0, o: pm.ordre > 0 ? pr.ordre / pm.ordre : 0};
  if (t === "SIMPLE_GAGNANT" && P[T[0]].coteDirect) return {c: pr.win * P[T[0]].coteDirect, a: 0, o: 0};
  return {c: 0, a: pm.win > 0 ? pr.win / pm.win : 0, o: 0};
}
function partRendue(t, v) {           // null tant que le calage n'est pas connu
  const K = Kcal[t] || {};
  if ((v.a && !K.a) || (v.o && !K.o)) return null;
  return v.c + (K.a || 0) * v.a + (K.o || 0) * v.o;
}
function rejouerJour(t, k, N, w, j, comp) {
  const P = j.partants.filter(c => c.partant), raps = j.rapports[t], multi = t === "MULTI";
  if (P.length < k + 1 || !raps || !raps.length) return null;
  if (multi && !raps.some(r => new RegExp("en " + k + "$").test(r.l.toLowerCase()))) return null;
  const mod = doser(comp || composantes(P, {}, ctxJour(j)), w);
  const jouer = (m, sty) => { const L = construireTickets(t, k, m, sty, N, true).liste, v = {c: 0, a: 0, o: 0};
    for (const x of L) { const e = coefVerdict(t, k, P, m, x.T); v.c += e.c / L.length; v.a += e.a / L.length; v.o += e.o / L.length; }
    return {g: L.reduce((s, x) => s + gainReel(t, k, x.T.map(i => P[i].num), raps), 0), nb: L.length, v}; };
  const res = {};
  for (const c of ["prudent", "equilibre", "outsiders"]) res[c] = jouer(mod, c);
  res.fav = jouer({p: mod.marche, marche: mod.marche}, "prudent");       // les favoris des parieurs, sans analyse
  res.type = {disc: nomDisc(j.discipline),
              nb: P.length <= 12 ? "12 partants ou moins" : P.length <= 15 ? "13 à 15 partants" : "16 partants ou plus",
              fav: (m => m >= .3 ? "Favori net (30 % ou plus)" : m >= .2 ? "Favori moyen (20 à 30 %)" : "Course ouverte (moins de 20 %)")(Math.max(...mod.marche))};
  return res;
}
async function calculerBilan() {
  const t = pari, cache = bilanCache[t]; if (!cache || !cache.jours) return;
  const tour = ++bilanTour;
  const w = poids(), k = tailleTicket(t), N = nbTickets(), multi = t === "MULTI";
  const cle = [t, k, N, CURSEURS.map(c => $(c).value).join("-")].join("|");
  if (bilanMemo.cle !== cle) { bilanMemo.cle = cle; bilanMemo.jours = new Map(); }
  const liste = coursesTest(cache, periode(), heuresTest()).filter(x => typeOk(x.j));
  const aFaire = liste.filter(x => !bilanMemo.jours.has(x.id));
  // calcul par tranches courtes, en rendant la main à l'écran entre deux
  let debut = performance.now(), faits = 0;
  for (const x of aFaire) {
    bilanMemo.jours.set(x.id, rejouerJour(t, k, N, w, x.j));
    if (++faits % 3 === 0 && performance.now() - debut > 25) {
      if (faits % 30 === 0 || faits === 3) $("bEtat").textContent = bilanAvance + `Calcul du test : ${faits} courses sur ${aFaire.length}…`;
      await new Promise(r => setTimeout(r, 0));
      if (tour !== bilanTour || bilanMemo.cle !== cle) return;        // un réglage a changé entre-temps : ce calcul est abandonné
      debut = performance.now();
    }
  }
  if (tour !== bilanTour) return;
  afficherBilan(t, k, N, multi, liste);
}
function afficherBilan(t, k, N, multi, liste) {
  const mise = maMise(), cles = ["prudent", "equilibre", "outsiders", "fav"], st = {}, filtre = $("joues").value, tc = $("typeC").value;
  const vide = () => ({n: 0, g: 0, mise: 0, ret: 0});
  for (const c of cles) st[c] = vide();
  const COMP = [["Simple placé, le favori", "SIMPLE_PLACE", 1, 0], ["Simple placé, le 2e favori", "SIMPLE_PLACE", 1, 1], ["Couplé placé, les 2 favoris", "COUPLE_PLACE", 2, 0],
                ["2sur4, les 2 favoris", "DEUX_SUR_QUATRE", 2, 0], ["Simple gagnant, le favori", "SIMPLE_GAGNANT", 1, 0], ["Trio, les 3 favoris", "TRIO", 3, 0],
                ["Multi en 6, les 6 favoris", "MULTI", 6, 0], ["Multi en 7, les 7 favoris", "MULTI", 7, 0]].map(x => ({nom: x[0], t: x[1], k: x[2], d: x[3], n: 0, g: 0, ret: 0}));
  const types = {}, lignes = [], cell = g => `<td class="${g ? "tr-down" : ""}">${g ? "✓ +" + euro(g) : "✗"}</td>`;
  let ecartes = 0, sansVerdict = 0;
  for (const {id, d, j} of liste) {
    const res = bilanMemo.jours.get(id); if (!res) continue;
    // le filtre suit le verdict de ton style actuel ; les quatre cartes portent sur les mêmes jours, pour comparer à égalité
    const part = partRendue(t, res[style].v);
    if (filtre !== "tous") { if (part === null) { sansVerdict++; continue; } if (part < (filtre === "favorable" ? 1 : 0.85)) { ecartes++; continue; } }
    for (const c of cles) { const g = res[c].g * mise; st[c].n++; st[c].mise += mise * res[c].nb; if (g) { st[c].g++; st[c].ret += g; } }
    const favs = j.partants.filter(c => c.partant && c.coteDirect).sort((a, b) => a.coteDirect - b.coteDirect).map(c => c.num);
    for (const x of COMP) { const raps = j.rapports[x.t];
      if (!raps || !raps.length || favs.length <= x.k + x.d || (x.t === "MULTI" && !raps.some(r => new RegExp("en " + x.k + "$").test(r.l.toLowerCase())))) continue;
      const g = gainReel(x.t, x.k, favs.slice(x.d, x.d + x.k), raps); x.n++; x.ret += g; if (g) x.g++; }
    const verd = part === null ? "Verdict inconnu" : part >= 1 ? "Verdict favorable" : part >= .85 ? "Verdict limite" : "Verdict défavorable";
    for (const [grp, nom] of [["Discipline", res.type.disc], ["Handicap", j.handicap === true ? "Handicap" : j.handicap === false ? "Sans handicap" : "Non renseigné"], ["Nombre de partants", res.type.nb], ["Force du favori", res.type.fav], ["Verdict de l'appli", verd]]) {
      const x = (types[grp] = types[grp] || {})[nom] = (types[grp][nom] || {a: vide(), f: vide()});
      for (const [o, c] of [[x.a, style], [x.f, "fav"]]) { o.n++; o.mise += res[c].nb; o.ret += res[c].g; }
    }
    lignes.push(`<tr><td class="l">${d.slice(0, 2)}/${d.slice(2, 4)}</td><td class="l">${esc(j.hippodrome)} R${j.r}C${j.c}</td>
      <td class="l">${j.arrivee.join("-")}</td>${cles.map(c => cell(res[c].g * mise)).join("")}</tr>`);
  }
  const carte = (titre, s, sous, actif) => `<div class="bcard${actif ? " actif" : ""}"><h3>${titre}</h3><div class="sub">${sous}</div>
      <div class="bnet ${s.ret - s.mise >= 0 ? "tr-down" : "tr-up"}">${signe(s.ret - s.mise)}</div>
      <div class="sub"><b>${s.mise ? Math.round(100 * s.ret / s.mise) : 0} % de la mise récupérée</b> · ${s.g} course${s.g > 1 ? "s" : ""} gagnante${s.g > 1 ? "s" : ""} sur ${s.n} · misé ${euro(s.mise)} · récupéré ${euro(s.ret)}</div></div>`;
  $("bCartes").innerHTML = st.fav.n ? ["prudent", "equilibre", "outsiders"].map(c =>
      carte("Style " + STYLES[c].nom, st[c], c === style ? "ton style actuel" : (N > 1 ? "tickets de l'appli" : "ticket de l'appli"), c === style)).join("") +
    carte(k > 1 ? `Les ${k} favoris` : "Le favori", st.fav, k > 1 ? "les plus petites cotes, sans analyse" : "la plus petite cote, sans analyse") : "";
  // tableau par type de course
  const pc = o => o.mise ? Math.round(100 * o.ret / o.mise) + " %" : "–";
  $("bTypes").innerHTML = Object.entries(types).map(([grp, noms]) => `<tr class="grp"><td class="l" colspan="4">${grp}</td></tr>` +
    Object.entries(noms).sort((a, b) => b[1].a.n - a[1].a.n).map(([nom, x]) => { const mieux = x.a.mise && x.a.ret / x.a.mise > x.f.ret / x.f.mise;
      return `<tr><td class="l">${nom}</td><td>${x.a.n}</td><td class="${mieux ? "tr-down" : ""}"><b>${pc(x.a)}</b></td><td>${pc(x.f)}</td></tr>`; }).join("")).join("");
  $("bParis").innerHTML = COMP.filter(x => x.n >= 5).sort((a, b) => b.ret / b.n - a.ret / a.n).map(x =>
    `<tr><td class="l">${x.nom}</td><td>${x.n}</td><td>${Math.round(100 * x.g / x.n)} %</td><td class="${x.ret >= x.n ? "tr-down" : ""}"><b>${Math.round(100 * x.ret / x.n)} €</b></td></tr>`).join("");
  // le détail jour par jour n'est rempli que s'il est ouvert (plusieurs centaines de lignes sur 6 mois)
  const det = $("bCorps").closest("details");
  det._lignes = lignes; if (det.open) $("bCorps").innerHTML = lignes.join(""); else $("bCorps").innerHTML = "";
  const tri = filtre === "tous" ? "" : ` Courses écartées par le filtre : ${ecartes}${sansVerdict ? `, plus ${sansVerdict} sans verdict` : ""}.`;
  const quelles = heuresTest().length > 1 ? "jusqu'à 4 courses par jour proposant ce pari, du matin au soir" : `chaque jour, la course proposant ce pari la plus proche de ${HEURE_CIBLE.replace(":", "h")}`;
  $("bEtat").textContent = bilanAvance + (st.fav.n
    ? `${st.fav.n} courses jouées, ${PARIS[t].nom}${multi ? " en " + k : ""} ${N > 1 ? ", " + N + " tickets" : ""} à ${euro(mise)} ${N > 1 ? "chacun" : "par course"} : ${quelles}${tc === "tous" ? "" : ", en gardant seulement « " + TYPES_C[tc] + " »"}.${tri} Gains calculés avec les vrais rapports du PMU et les cotes finales : en vrai, quelques minutes avant le départ, c'est un peu moins bon.`
    : (bilanAvance ? "" : tc !== "tous" ? `Aucune course « ${TYPES_C[tc]} » chargée pour ce pari sur la période. Essaie « 6 derniers mois » et « 4, du matin au soir ».` : filtre === "tous" ? "Aucune course exploitable pour ce pari sur la période." : `Aucune course ne passe ce filtre sur la période.${tri}`));
}
$("bCorps").closest("details").addEventListener("toggle", e => { if (e.target.open && e.target._lignes) $("bCorps").innerHTML = e.target._lignes.join(""); });

// ---------- recherche automatique du meilleur dosage
// On règle les poids sur les courses les plus anciennes, puis on vérifie sur les plus récentes, jamais vues pendant le réglage.
// Le réglage cherche le dosage qui prévoit le mieux les 3 premiers (vraisemblance), pas celui qui a rapporté le plus :
// viser le gain sur si peu de courses reviendrait à régler l'appli sur quelques gros rapports dus au hasard.
const NOMS_POIDS = {M: "Cotes", T: "Mouvement", F: "Forme", R: "Régularité", A: "Aptitudes", S: "Signaux"};
const COTES_SEULES = {M: 100, T: 0, F: 0, R: 0, A: 0, S: 0};
let reglageTrouve = null;
function prevision(jeu, w) {          // moyenne du log de la proba donnée à l'arrivée réelle (3 premiers) : plus c'est haut, mieux c'est
  let s = 0;
  for (const x of jeu) s += Math.log(Math.max(seqP(doser(x.comp, w).p, x.arr), 1e-12));
  return s / jeu.length;
}
async function chercherReglage() {
  const t = pari, cache = bilanCache[t], box = $("optimRes"); box.hidden = false;
  const k = tailleTicket(t), N = nbTickets();
  const jeu = [];
  for (const {j} of (cache && cache.jours ? coursesTest(cache, periode(), heuresTest()).filter(x => typeOk(x.j)) : []).reverse()) {          // du plus ancien au plus récent
    const P = j.partants.filter(c => c.partant), arr = j.arrivee.slice(0, 3).map(nm => P.findIndex(c => c.num === nm));
    if (P.length < Math.max(k + 1, 5) || arr.length < 3 || arr.some(i => i < 0)) continue;
    jeu.push({j, arr, comp: composantes(P, {}, ctxJour(j))});
  }
  if (jeu.length < 40) { box.innerHTML = `<b class="t">Pas assez de courses</b><span class="sub">Il en faut au moins 40 pour régler puis vérifier (${jeu.length} chargées${$("typeC").value === "tous" ? "" : " dans « " + TYPES_C[$("typeC").value] + " »"}). Choisis une période plus longue ou « 4 courses par jour », et attends la fin du chargement.</span>`; return; }
  box.innerHTML = '<span class="sub">Recherche en cours…</span>';
  await new Promise(r => setTimeout(r, 30));
  const coupe = Math.round(jeu.length * 0.6), regl = jeu.slice(0, coupe), verif = jeu.slice(coupe);
  // descente coordonnée par coordonnée, depuis deux points de départ
  let meilleur = null;
  for (const depart of [poids(), COTES_SEULES]) {
    const w = Object.assign({}, depart); let score = prevision(regl, w);
    for (let passe = 0; passe < 3; passe++) for (const c of Object.keys(NOMS_POIDS)) {
      let bv = w[c];
      for (let v = 0; v <= 100; v += 5) { const essai = Object.assign({}, w, {[c]: v}); if (!(essai.M + essai.T + essai.F + essai.R + essai.A + essai.S)) continue;
        const sc = prevision(regl, essai); if (sc > score + 1e-9) { score = sc; bv = v; } }
      w[c] = bv;
    }
    if (!meilleur || score > meilleur.score) meilleur = {w, score};
    await new Promise(r => setTimeout(r, 0));
  }
  // vérification sur les courses récentes : qualité de prévision, puis mise récupérée avec ton pari actuel
  const rendu = w => { let mise = 0, ret = 0, fm = 0, fr = 0;
    for (const x of verif) { const r = rejouerJour(t, k, N, w, x.j, x.comp); if (!r) continue; mise += r[style].nb; ret += r[style].g; fm += r.fav.nb; fr += r.fav.g; }
    return {app: mise ? ret / mise : null, fav: fm ? fr / fm : null}; };
  const actuel = poids();
  const pv = {trouve: prevision(verif, meilleur.w), cotes: prevision(verif, COTES_SEULES), actuel: prevision(verif, actuel)};
  await new Promise(r => setTimeout(r, 0));
  const rd = {trouve: rendu(meilleur.w), cotes: rendu(COTES_SEULES), actuel: rendu(actuel)};
  const mieux = pv.trouve > pv.cotes + 0.01;        // gain net de prévision par rapport aux cotes seules
  reglageTrouve = mieux ? meilleur.w : COTES_SEULES;
  const txtW = w => Object.keys(NOMS_POIDS).filter(c => w[c] > 0).map(c => `${NOMS_POIDS[c]} ${w[c]}`).join(" · ");
  const p100 = x => x === null ? "–" : Math.round(100 * x) + " %";
  box.innerHTML = `<b class="t">${mieux ? "Un réglage prévoit mieux que les cotes seules" : "Aucun réglage ne prévoit mieux que les cotes seules"}</b>
    <span class="sub">Réglé sur les ${regl.length} courses les plus anciennes, vérifié sur les ${verif.length} plus récentes, que le réglage n'a jamais vues.</span>
    <span>Meilleur dosage trouvé : <b>${txtW(meilleur.w)}</b></span>
    <span class="sub">Mise récupérée sur les ${verif.length} courses de vérification (${PARIS[t].nom}, style ${STYLES[style].nom}) :</span>
    <ul><li>dosage trouvé : <b>${p100(rd.trouve.app)}</b></li><li>ton réglage actuel : <b>${p100(rd.actuel.app)}</b></li>
      <li>cotes seules : <b>${p100(rd.cotes.app)}</b></li><li>favoris sans analyse : <b>${p100(rd.trouve.fav)}</b></li></ul>
    <span class="sub">${mieux ? "Ce dosage a mieux prévu les arrivées que les cotes sur des courses qu'il ne connaissait pas. C'est encourageant, mais " + verif.length + " courses restent peu : refais la recherche dans quelques semaines."
      : "Sur les courses de vérification, les analyses ajoutées aux cotes n'ont pas amélioré la prévision. Le plus sûr est de suivre les cotes seules."} La mise récupérée ci-dessus dépend de quelques gros rapports : c'est la qualité de prévision qui décide.</span>
    <button class="copie" id="optimOk" type="button">Appliquer ${mieux ? "ce dosage" : "« cotes seules »"}</button>`;
  $("optimOk").onclick = () => {
    for (const c of Object.keys(NOMS_POIDS)) { $("w" + c).value = reglageTrouve[c]; $("v" + c).textContent = reglageTrouve[c]; try { localStorage.setItem("ml_w" + c, reglageTrouve[c]); } catch (_) {} }
    $("optimOk").textContent = "Réglage appliqué"; recalcul();
  };
}
$("optim").addEventListener("click", () => { chercherReglage().catch(e => { $("optimRes").hidden = false; $("optimRes").textContent = "Recherche impossible : " + e.message; }); });
// ---------- export : toutes les courses passées gardées sur le téléphone pour ce pari, dans un seul fichier compressé
async function exporterCourses() {
  const t = pari, cache = bilanCache[t], et = $("exportEtat"); et.hidden = false;
  const vues = new Set(), liste = [];
  for (const j of (cache && cache.jours ? cache.jours.values() : [])) { if (!j) continue;
    const cle = j.date + "-" + j.r + "-" + j.c; if (vues.has(cle)) continue; vues.add(cle); liste.push(j); }
  if (!liste.length) { et.textContent = "Aucune course chargée pour ce pari : attends la fin du chargement."; return; }
  et.textContent = "Préparation du fichier…";
  const texte = JSON.stringify({pari: t, exporte: new Date().toISOString(), courses: liste});
  let blob = new Blob([texte], {type: "application/json"}), nom = `multi-live-${t.toLowerCase()}-${liste.length}-courses.json`;
  try { if (window.CompressionStream) { blob = await new Response(blob.stream().pipeThrough(new CompressionStream("gzip"))).blob(); blob = new Blob([blob], {type: "application/gzip"}); nom += ".gz"; } } catch (e) {}
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = nom; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  et.textContent = `Fichier « ${nom} » enregistré dans tes Téléchargements (${liste.length} courses, ${(blob.size / 1e6).toFixed(1).replace(".", ",")} Mo).`;
}
// ---------- collecte complète : toutes les courses de chaque jour, gardées sur le téléphone (base à part)
const complet = {
  db: null, actif: false, lock: null,
  ouvrir() {
    if (this.db !== null) return this.db;
    this.db = new Promise(res => { try { const rq = indexedDB.open("multilive-complet", 1);
      rq.onupgradeneeded = () => rq.result.createObjectStore("jours");
      rq.onsuccess = () => res(rq.result); rq.onerror = () => res(null); rq.onblocked = () => res(null); } catch (e) { res(null); } });
    return this.db;
  },
  async tout() {
    const db = await this.ouvrir(); if (!db) return new Map();
    return new Promise(res => { const m = new Map(); try {
      const rq = db.transaction("jours").objectStore("jours").openCursor();
      rq.onsuccess = e => { const c = e.target.result; if (!c) return res(m); m.set(c.key, c.value); c.continue(); }; rq.onerror = () => res(m);
    } catch (e) { res(m); } });
  },
  async poser(d, v) { const db = await this.ouvrir(); if (!db) return;
    return new Promise(res => { try { const tx = db.transaction("jours", "readwrite"); tx.objectStore("jours").put(v, d); tx.oncomplete = res; tx.onerror = res; } catch (e) { res(); } }); },
};
const NB_JOURS_COMPLET = 183;
async function etatComplet(enCours, mem) {
  const m = mem || await complet.tout(), voulus = datesPassees(NB_JOURS_COMPLET);
  const faits = voulus.filter(d => m.has(d)).length, nc = voulus.reduce((n, d) => n + (m.get(d) || []).length, 0);
  $("cEtat").textContent = `Jours complets : ${faits} sur ${voulus.length} · ${nc} courses gardées.` + (enCours ? " " + enCours : "");
  return {m, voulus};
}
async function collecter() {
  if (complet.actif) { complet.actif = false; $("cLancer").textContent = "Lancer la collecte"; return; }
  complet.actif = true; $("cLancer").textContent = "Arrêter la collecte";
  try { if (navigator.wakeLock) complet.lock = await navigator.wakeLock.request("screen"); } catch (e) {}
  let msg = "";
  try {
    const {m, voulus} = await etatComplet();
    for (let i = 0; i < voulus.length; ) {
      const d = voulus[i];
      if (!complet.actif) { msg = "Collecte arrêtée. Elle reprendra où elle s'est arrêtée."; break; }
      if (m.has(d)) { i++; continue; }
      let debut = 0, courses = [], sature = false;
      while (debut !== null && complet.actif) {
        await etatComplet(`En cours : ${d.slice(0, 2)}/${d.slice(2, 4)} (${courses.length} courses)…`, m);
        const r = await fetch(`/api/complet?date=${d}&debut=${debut}`), j = await r.json();
        if (!r.ok) throw new Error(j.erreur || "erreur " + r.status);
        courses = courses.concat(j.courses); debut = j.suite;
        if (j.sature) { sature = true; break; }
      }
      if (sature) { msg = "Le PMU limite les demandes : pause de 3 minutes, la collecte reprend toute seule."; await etatComplet(msg, m);
        for (let s = 0; s < 190 && complet.actif; s++) await new Promise(r => setTimeout(r, 1000)); continue; }   // même jour, de nouveau
      if (debut === null) { await complet.poser(d, courses); m.set(d, courses); }
      i++;
    }
    if (complet.actif) msg = "Collecte terminée.";
  } catch (e) { msg = "Collecte interrompue (" + e.message + "). Relance-la pour reprendre."; }
  complet.actif = false; $("cLancer").textContent = "Lancer la collecte";
  try { if (complet.lock) await complet.lock.release(); } catch (e) {}
  await etatComplet(msg);
}
async function exporterComplet() {
  const {m, voulus} = await etatComplet("Préparation du fichier…");
  const liste = []; let jours = 0;
  for (const d of voulus) if (m.has(d)) { jours++; for (const c of m.get(d)) liste.push(c); }
  if (!liste.length) { await etatComplet("Rien à exporter pour l'instant : lance d'abord la collecte."); return; }
  const texte = JSON.stringify({format: "complet-v1", exporte: new Date().toISOString(), jours, courses: liste});
  let blob = new Blob([texte], {type: "application/json"}), nom = `multi-live-complet-${jours}-jours-${liste.length}-courses.json`;
  try { if (window.CompressionStream) { blob = await new Response(blob.stream().pipeThrough(new CompressionStream("gzip"))).blob(); blob = new Blob([blob], {type: "application/gzip"}); nom += ".gz"; } } catch (e) {}
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = nom; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  await etatComplet(`Fichier « ${nom} » enregistré dans tes Téléchargements (${(blob.size / 1e6).toFixed(1).replace(".", ",")} Mo).`);
}
$("cLancer").addEventListener("click", () => collecter());
$("cExport").addEventListener("click", () => exporterComplet().catch(e => { $("cEtat").textContent = "Export impossible : " + e.message; }));
$("complet").addEventListener("toggle", e => { if (e.target.open && !complet.actif) etatComplet(); });
$("exporter").addEventListener("click", () => exporterCourses().catch(e => { $("exportEtat").hidden = false; $("exportEtat").textContent = "Export impossible : " + e.message; }));
$("parjour").addEventListener("change", e => { try { localStorage.setItem("ml_parjour", e.target.value); } catch (_) {} chargerBilan(); });
$("joues").addEventListener("change", e => { try { localStorage.setItem("ml_joues", e.target.value); } catch (_) {} calculerBilan(); });
try { const jo = localStorage.getItem("ml_joues"); if (jo) $("joues").value = jo; } catch (e) {}

function spark(h) {
  if (!h || h.length < 2) return '<svg class="spark" width="60" height="18"></svg>';
  const v = h.map(x => x[1]); const mn = Math.min(...v), mx = Math.max(...v), rg = (mx - mn) || 1;
  const pts = v.map((y, i) => `${(i / (v.length - 1) * 58 + 1).toFixed(1)},${(1 + 16 * (y - mn) / rg).toFixed(1)}`).join(" ");
  const last = pts.split(" ").pop().split(",");
  return `<svg class="spark" width="60" height="18" viewBox="0 0 60 18"><polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts}"/><circle cx="${last[0]}" cy="${last[1]}" r="2" fill="currentColor"/></svg>`;
}

// ---------- réglages
function recalcul() { calculer(); clearTimeout(bilanTimer); bilanTimer = setTimeout(calculerBilan, 250); }
let curseurTimer = null;
for (const k of CURSEURS) $(k).addEventListener("input", e => {
  try { localStorage.setItem("ml_" + k, e.target.value); } catch (_) {}
  $("v" + k.slice(1)).textContent = e.target.value;                    // le chiffre suit le doigt, le calcul attend la fin du geste
  clearTimeout(curseurTimer); curseurTimer = setTimeout(recalcul, 120); });
$("nbt").addEventListener("change", e => {
  try { localStorage.setItem("ml_nbt", e.target.value); } catch (_) {} recalcul(); });
$("periode").addEventListener("change", e => {
  try { localStorage.setItem("ml_periode", e.target.value); } catch (_) {} chargerBilan(); });
$("style").addEventListener("change", e => {
  style = e.target.value; try { localStorage.setItem("ml_style", style); } catch (_) {} recalcul(); });
for (const id of ["formule", "mise"]) $(id).addEventListener(id === "mise" ? "input" : "change", () => {
  try { localStorage.setItem("ml_" + id, $(id).value); } catch (_) {} recalcul(); });

$("copier").addEventListener("click", async e => {
  const t = e.target.dataset.txt || "";
  const avant = e.target.textContent;
  try { await navigator.clipboard.writeText(t); e.target.textContent = "Copié : " + t.replace(/\n/g, " | "); }
  catch (_) { e.target.textContent = t.replace(/\n/g, " | "); }
  setTimeout(() => e.target.textContent = avant, 2500);
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
