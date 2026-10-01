#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi Live — tableau de bord en temps réel pour choisir tes 6 chevaux au Multi (PMU).

Lance :   python multi_live.py
Puis ouvre dans ton navigateur :  http://localhost:8765
(Sur ton téléphone, s'il est sur le même Wi-Fi : http://<adresse-IP-du-PC>:8765, affichée au lancement.)

Ce que fait la page :
  - Choisit d'office la course avec Multi (ou Mini Multi) la plus proche de 13h55 ; tu peux en changer.
  - Rafraîchit les cotes PMU toutes les 20 s (toutes les 10 s dans les 5 dernières minutes).
  - Croise 4 analyses : cotes du marché, mouvement des cotes (argent qui rentre),
    forme récente (musique), régularité (places / courses). Tu règles leur poids.
  - Tu peux coller les cotes d'autres sites (Zeturf, Geny...) : elles sont ajoutées au marché.
  - Calcule la probabilité que les 4 premiers soient dans 6 chevaux et propose le meilleur ticket
    pour 5 € : 1 ticket à 3 € ou 3 tickets en Flexi 50 % à 1,50 €.
  - Après la course, affiche l'arrivée et si le ticket proposé était gagnant.

Il ne parie jamais : tu joues toi-même sur ton appli PMU.

Options :
  python multi_live.py --port 8800     # autre port
  python multi_live.py --demo          # course fictive qui bouge, sans Internet (pour tester)

Python 3.8+ — rien à installer.
"""

import argparse
import base64
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
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://online.turfinfo.api.pmu.fr/rest/client/1/programme"
HEADERS = {"User-Agent": "Mozilla/5.0 (multi-live perso)", "Accept": "application/json"}

HISTO = {}            # (date, r, c) -> {num: [[ts, cote], ...]}
HISTO_LOCK = threading.Lock()
DEMO = False


# ------------------------------------------------------------------ accès PMU

def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def type_multi(course):
    types = [str(p.get("typePari", "")).upper() for p in course.get("paris", []) or []]
    if any("MINI" in t and "MULTI" in t for t in types):
        return "Mini Multi"
    if any("MULTI" in t for t in types):
        return "Multi"
    return None


def liste_courses(jour):
    if DEMO:
        return demo_programme(jour)
    data = get_json(f"{API}/{jour}?specialisation=INTERNET")
    out = []
    for reu in data.get("programme", {}).get("reunions", []):
        r = reu.get("numOfficiel")
        hippo = (reu.get("hippodrome") or {}).get("libelleCourt", "")
        for c in reu.get("courses", []):
            out.append({
                "r": r, "c": c.get("numOrdre"), "hippodrome": hippo,
                "libelle": c.get("libelle", ""), "heure": c.get("heureDepart"),
                "discipline": c.get("discipline", ""), "distance": c.get("distance"),
                "partants": c.get("nombreDeclaresPartants"), "multi": type_multi(c),
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


def details_course(jour, r, c):
    if DEMO:
        partants, arrivee = demo_partants()
    else:
        data = get_json(f"{API}/{jour}/R{r}/C{c}/participants?specialisation=INTERNET")
        partants = []
        for p in data.get("participants", []):
            direct = (p.get("dernierRapportDirect") or {}).get("rapport")
            ref = (p.get("dernierRapportReference") or {}).get("rapport")
            gains = (p.get("gainsParticipant") or {}).get("gainsCarriere")
            partants.append({
                "num": p.get("numPmu"), "nom": p.get("nom", "?"),
                "partant": str(p.get("statut", "PARTANT")).upper() == "PARTANT",
                "musique": p.get("musique", "") or "",
                "driver": p.get("driver") or p.get("jockey") or "",
                "entraineur": p.get("entraineur", "") or "",
                "courses": p.get("nombreCourses") or 0,
                "victoires": p.get("nombreVictoires") or 0,
                "places": p.get("nombrePlaces") or 0,
                "gains": (gains or 0) / 100,
                "coteMatin": float(ref) if ref else None,
                "coteDirect": float(direct) if direct else None,
                "ordreArrivee": p.get("ordreArrivee"),
            })
        arrivee = sorted((p for p in partants if p.get("ordreArrivee")),
                         key=lambda p: p["ordreArrivee"])
        arrivee = [p["num"] for p in arrivee] or arrivee_course(jour, r, c)

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
    return {"partants": partants, "arrivee": arrivee[:4], "maj": now}


# ------------------------------------------------------------------ mode démo

_demo_state = {}


def demo_programme(jour):
    base = datetime.now().replace(hour=13, minute=55, second=0, microsecond=0)
    if datetime.now() > base + timedelta(minutes=20):
        base = datetime.now() + timedelta(minutes=12)
    ms = lambda d: int(d.timestamp() * 1000)
    return [
        {"r": 1, "c": 1, "hippodrome": "VINCENNES", "libelle": "PRIX DE BAZOCHES", "heure": ms(base - timedelta(minutes=65)),
         "discipline": "ATTELE", "distance": 2700, "partants": 12, "multi": None, "statut": "FIN_COURSE"},
        {"r": 1, "c": 3, "hippodrome": "VINCENNES", "libelle": "PRIX DE RUNGIS (démo)", "heure": ms(base),
         "discipline": "ATTELE", "distance": 2850, "partants": 16, "multi": "Multi", "statut": "PROGRAMMEE"},
        {"r": 2, "c": 5, "hippodrome": "LONGCHAMP", "libelle": "PRIX DES ETANGS", "heure": ms(base + timedelta(minutes=80)),
         "discipline": "PLAT", "distance": 1600, "partants": 11, "multi": "Mini Multi", "statut": "PROGRAMMEE"},
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
        st["_t0"] = time.time()
    out = []
    for n, nom, mus, matin, drv, crs, vic, pl in DEMO_CHEVAUX:
        drift = random.uniform(-0.06, 0.06) + (-0.02 if n in (7, 11) else 0)  # 7 et 11 « joués »
        st[n] = round(max(1.5, st[n] * (1 + drift)), 1)
        out.append({"num": n, "nom": nom, "partant": n != 16, "musique": mus, "driver": drv,
                    "entraineur": "", "courses": crs, "victoires": vic, "places": pl, "gains": crs * 4100.0,
                    "coteMatin": matin, "coteDirect": st[n] if n != 16 else None, "ordreArrivee": None})
    return out, []


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
            if u.path == "/api/course":
                d = details_course(jour, int(q["r"]), int(q["c"]))
                return self.envoyer(200, json.dumps(d))
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
    ap = argparse.ArgumentParser(description="Tableau de bord Multi en direct")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--demo", action="store_true", help="course fictive, sans Internet")
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
</style></head><body><div class="wrap">

<header>
  <h1>Multi Live <small id="modeDemo"></small></h1>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <label for="choixCourse" class="sub">Course</label>
    <select id="choixCourse"></select>
    <span id="etat" class="chip"><span class="dot"></span><span id="etatTxt">Connexion…</span></span>
  </div>
</header>

<div class="course">
  <span class="nom" id="cNom">Chargement du programme…</span>
  <span class="meta" id="cMeta"></span>
  <span class="compte" id="compte"></span>
</div>
<div id="erreur" class="err" hidden></div>
<div id="resultat" class="res" hidden></div>

<div class="grid">
  <section class="panel ticket">
    <h2 id="tTitre">Ticket conseillé — Multi en 6, mise 3 €</h2>
    <div class="nums" id="tNums"></div>
    <div class="kpis">
      <div class="kpi"><b id="tProba">–</b><span>chance que les 4 premiers soient dedans</span></div>
      <div class="kpi"><b id="tChance">–</b><span>soit environ</span></div>
      <div class="kpi"><b id="tSeuil">–</b><span>rapport mini pour être rentable</span></div>
    </div>
    <button class="copie" id="copier" type="button">Copier les numéros</button>
    <div class="flexi">
      <h2>Variante Flexi 50 % — 3 tickets à 1,50 € = 4,50 €</h2>
      <div id="fTickets"></div>
      <p class="note" id="fTotal"></p>
    </div>
    <p class="note">Gains Flexi divisés par 2. Le rapport mini = mise ÷ probabilité : en dessous, le pari perd en moyenne.</p>
  </section>

  <section class="panel">
    <h2>Poids des analyses</h2>
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
    <details>
      <summary>Ajouter les cotes d'un autre site</summary>
      <p class="sub">Une ligne par cheval : <code>numéro;cote</code>. Donne un nom au site, puis Ajouter. Mets-les à jour avant le départ.</p>
      <input id="srcNom" placeholder="Nom du site (ex. Zeturf)" style="width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--surface)">
      <textarea id="srcTxt" placeholder="3;3,8&#10;1;4,1&#10;7;6,5"></textarea>
      <button class="copie" id="srcAjout" type="button">Ajouter</button>
      <div class="srcs" id="srcListe"></div>
    </details>
  </section>
</div>

<div class="tablewrap">
<table>
  <thead><tr>
    <th class="l">N°</th><th class="l">Cheval</th><th>Cote matin</th><th>Cote direct</th><th class="l">Évolution</th>
    <th>Forme</th><th>Régularité</th><th>Proba gagner</th><th class="l">Proba dans les 4</th>
  </tr></thead>
  <tbody id="corps"></tbody>
</table>
</div>

<p class="foot">Estimations à partir des cotes, de la musique et des statistiques du PMU. Elles aident à choisir, elles ne garantissent rien : le PMU prélève une part des mises, donc sur la durée aucune méthode ne gagne à coup sûr. Joue seulement ce que tu acceptes de perdre.</p>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const HEURE_CIBLE = "13:55";
let courses = [], courant = null, donnees = null, timer = null, sources = {};
try { sources = JSON.parse(localStorage.getItem("ml_sources") || "{}"); } catch(e) {}
for (const k of ["wM","wT","wF","wR"]) { try { const v = localStorage.getItem("ml_"+k); if (v !== null) $(k).value = v; } catch(e) {} }

const pct = x => (100*x).toFixed(1).replace(".", ",") + " %";
const euro = x => x.toFixed(2).replace(".", ",") + " €";
const hhmm = ms => new Date(ms).toLocaleTimeString("fr-FR", {hour:"2-digit", minute:"2-digit"});

// ---------- programme
async function chargerProgramme() {
  const r = await fetch("/api/courses"); const j = await r.json();
  if (!r.ok) throw new Error(j.erreur || "Programme indisponible");
  $("modeDemo").textContent = j.demo ? "— démo, course fictive" : "";
  courses = j.courses;
  const sel = $("choixCourse"); sel.innerHTML = "";
  const avecMulti = courses.filter(c => c.multi);
  for (const c of courses) {
    const o = document.createElement("option");
    o.value = c.r + "-" + c.c;
    o.textContent = `${c.heure ? hhmm(c.heure) : "--:--"}  R${c.r}C${c.c} ${c.hippodrome}${c.multi ? "  · " + c.multi : ""}`;
    sel.appendChild(o);
  }
  // course par défaut : Multi la plus proche de 13h55
  const [h, m] = HEURE_CIBLE.split(":").map(Number);
  const cible = new Date(); cible.setHours(h, m, 0, 0);
  const pool = avecMulti.length ? avecMulti : courses;
  let choix = pool[0];
  for (const c of pool) if (Math.abs((c.heure||0) - cible) < Math.abs((choix.heure||0) - cible)) choix = c;
  if (choix) { sel.value = choix.r + "-" + choix.c; choisir(choix); }
}
$("choixCourse").addEventListener("change", e => {
  const [r, c] = e.target.value.split("-").map(Number);
  choisir(courses.find(x => x.r === r && x.c === c));
});

function choisir(c) {
  courant = c; donnees = null;
  $("cNom").textContent = `R${c.r}C${c.c} — ${c.libelle || c.hippodrome}`;
  const bits = [c.hippodrome, c.discipline && c.discipline.toLowerCase(), c.distance && c.distance + " m",
                c.partants && c.partants + " partants", c.multi || "pas de Multi sur cette course"].filter(Boolean);
  $("cMeta").textContent = (c.heure ? "Départ " + hhmm(c.heure) + " · " : "") + bits.join(" · ");
  rafraichir();
}

// ---------- boucle temps réel
async function rafraichir() {
  clearTimeout(timer);
  if (!courant) return;
  try {
    const r = await fetch(`/api/course?r=${courant.r}&c=${courant.c}`); const j = await r.json();
    if (!r.ok) throw new Error(j.erreur || "Données indisponibles");
    donnees = j; $("erreur").hidden = true;
    etat(true, "Cotes en direct · " + new Date(j.maj).toLocaleTimeString("fr-FR"));
    calculer();
  } catch (e) {
    $("erreur").hidden = false; $("erreur").textContent = e.message + ". Nouvel essai dans 20 s.";
    etat(false, "Hors ligne");
  }
  const reste = (courant.heure || 0) - Date.now();
  timer = setTimeout(rafraichir, reste > 0 && reste < 5*60000 ? 10000 : 20000);
}
// Sur téléphone, le navigateur met la page en pause en arrière-plan : on relance dès qu'elle revient
document.addEventListener("visibilitychange", () => { if (!document.hidden) rafraichir(); });
function etat(ok, txt) { $("etat").className = "chip " + (ok ? "live" : "warn"); $("etatTxt").textContent = txt; }

setInterval(() => {
  if (!courant || !courant.heure) { $("compte").textContent = ""; return; }
  const d = courant.heure - Date.now();
  if (d <= 0) { $("compte").textContent = "Partie"; return; }
  const h = Math.floor(d/3600000), m = Math.floor(d/60000)%60, s = Math.floor(d/1000)%60;
  $("compte").textContent = (h ? h + " h " : "") + String(m).padStart(2,"0") + ":" + String(s).padStart(2,"0");
}, 1000);

// ---------- analyses
function scoreMusique(mus) {
  const m = (mus||"").replace(/\(\d+\)/g, "");
  const places = [...m.matchAll(/([0-9DATRN])[a-z]/gi)].map(x => x[1].toUpperCase()).slice(0, 5);
  if (!places.length) return null;
  const bar = {"1":1,"2":.8,"3":.65,"4":.5,"5":.4,"6":.25,"7":.2,"8":.15,"9":.1,"0":.05};
  const w = [1,.85,.7,.55,.4];
  let t = 0, s = 0; places.forEach((p,i) => { t += (bar[p]||0)*w[i]; s += w[i]; });
  return t/s;
}
function normaliser(vals) {           // tableau de valeurs >=0 -> distribution
  const s = vals.reduce((a,b) => a+b, 0) || 1; return vals.map(v => v/s);
}

function calculer() {
  if (!donnees) return;
  const tous = donnees.partants;
  const P = tous.filter(c => c.partant);
  const n = P.length;
  if (n < 4) { $("tNums").textContent = "Pas assez de partants."; return; }

  // 1) Marché : PMU direct + autres sites, chaque source normalisée (marge retirée), puis moyenne
  const parSource = [];
  parSource.push(P.map(c => c.coteDirect || c.coteMatin || null));
  for (const [nom, cotes] of Object.entries(sources)) parSource.push(P.map(c => cotes[c.num] || null));
  const pm = P.map(() => []);
  for (const src of parSource) {
    const inv = src.map(v => v && v > 1 ? 1/v : 0); const s = inv.reduce((a,b)=>a+b,0);
    if (!s) continue; inv.forEach((v,i) => { if (v) pm[i].push(v/s); });
  }
  let marche = pm.map(a => a.length ? a.reduce((x,y)=>x+y,0)/a.length : null);
  const plancher = Math.min(...marche.filter(x => x !== null), 1/n) / 2;
  marche = normaliser(marche.map(x => x === null ? plancher : x));

  // 2) Mouvement : cote matin / cote direct (>1 = cote qui baisse = cheval joué)
  const mouv = normaliser(P.map((c,i) => {
    const f = c.coteMatin && c.coteDirect ? Math.min(2, Math.max(.5, c.coteMatin / c.coteDirect)) : 1;
    return marche[i] * f;
  }));
  // 3) Forme
  const fr = P.map(c => scoreMusique(c.musique));
  const moyF = (fr.filter(x => x !== null).reduce((a,b)=>a+b,0) / (fr.filter(x=>x!==null).length||1)) || .3;
  const forme = normaliser(fr.map(x => Math.pow(x === null ? moyF : x, 2)));
  // 4) Régularité (places / courses, lissée vers 30 % pour les chevaux qui ont peu couru)
  const rg = P.map(c => (c.places + 3*.3) / ((c.courses||0) + 3));
  const regul = normaliser(rg.map(x => x*x));

  const w = {M:+$("wM").value, T:+$("wT").value, F:+$("wF").value, R:+$("wR").value};
  for (const k in w) $("v"+k).textContent = w[k];
  const W = (w.M + w.T + w.F + w.R) || 1;
  const p = normaliser(P.map((_,i) => (w.M*marche[i] + w.T*mouv[i] + w.F*forme[i] + w.R*regul[i]) / W));

  // Probabilités des groupes de 4 premiers (modèle de Harville)
  const idx = P.map((_,i) => i);
  const quart = new Map(); const top4 = new Array(n).fill(0);
  for (const a of idx) for (const b of idx) { if (b===a) continue;
    for (const c of idx) { if (c===a||c===b) continue;
      const r3 = 1 - p[a] - p[b] - p[c]; if (r3 <= 0) continue;
      const base = p[a] * p[b]/(1-p[a]) * p[c]/(1-p[a]-p[b]) / r3;
      for (const d of idx) { if (d===a||d===b||d===c) continue;
        const pr = base * p[d];
        const k = [a,b,c,d].sort((x,y)=>x-y).join(",");
        quart.set(k, (quart.get(k)||0) + pr);
        top4[a]+=pr; top4[b]+=pr; top4[c]+=pr; top4[d]+=pr;
  }}}
  const Q = [...quart].map(([k,v]) => [k.split(",").map(Number), v]);

  // Meilleures sélections de 6 parmi les 11 chevaux les plus probables
  const cand = idx.slice().sort((x,y) => p[y]-p[x]).slice(0, Math.min(11, n));
  const ensembles = combinaisons(cand, Math.min(6, n));
  const couvre = (ens, q) => q.every(i => ens.has(i));
  function meilleur(dejaCouverts, exclus) {
    let best = null, g = -1;
    for (const e of ensembles) { const key = [...e].sort((a,b)=>a-b).join(","); if (exclus.has(key)) continue;
      let s = 0; for (const [q,v] of Q) if (!dejaCouverts.has(q.join(",")) && couvre(e,q)) s += v;
      if (s > g) { g = s; best = e; } }
    return [best, g];
  }
  const [t1, p1] = meilleur(new Set(), new Set());

  // Ticket principal
  const ordre6 = [...t1].sort((a,b) => p[b]-p[a]);
  $("tNums").innerHTML = ordre6.map(i => `<span class="n">${P[i].num}</span>`).join("");
  $("tProba").textContent = pct(p1);
  $("tChance").textContent = "1 chance sur " + Math.round(1/p1);
  $("tSeuil").textContent = euro(3/p1);
  $("copier").dataset.txt = ordre6.map(i => P[i].num).join(" - ");

  // Flexi : 3 tickets choisis pour se compléter
  const couverts = new Set(), exclus = new Set(); let total = 0; let html = "";
  for (let t = 0; t < 3; t++) {
    const [e, g] = meilleur(couverts, exclus); if (!e) break;
    const key = [...e].sort((a,b)=>a-b).join(","); exclus.add(key);
    for (const [q] of Q) if (couvre(e,q)) couverts.add(q.join(","));
    total += g;
    const o = [...e].sort((a,b) => p[b]-p[a]);
    html += `<div class="row">${o.map(i => `<span class="n small" style="display:inline-grid;place-items:center;width:34px;height:34px;border-radius:7px;background:var(--turf);color:var(--surface);font-weight:700">${P[i].num}</span>`).join("")}<em>+${pct(g)}</em></div>`;
  }
  $("fTickets").innerHTML = html;
  $("fTotal").textContent = `Ensemble : ${pct(total)} de chances d'avoir un ticket gagnant (1 sur ${Math.round(1/total)}). Rapport mini par ticket : ${euro(4.5/total)} pour 1,50 €.`;

  // Tableau
  const ordre = idx.slice().sort((x,y) => p[y]-p[x]);
  const maxT = Math.max(...top4);
  const lignes = ordre.map(i => {
    const c = P[i]; const sel = t1.has(i);
    let evo = "", cls = "";
    if (c.coteMatin && c.coteDirect) { const d = (c.coteDirect - c.coteMatin)/c.coteMatin;
      cls = d < -0.03 ? "tr-down" : d > 0.03 ? "tr-up" : ""; evo = (d<0?"▼ ":d>0?"▲ ":"") + Math.abs(d*100).toFixed(0) + " %"; }
    return `<tr class="${sel?"sel":""}">
      <td class="l"><span class="num">${c.num}</span></td>
      <td class="l"><div class="cheval">${esc(c.nom)}</div><div class="sub">${esc(c.driver||"")} · ${esc(c.musique||"")}</div></td>
      <td>${c.coteMatin ? c.coteMatin.toFixed(1).replace(".",",") : "–"}</td>
      <td><b>${c.coteDirect ? c.coteDirect.toFixed(1).replace(".",",") : "–"}</b></td>
      <td class="l ${cls}">${spark(c.histo)} ${evo}</td>
      <td>${fr[i] === null ? "–" : Math.round(fr[i]*100)}</td>
      <td>${c.courses ? Math.round(100*c.places/c.courses) + " %" : "–"}</td>
      <td>${pct(p[i])}</td>
      <td class="l"><span class="bar" style="width:${Math.round(70*top4[i]/maxT)}px"></span>${pct(top4[i])}</td></tr>`;
  });
  const np = tous.filter(c => !c.partant).map(c => `<tr class="np"><td class="l"><span class="num">${c.num}</span></td><td class="l" colspan="8">${esc(c.nom)} — non partant</td></tr>`);
  $("corps").innerHTML = lignes.join("") + np.join("");

  // Arrivée
  const arr = donnees.arrivee || [];
  if (arr.length >= 4) {
    const nums6 = new Set(ordre6.map(i => P[i].num));
    const ok = arr.every(x => nums6.has(x));
    $("resultat").hidden = false;
    $("resultat").textContent = `Arrivée : ${arr.join(" - ")}. Le ticket conseillé était ${ok ? "GAGNANT" : "perdant"}.`;
  } else $("resultat").hidden = true;
}

function combinaisons(arr, k) {
  const out = [];
  (function rec(s, cur) { if (cur.length === k) { out.push(new Set(cur)); return; }
    for (let i = s; i < arr.length; i++) { cur.push(arr[i]); rec(i+1, cur); cur.pop(); } })(0, []);
  return out;
}
function spark(h) {
  if (!h || h.length < 2) return '<svg class="spark" width="60" height="18"></svg>';
  const v = h.map(x => x[1]); const mn = Math.min(...v), mx = Math.max(...v), rg = (mx-mn) || 1;
  const pts = v.map((y,i) => `${(i/(v.length-1)*58+1).toFixed(1)},${(1+16*(y-mn)/rg).toFixed(1)}`).join(" ");
  const last = pts.split(" ").pop().split(",");
  return `<svg class="spark" width="60" height="18" viewBox="0 0 60 18"><polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts}"/><circle cx="${last[0]}" cy="${last[1]}" r="2" fill="currentColor"/></svg>`;
}
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

// ---------- réglages
for (const k of ["wM","wT","wF","wR"]) $(k).addEventListener("input", e => {
  try { localStorage.setItem("ml_"+k, e.target.value); } catch(_) {} calculer(); });

$("copier").addEventListener("click", async e => {
  const t = e.target.dataset.txt || "";
  try { await navigator.clipboard.writeText(t); e.target.textContent = "Copié : " + t; }
  catch(_) { e.target.textContent = t; }
  setTimeout(() => e.target.textContent = "Copier les numéros", 2500);
});

function afficherSources() {
  $("srcListe").innerHTML = Object.entries(sources).map(([n, c]) =>
    `<span class="chip">${esc(n)} · ${Object.keys(c).length} cotes <button type="button" data-n="${esc(n)}" style="border:0;background:none;color:inherit;cursor:pointer" aria-label="Retirer ${esc(n)}">✕</button></span>`).join("");
  $("srcListe").querySelectorAll("button").forEach(b => b.onclick = () => {
    delete sources[b.dataset.n]; sauverSources(); afficherSources(); calculer(); });
}
function sauverSources() { try { localStorage.setItem("ml_sources", JSON.stringify(sources)); } catch(_) {} }
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
