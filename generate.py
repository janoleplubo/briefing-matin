#!/usr/bin/env python3
"""Briefing du matin de Jean : collecte des données gratuites, rédaction par Gemini,
voix d'animateur par Gemini TTS, export en public/briefing.m4a.

Variables d'environnement : GEMINI_API_KEY (obligatoire), VOICE (optionnel, défaut Puck).
"""
import base64
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
NOW = dt.datetime.now(PARIS)
TODAY = NOW.strftime("%Y-%m-%d")
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "public")
HISTORY = os.path.join(ROOT, "history.json")
KEY = os.environ.get("GEMINI_API_KEY", "").strip()
VOICE = os.environ.get("VOICE", "").strip() or "Puck"
UA = "Mozilla/5.0 (compatible; BriefingDuMatin/1.0)"

TEXT_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest"]
TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts", "gemini-2.5-flash-preview-tts"]

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
DATE_LONGUE = f"{JOURS[NOW.weekday()]} {NOW.day} {MOIS[NOW.month - 1]} {NOW.year}"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- réseau
def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def get_json(url):
    return json.loads(get(url))


def clean(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = html.unescape(s)  # double encodage fréquent dans les flux
    return re.sub(r"\s+", " ", s).strip()


def rss(url, n, desc=True):
    xml = get(url)
    out = []
    for it in re.findall(r"<item[\s>].*?</item>", xml, flags=re.S)[:n]:
        t = clean((re.search(r"<title>(.*?)</title>", it, re.S) or [None, ""])[1])
        d = clean((re.search(r"<description>(.*?)</description>", it, re.S) or [None, ""])[1]) if desc else ""
        out.append(f"- {t} — {d[:260]}" if d and d != t else f"- {t}")
    return "\n".join(out)


def gnews(q, n=15, lang="fr"):
    if lang == "fr":
        p = "hl=fr&gl=FR&ceid=FR:fr"
    else:
        p = "hl=en-US&gl=US&ceid=US:en"
    return rss(f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&{p}", n, desc=False)


# ---------------------------------------------------------------- données
WMO = {0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert", 45: "brouillard",
       48: "brouillard givrant", 51: "bruine légère", 53: "bruine", 55: "bruine dense", 61: "pluie faible",
       63: "pluie", 65: "forte pluie", 71: "neige faible", 73: "neige", 75: "forte neige", 80: "averses",
       81: "averses", 82: "violentes averses", 95: "orages", 96: "orages avec grêle", 99: "orages avec grêle"}


def weather():
    j = get_json("https://api.open-meteo.com/v1/forecast?latitude=48.8566&longitude=2.3522"
                 "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,"
                 "wind_speed_10m_max,sunrise,sunset&hourly=temperature_2m,precipitation_probability"
                 "&timezone=Europe%2FParis&forecast_days=1")
    d, h = j["daily"], j["hourly"]
    det = " ; ".join(f"{x}h : {round(h['temperature_2m'][x])} degrés, pluie {h['precipitation_probability'][x]} pour cent"
                     for x in (8, 12, 18))
    return (f"Paris : {WMO.get(d['weather_code'][0], 'temps variable')}, minimum {round(d['temperature_2m_min'][0])} degrés, "
            f"maximum {round(d['temperature_2m_max'][0])} degrés, risque de pluie maximum {d['precipitation_probability_max'][0]} pour cent, "
            f"vent jusqu'à {round(d['wind_speed_10m_max'][0])} kilomètres heure. Lever du soleil {d['sunrise'][0][-5:]}, "
            f"coucher {d['sunset'][0][-5:]}. Détail : {det}.")


def quote(symbol, rng="5d"):
    last_err = None
    for host in ("query1", "query2"):
        try:
            j = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
            r = j["chart"]["result"][0]
            closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
            last, prev, first = closes[-1], closes[-2] if len(closes) > 1 else None, closes[0]
            m = r["meta"]
            return {"last": last, "day": (last - prev) / prev * 100 if prev else None,
                    "range": (last - first) / first * 100, "hi": m.get("fiftyTwoWeekHigh"),
                    "lo": m.get("fiftyTwoWeekLow"), "cur": m.get("currency")}
        except Exception as e:  # noqa
            last_err = e
    raise last_err


def pct(x):
    return "inconnu" if x is None else f"{'+' if x >= 0 else ''}{x:.1f} pour cent"


def markets():
    lines = []
    for name, sym in (("CAC 40", "^FCHI"), ("S&P 500", "^GSPC")):
        try:
            q = quote(sym)
            lines.append(f"{name} : {round(q['last'])} points, {pct(q['day'])} sur la dernière séance, {pct(q['range'])} sur 5 séances.")
        except Exception as e:
            log("bourse", name, e)
    try:
        b = get_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=eur,usd&include_24hr_change=true")["bitcoin"]
        lines.append(f"Bitcoin : {round(b['eur'])} euros ({round(b['usd'])} dollars), {pct(b['eur_24h_change'])} sur 24 heures.")
    except Exception as e:
        log("btc", e)
    return "\n".join(lines) or "(indisponible)"


def saint():
    j = get_json("https://nominis.cef.fr/json/saintdujour.php")["response"]
    s = j.get("saintdujour") or {}
    autres = list((j.get("saints") or {}).get("majeurs", {}).keys())[:3]
    txt = s.get("nom", "inconnu") + (f" ({clean(s['description'])})" if s.get("description") else "")
    return txt + (f". Autres fêtés : {', '.join(autres)}" if autres else "")


def births():
    j = get_json(f"https://api.wikimedia.org/feed/v1/wikipedia/fr/onthisday/births/{NOW.month:02d}/{NOW.day:02d}")
    rows = [f"{b['year']} : {b['text']}" for b in j.get("births", []) if b.get("year", 0) >= 1900]
    return "\n".join(rows[:90])


CAC40 = [("AI.PA", "Air Liquide"), ("AIR.PA", "Airbus"), ("MT.AS", "ArcelorMittal"), ("CS.PA", "AXA"),
         ("BNP.PA", "BNP Paribas"), ("EN.PA", "Bouygues"), ("BVI.PA", "Bureau Veritas"), ("CAP.PA", "Capgemini"),
         ("CA.PA", "Carrefour"), ("ACA.PA", "Crédit Agricole"), ("BN.PA", "Danone"), ("DSY.PA", "Dassault Systèmes"),
         ("ENGI.PA", "Engie"), ("EL.PA", "EssilorLuxottica"), ("ERF.PA", "Eurofins Scientific"), ("RMS.PA", "Hermès"),
         ("KER.PA", "Kering"), ("OR.PA", "L'Oréal"), ("LR.PA", "Legrand"), ("MC.PA", "LVMH"),
         ("ML.PA", "Michelin"), ("ORA.PA", "Orange"), ("RI.PA", "Pernod Ricard"), ("PUB.PA", "Publicis"),
         ("RNO.PA", "Renault"), ("SAF.PA", "Safran"), ("SGO.PA", "Saint-Gobain"), ("SAN.PA", "Sanofi"),
         ("SU.PA", "Schneider Electric"), ("GLE.PA", "Société Générale"), ("STLAP.PA", "Stellantis"),
         ("STMPA.PA", "STMicroelectronics"), ("TEP.PA", "Teleperformance"), ("HO.PA", "Thales"),
         ("TTE.PA", "TotalEnergies"), ("URW.PA", "Unibail-Rodamco-Westfield"), ("VIE.PA", "Veolia"),
         ("DG.PA", "Vinci"), ("EDEN.PA", "Edenred"), ("ALO.PA", "Alstom")]


def sujet_mode():
    r = random.random()
    if r < 0.6:
        return ("SUJET LIBRE : choisis toi-même un sujet passionnant adapté au profil de Jean (une entreprise, un entrepreneur "
                "et comment il a fait fortune, un mécanisme économique ou financier, une crypto ou une technologie, une success "
                "story française, une notion d'investissement, une innovation dans le bâtiment ou la domotique...). Exemples du "
                "style attendu : « comment Bernard Arnault a bâti sa fortune », « masterclass sur Ledger et Éric Larchevêque ».")
    if r < 0.8:
        return ("LIVRE OU THÉORIE : explique un livre célèbre (essai, économie, business, classique) ou une théorie connue "
                "(économie, psychologie, sciences, stratégie) : idée centrale, origine, ce qu'il faut en retenir concrètement.")
    if r < 0.92:
        return ("POÉSIE : présente un poème célèbre du domaine public (auteur mort avant 1950 : Hugo, Baudelaire, Verlaine, "
                "Rimbaud, Apollinaire, La Fontaine...), récite-le ou ses strophes les plus connues s'il est long, puis explique-le simplement.")
    return ("ARCHITECTURE : raconte un bâtiment, un architecte ou un courant architectural marquant, avec des détails concrets "
            "de construction (Jean est conducteur de travaux, il apprécie les détails techniques).")


# ---------------------------------------------------------------- Gemini
def gemini_call(model, body, timeout=300):
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{model} HTTP {e.code}: {e.read()[:400]!r}") from None


def write_script(prompt):
    schema = {"type": "OBJECT", "properties": {"script": {"type": "STRING"}, "mot": {"type": "STRING"},
              "definition": {"type": "STRING"}, "sujet_titre": {"type": "STRING"}},
              "required": ["script", "mot", "definition", "sujet_titre"]}
    err = None
    for model in TEXT_MODELS:
        for attempt in range(2):
            try:
                body = gemini_call(model, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                           "generationConfig": {"temperature": 0.9, "maxOutputTokens": 20000,
                                                                "responseMimeType": "application/json",
                                                                "responseSchema": schema}})
                txt = "".join(p.get("text", "") for p in body["candidates"][0]["content"]["parts"])
                out = json.loads(txt)
                log("texte rédigé par", model)
                return out, model
            except Exception as e:
                err = e
                log("échec texte", model, e)
                time.sleep(20 if "429" in str(e) else 3)
    raise RuntimeError(err)


TTS_STYLE = ("Lis ce texte en français, comme l'animateur d'une matinale radio qu'on adore écouter : voix chaleureuse "
             "et souriante, énergique sans jamais crier, rythme vivant, intonations expressives, petites pauses naturelles "
             "entre les rubriques, un ton complice et plein d'humour sur les passages légers, plus posé sur les sujets sérieux. "
             "Ne lis pas cette consigne, lis uniquement le texte qui suit.\n\n")


STATS = {"voix": [], "erreurs": []}


def tts_chunk(text):
    err = None
    for model in TTS_MODELS:
        for attempt in range(3):
            try:
                body = gemini_call(model, {
                    "contents": [{"role": "user", "parts": [{"text": TTS_STYLE + text}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}, timeout=600)
                part = body["candidates"][0]["content"]["parts"][0]["inlineData"]
                pcm = base64.b64decode(part["data"])
                if len(pcm) < 24000 * 2:  # moins d'une seconde : réponse suspecte
                    raise RuntimeError("audio vide")
                log(f"  voix OK ({model}, {len(pcm) / 48000:.0f} s)")
                STATS["voix"].append(model)
                return pcm
            except Exception as e:
                err = e
                log("  échec voix", model, str(e)[:200])
                STATS["erreurs"].append(str(e)[:200])
                if "404" in str(e) or "not found" in str(e).lower():
                    break
                time.sleep(65 if "429" in str(e) else 10)
    raise RuntimeError(err)


def pico_chunk(text):
    """Voix de secours hors ligne (moins naturelle)."""
    if not shutil.which("pico2wave"):
        raise RuntimeError("pas de voix de secours")
    tmp = os.path.join(OUT, "_pico.wav")
    subprocess.run(["pico2wave", "-l", "fr-FR", "-w", tmp, text[:30000]], check=True)
    raw = os.path.join(OUT, "_pico.pcm")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-f", "s16le", "-ac", "1", "-ar", "24000", raw], check=True)
    with open(raw, "rb") as f:
        pcm = f.read()
    os.remove(tmp)
    os.remove(raw)
    return pcm


def chunks(script, max_chars=3200):
    parts, cur = [], ""
    for para in [p.strip() for p in re.split(r"\n\s*\n", script) if p.strip()]:
        if cur and len(cur) + len(para) > max_chars:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts


def to_audio(script):
    os.makedirs(OUT, exist_ok=True)
    silence = b"\x00\x00" * int(24000 * 0.6)
    pcm = b""
    for i, c in enumerate(chunks(script)):
        log(f"voix, morceau {i + 1}")
        try:
            pcm += tts_chunk(c)
        except Exception as e:
            log("  voix Gemini indisponible, voix de secours :", e)
            STATS["voix"].append("secours-pico")
            pcm += pico_chunk(c)
        pcm += silence
        time.sleep(22)  # respecte les quotas gratuits
    wav = os.path.join(OUT, "_briefing.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-af", "loudnorm=I=-16:TP=-1.5",
                    "-c:a", "aac", "-b:a", "64k", os.path.join(OUT, "briefing.m4a")], check=True)
    os.remove(wav)
    return len(pcm) / 48000


# ---------------------------------------------------------------- principal
def main():
    if not KEY:
        sys.exit("GEMINI_API_KEY manquante")
    hist = {"vocab": [], "topics": [], "actions": []}
    if os.path.exists(HISTORY):
        with open(HISTORY, encoding="utf-8") as f:
            hist.update(json.load(f))

    recent = {a["name"] for a in hist["actions"][-30:]}
    ticker, action = random.choice([c for c in CAC40 if c[1] not in recent] or CAC40)

    jobs = {
        "meteo": weather, "fi": lambda: rss("https://www.francetvinfo.fr/titres.rss", 18),
        "lemonde": lambda: rss("https://www.lemonde.fr/rss/une.xml", 15), "saint": saint, "naissances": births,
        "marches": markets,
        "ia_fr": lambda: gnews("intelligence artificielle when:2d", 20),
        "ia_en": lambda: gnews("artificial intelligence OpenAI OR Anthropic OR Google when:2d", 15, "en"),
        "eco": lambda: rss("https://www.lemonde.fr/economie/rss_full.xml", 12),
        "politique": lambda: rss("https://www.lemonde.fr/politique/rss_full.xml", 10),
        "inter": lambda: rss("https://www.lemonde.fr/international/rss_full.xml", 10),
        "action": lambda: quote(ticker, "1y"),
        "action_news": lambda: gnews(f"{action} bourse when:14d", 8),
    }
    data = {}
    with ThreadPoolExecutor(8) as ex:
        futs = {k: ex.submit(f) for k, f in jobs.items()}
        for k, fu in futs.items():
            try:
                data[k] = fu.result()
            except Exception as e:
                log("donnée indisponible", k, e)
                data[k] = "(indisponible)"
    q = data["action"]
    action_data = (f"Cours {q['last']:.2f} {q['cur']}, {pct(q['day'])} sur la dernière séance, {pct(q['range'])} sur un an, "
                   f"plus haut sur un an {q['hi']}, plus bas {q['lo']}.") if isinstance(q, dict) else "(cours indisponible)"

    past_words = [v["word"] for v in hist["vocab"]]
    revise = random.sample(past_words, min(3, len(past_words))) if NOW.weekday() in (2, 6) and past_words else []
    past_topics = [t["title"] for t in hist["topics"][-60:]]

    revision_txt = (f"\n   Puis une petite RÉVISION : demande à Jean s'il se souvient du sens de ces mots déjà vus : "
                    f"{', '.join(revise)}. Laisse un court suspense, puis redonne la définition de chacun.") if revise else ""

    prompt = f"""Tu écris le texte d'une matinale audio personnelle pour Jean, conducteur de travaux à Paris, curieux d'économie, de bourse, d'entrepreneuriat, de crypto, d'intelligence artificielle, de domotique et de projets techniques.
Nous sommes le {DATE_LONGUE}. Le texte sera LU À VOIX HAUTE par une voix de synthèse d'animateur radio, juste après une chanson de réveil.

RÈGLES D'ÉCRITURE (très important) :
- Français parlé, chaleureux, vivant, phrases courtes. Tutoie Jean.
- AUCUN markdown, aucune puce, aucun emoji, aucun astérisque, aucun titre, aucune indication de mise en scène entre crochets. Uniquement du texte à dire, en paragraphes séparés par une ligne vide.
- Pour la voix : écris "pour cent" au lieu de %, "euros" et "dollars" en toutes lettres, arrondis les chiffres (une décimale maximum), pas d'abréviations obscures, pas d'URL.
- Transitions naturelles et courtes entre les rubriques.
- N'invente RIEN de factuel : actus, bourse, saint et anniversaires viennent uniquement des DONNÉES fournies. Pour la culture, n'affirme que ce dont tu es sûr.
- Longueur totale visée : environ 1650 mots (environ 11 minutes).

ORDRE ET DURÉE DES RUBRIQUES :
1. Bonjour rapide + météo du jour à Paris (environ 60 mots), avec un conseil pratique.
2. Les actus de la veille (environ 300 mots) : les 4 à 6 faits les plus importants.
3. Saint du jour et anniversaires (environ 60 mots, TRÈS rapide) : le saint, puis 2 ou 3 personnalités vraiment connues du grand public nées ce jour.
4. La bourse (environ 80 mots) : CAC 40, S&P 500, Bitcoin.
5. L'info rigolote (environ 80 mots) : une ou deux anecdotes vraies, surprenantes et drôles.
6. L'intelligence artificielle (environ 230 mots) : les trois informations les plus importantes de ces derniers jours, et pourquoi c'est important.
7. Économie mondiale et macroéconomie (environ 150 mots) : une ou deux informations, avec le mécanisme expliqué.
8. Politique française et internationale (environ 220 mots).
9. L'action du jour : {action} (environ 150 mots) : métier, modèle économique, forces et risques, contexte récent, comportement du cours. Pas de conseil d'achat ou de vente.
10. Le sujet du jour (environ 450 mots, trois minutes), forcément DIFFÉRENT de l'action du jour et sans rapport avec {action} : {sujet_mode()}
   Sujets déjà traités, à NE PAS reprendre : {' | '.join(past_topics) or 'aucun'}.
11. Le mot du jour (environ 70 mots) : un mot français soutenu, rare ou utile, sa définition et un exemple. Mots déjà vus, à NE PAS reprendre : {', '.join(past_words[-100:]) or 'aucun'}.{revision_txt}
12. Conclusion d'une phrase, motivante.

DONNÉES DU JOUR :
[MÉTÉO]
{data['meteo']}

[ACTUS franceinfo]
{data['fi']}

[ACTUS Le Monde]
{data['lemonde']}

[SAINT DU JOUR]
{data['saint']}

[NAISSANCES CE JOUR (Wikipédia)]
{data['naissances']}

[MARCHÉS]
{data['marches']}

[IA - presse française]
{data['ia_fr']}

[IA - presse anglophone]
{data['ia_en']}

[ÉCONOMIE]
{data['eco']}

[POLITIQUE FRANCE]
{data['politique']}

[INTERNATIONAL]
{data['inter']}

[ACTION DU JOUR : {action}]
{action_data}
Titres récents :
{data['action_news']}

Réponds en JSON avec : "script" (le texte complet à dire), "mot" (le mot du jour), "definition" (sa définition courte), "sujet_titre" (titre court du sujet du jour)."""

    out, model = write_script(prompt)
    script = re.sub(r"[*#_`]|\[[^\]]{0,40}\]", "", str(out["script"]))
    script = re.sub(r"\n{3,}", "\n\n", script).strip()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "briefing.txt"), "w", encoding="utf-8") as f:
        f.write(script)
    log(f"script : {len(script.split())} mots")

    secs = to_audio(script)
    log(f"audio : {secs / 60:.1f} minutes")

    hist["vocab"].append({"date": TODAY, "word": out["mot"], "definition": out["definition"]})
    hist["topics"].append({"date": TODAY, "title": out["sujet_titre"]})
    hist["actions"].append({"date": TODAY, "name": action})
    hist["last"] = TODAY
    for k in ("vocab", "topics", "actions"):
        hist[k] = hist[k][-400:]
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    status = {"date": TODAY, "heure": dt.datetime.now(PARIS).strftime("%H:%M"), "modele_texte": model,
              "mots": len(script.split()), "minutes": round(secs / 60, 1), "voix": STATS["voix"],
              "erreurs": STATS["erreurs"][-5:], "donnees_manquantes": [k for k, v in data.items() if v == "(indisponible)"]}
    with open(os.path.join(ROOT, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)

    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Briefing du matin</title><style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px;line-height:1.6;background:#fbfaf7;color:#1d1d1b}}
audio{{width:100%}}@media(prefers-color-scheme:dark){{body{{background:#161615;color:#ecebe6}}}}</style></head><body>
<h1>Briefing du {html.escape(DATE_LONGUE)}</h1><audio controls src="briefing.m4a"></audio>
<p>Mot du jour : <b>{html.escape(out['mot'])}</b> — {html.escape(out['definition'])}</p>
{''.join(f'<p>{html.escape(p)}</p>' for p in script.split(chr(10) + chr(10)))}</body></html>""")
    log("terminé, modèle texte :", model)


if __name__ == "__main__":
    main()
