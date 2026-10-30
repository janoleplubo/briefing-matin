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

TEXT_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-3.8-flash-lite-tts"]

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
    # Après 18h (aperçu la veille), on prend la météo du lendemain
    k = 1 if NOW.hour >= 18 else 0
    j = get_json("https://api.open-meteo.com/v1/forecast?latitude=48.8566&longitude=2.3522"
                 "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,weather_code"
                 "&hourly=temperature_2m,precipitation_probability,precipitation"
                 "&timezone=Europe%2FParis&forecast_days=2")
    d, h = j["daily"], j["hourly"]
    H = lambda x: 24 * k + x
    trajet = lambda hs: ", ".join(f"{x}h : {h['precipitation_probability'][H(x)]} pour cent de risque, "
                                  f"{round(h['precipitation'][H(x)], 1)} mm" for x in hs)
    return (f"Paris : {WMO.get(d['weather_code'][k], 'temps variable')}. Température à 8h : {round(h['temperature_2m'][H(8)])} degrés. "
            f"Température à 17h : {round(h['temperature_2m'][H(17)])} degrés. Pluie sur la journée : risque maximum "
            f"{d['precipitation_probability_max'][k]} pour cent, cumul {round(d['precipitation_sum'][k], 1)} mm. "
            f"Trajet du matin : {trajet((7, 8, 9))}. Trajet du soir : {trajet((17, 18, 19))}.")


def velib(code="15105"):
    B = "https://velib-metropole-opendata.smovengo.cloud/opendata/Velib_Metropole/"
    info = {x["station_id"]: x for x in get_json(B + "station_information.json")["data"]["stations"]}
    for x in get_json(B + "station_status.json")["data"]["stations"]:
        if str(x.get("stationCode")) == code:
            t = {k: v for d in x.get("num_bikes_available_types", []) for k, v in d.items()}
            heure = dt.datetime.fromtimestamp(x.get("last_reported", 0), PARIS).strftime("%Hh%M")
            return (f"Station {info.get(x['station_id'], {}).get('name', code)} ({code}), relevé à {heure} : "
                    f"{t.get('mechanical', 0)} Vélib verts (mécaniques), {t.get('ebike', 0)} Vélib bleus (électriques), "
                    f"{x.get('numDocksAvailable', 0)} places libres.")
    return "(station introuvable)"


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
    """Variation arrondie à l'unité, sans décimale."""
    if x is None:
        return "inconnu"
    r = round(x)
    if r == 0:
        return "stable (moins de 0,5 pour cent)" if abs(x) < 0.5 else "stable"
    return f"{'+' if r > 0 else 'moins '}{abs(r)} pour cent"


def markets():
    lines = []
    indices = (("CAC 40", "^FCHI", True), ("MSCI World (via l'ETF iShares MSCI World)", "URTH", True),
               ("Euro Stoxx 50", "^STOXX50E", False), ("S&P 500", "^GSPC", False), ("Nasdaq", "^IXIC", False),
               ("Nikkei", "^N225", False))
    for name, sym, toujours in indices:
        try:
            q = quote(sym)
            lines.append(f"{'[À CITER]' if toujours else '[SEULEMENT SI ÇA BOUGE FORT]'} {name} : {round(q['last'])} points, "
                         f"{pct(q['day'])} sur la dernière séance, {pct(q['range'])} sur 5 séances.")
        except Exception as e:
            log("bourse", name, e)
    try:
        b = get_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=eur&include_24hr_change=true")["bitcoin"]
        lines.append(f"[À CITER] Bitcoin : {round(b['eur'] / 1000)} mille euros, {pct(b['eur_24h_change'])} sur 24 heures.")
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
    if r < 0.45:
        return ("SUJET LIBRE : choisis toi-même un sujet passionnant adapté au profil de Jean (une entreprise, un entrepreneur "
                "et comment il a fait fortune, un mécanisme économique ou financier, une crypto ou une technologie, une success "
                "story française, une notion d'investissement, une innovation dans le bâtiment ou la domotique, un grand "
                "chantier, une découverte scientifique, un fait historique méconnu...). Exemples du style attendu : « comment "
                "Bernard Arnault a bâti sa fortune », « masterclass sur Ledger et Éric Larchevêque ».")
    if r < 0.6:
        return ("INNOVATION : raconte une innovation récente ou à venir, dans la tech et l'IA ou dans le bâtiment et les "
                "matériaux (béton bas carbone, impression 3D de maisons, robots de chantier, BIM, jumeau numérique...) : "
                "le principe, qui est derrière, où on en est, ce que ça change concrètement.")
    if r < 0.78:
        return ("LIVRE OU THÉORIE : explique un livre célèbre (essai, économie, business, classique) ou une théorie connue "
                "(économie, psychologie, sciences, stratégie) : idée centrale, origine, ce qu'il faut en retenir concrètement.")
    if r < 0.9:
        return ("POÉSIE : présente un poème célèbre du domaine public (auteur mort avant 1950 : Hugo, Baudelaire, Verlaine, "
                "Rimbaud, Apollinaire, La Fontaine...), récite-le ou ses strophes les plus connues s'il est long, puis explique-le simplement.")
    return ("ARCHITECTURE : raconte un bâtiment, un architecte ou un courant architectural marquant, avec des détails concrets "
            "de construction (Jean est conducteur de travaux, il apprécie les détails techniques).")


def github_week():
    since = (NOW - dt.timedelta(days=7)).strftime("%Y-%m-%d")
    hdr = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        hdr["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    out = []
    for label, q in (("Connecteurs MCP (pour brancher une IA sur des outils)", f"topic:mcp created:>{since}"),
                     ("Projets IA", f"topic:ai created:>{since}"),
                     ("Projets IA les plus suivis cette semaine (toutes dates)", f"topic:llm pushed:>{since} stars:>5000")):
        req = urllib.request.Request("https://api.github.com/search/repositories?" +
                                     urllib.parse.urlencode({"q": q, "sort": "stars", "order": "desc", "per_page": 5}), headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            items = json.loads(r.read()).get("items", [])
        out.append(f"{label} :\n" + "\n".join(f"- {i['full_name']} ({i['stargazers_count']} étoiles) : {(i.get('description') or '')[:160]}" for i in items))
    return "\n".join(out)


def atom(url, n):
    """Flux Atom (ou RSS) : titres + résumé court."""
    xml = get(url)
    items = re.findall(r"<entry[\s>].*?</entry>", xml, flags=re.S) or re.findall(r"<item[\s>].*?</item>", xml, flags=re.S)
    out = []
    for it in items[:n]:
        t = clean((re.search(r"<title[^>]*>(.*?)</title>", it, re.S) or [None, ""])[1])
        d = clean((re.search(r"<(?:summary|content|description)[^>]*>(.*?)</(?:summary|content|description)>", it, re.S) or [None, ""])[1])
        out.append(f"- {t} — {d[:220]}" if d and d != t else f"- {t}")
    return "\n".join(out)


def hn_ai():
    """Hacker News : histoires IA les plus votées des dernières 36 h (le radar des développeurs)."""
    since = int(time.time()) - 36 * 3600
    seen, out = set(), []
    for q in ("LLM", "AI model", "OpenAI", "Anthropic Claude", "Gemini", "DeepSeek", "Qwen", "agent"):
        j = get_json("https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
            {"tags": "story", "query": q, "numericFilters": f"created_at_i>{since},points>60", "hitsPerPage": 8}))
        for h in j.get("hits", []):
            if h["objectID"] not in seen:
                seen.add(h["objectID"])
                out.append((h.get("points", 0), f"- {h['title']} ({h.get('points', 0)} points)"))
    return "\n".join(x for _, x in sorted(out, reverse=True)[:15]) or "(rien de marquant)"


def hf_trending():
    """Hugging Face : modèles ouverts en tendance (on y voit arriver les modèles chinois et open source)."""
    j = get_json("https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=20")
    out = []
    for m in j:
        created = (m.get("createdAt") or "")[:10]
        out.append(f"- {m['id']} (tâche : {m.get('pipeline_tag') or '?'}, créé le {created}, {m.get('likes', 0)} likes)")
    return "\n".join(out)


def hf_papers():
    """Hugging Face Daily Papers : les publications de recherche du jour les plus votées."""
    j = get_json("https://huggingface.co/api/daily_papers?limit=30")
    j = sorted(j, key=lambda x: x.get("paper", {}).get("upvotes", 0), reverse=True)[:6]
    return "\n".join(f"- {x['paper']['title']} — {clean(x['paper'].get('summary', ''))[:200]}" for x in j)


# ---------------------------------------------------------------- Gemini
STATS = {"voix": [], "erreurs": []}


def gemini_call(model, body, timeout=180):
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
              "definition": {"type": "STRING"}, "sujet_titre": {"type": "STRING"},
              "ia_titre": {"type": "STRING"},
              "ia_actus": {"type": "ARRAY", "items": {"type": "STRING"}}},
              "required": ["script", "mot", "definition", "sujet_titre", "ia_titre", "ia_actus"]}
    err, best = None, None
    for model in TEXT_MODELS:
        for attempt in range(2):
            try:
                body = gemini_call(model, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                           "generationConfig": {"temperature": 0.9, "maxOutputTokens": 20000,
                                                                "responseMimeType": "application/json",
                                                                "responseSchema": schema}})
                cand = body["candidates"][0]
                if "content" not in cand:
                    raise RuntimeError(f"réponse vide ({cand.get('finishReason')})")
                out = json.loads("".join(p.get("text", "") for p in cand["content"]["parts"]))
                n = len(str(out.get("script", "")).split())
                log("texte rédigé par", model, n, "mots")
                if best is None or n > best[2]:
                    best = (out, model, n)
                if n >= 1400:
                    return out, model
                STATS["erreurs"].append(f"{model}: texte trop court ({n} mots)")
                break
            except Exception as e:
                err = e
                STATS["erreurs"].append(f"{model}: {str(e)[:150]}")
                log("échec texte", model, e)
                if "404" in str(e):
                    break
                time.sleep(20 if "429" in str(e) else 3)
    if best:
        return best[0], best[1]
    raise RuntimeError(err)


TTS_STYLE = ""  # aucune consigne : le modèle de voix la lisait à voix haute


START = time.time()
DEADLINE = 32 * 60  # au-delà, voix de secours pour garantir un briefing à l'heure


QUOTA_DEAD = False  # True dès qu'un morceau entier a échoué sur quota : on passe direct à la voix de secours


def tts_chunk(text):
    global QUOTA_DEAD
    if time.time() - START > DEADLINE:
        raise RuntimeError("temps dépassé, voix de secours")
    if QUOTA_DEAD:
        raise RuntimeError("quota de voix Gemini épuisé, voix de secours")
    err = None
    for model in TTS_MODELS:
        for attempt in range(3):
            try:
                body = gemini_call(model, {
                    "contents": [{"role": "user", "parts": [{"text": TTS_STYLE + text}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}, timeout=240)
                cand = body["candidates"][0]
                if "content" not in cand:
                    raise RuntimeError(f"pas d'audio ({cand.get('finishReason')})")
                part = cand["content"]["parts"][0]["inlineData"]
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
                if time.time() - START > DEADLINE:
                    break
                time.sleep(40 if "429" in str(e) else 8)
    if err is not None and "429" in str(err) and "quota" in str(err).lower():
        QUOTA_DEAD = True
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
    dest = os.path.join(OUT, "briefing.m4a")
    music_dir = os.path.join(ROOT, "music")
    # Musique d'intro désactivée pour l'instant (mettre MUSIC=1 pour la réactiver)
    tracks = sorted(f for f in os.listdir(music_dir) if f.endswith(".mp3")) if os.path.isdir(music_dir) and os.environ.get("MUSIC", "1") == "1" else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-af", "loudnorm=I=-16:TP=-1.5",
                    "-ar", "44100", "-ac", "1", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", os.path.join(OUT, "briefing-voix.m4a")], check=True)
    if tracks:
        track = random.choice(tracks)
        STATS["musique"] = track
        log("musique d'intro :", track)
        # Façon radio : la musique monte très doucement (45 s), la voix entre par-dessus,
        # la musique passe en fond puis s'efface progressivement sous la voix (jusqu'à 75 s).
        env = "if(lt(t,45),1,if(lt(t,51),1-(t-45)/6*0.85,0.15*max(0,(75-t)/24)))"
        flt = ("[0:a]atrim=0:75,asetpts=N/SR/TB,aformat=sample_rates=44100:channel_layouts=mono,"
               "loudnorm=I=-18:TP=-2,aresample=44100,afade=t=in:st=0:d=45:curve=qua,"
               f"volume='{env}':eval=frame[m];"
               "[1:a]aformat=sample_rates=44100:channel_layouts=mono,loudnorm=I=-16:TP=-1.5,aresample=44100,"
               "adelay=45000:all=1[v];"
               "[m][v]amix=inputs=2:duration=longest:normalize=0[out]")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.join(music_dir, track), "-i", wav,
                        "-filter_complex", flt, "-map", "[out]", "-ar", "44100", "-ac", "1", "-c:a", "aac", "-b:a", "80k", "-movflags", "+faststart", dest], check=True)
    else:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-af", "loudnorm=I=-16:TP=-1.5",
                        "-ar", "44100", "-ac", "1", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", dest], check=True)
    os.remove(wav)
    return len(pcm) / 48000


# ---------------------------------------------------------------- principal
def republish():
    """Republie le site (flux podcast compris) à partir de l'audio déjà en ligne, sans regénérer."""
    os.makedirs(OUT, exist_ok=True)
    for f in ("briefing.m4a", "briefing-voix.m4a", "briefing.txt", "index.html"):
        try:
            req = urllib.request.Request(BASE_URL + f, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(os.path.join(OUT, f), "wb") as o:
                o.write(r.read())
        except Exception as e:
            log("republication : fichier absent", f, e)
    for f in ("briefing.m4a", "briefing-voix.m4a"):  # ré-encodage au format lu par Apple Podcasts
        src = os.path.join(OUT, f)
        if os.path.exists(src):
            tmp = src + ".tmp.m4a"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ar", "44100", "-ac", "1",
                            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", tmp], check=True)
            os.replace(tmp, src)
    if not os.path.exists(os.path.join(OUT, "briefing-voix.m4a")):
        shutil.copy(os.path.join(OUT, "briefing.m4a"), os.path.join(OUT, "briefing-voix.m4a"))
    secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 os.path.join(OUT, "briefing.m4a")], capture_output=True, text=True).stdout.strip() or 0)
    with open(HISTORY, encoding="utf-8") as f:
        h = json.load(f)
    out = {"mot": h["vocab"][-1]["word"], "sujet_titre": h["topics"][-1]["title"]}
    write_feed(secs, out)
    log("republication faite", round(secs / 60, 1), "minutes")


def main():
    if os.environ.get("REPUBLISH") == "1":
        return republish()
    if not KEY:
        sys.exit("GEMINI_API_KEY manquante")
    hist = {"vocab": [], "topics": [], "actions": [], "ia": [], "ia_news": []}
    if os.path.exists(HISTORY):
        with open(HISTORY, encoding="utf-8") as f:
            hist.update(json.load(f))

    recent = {a["name"] for a in hist["actions"][-30:]}
    pool = [c for c in CAC40 if c[1] != "Saint-Gobain"]
    ticker, action = random.choice([c for c in pool if c[1] not in recent] or pool)

    jobs = {
        "meteo": weather, "velib": velib, "fi": lambda: rss("https://www.francetvinfo.fr/titres.rss", 18),
        "lemonde": lambda: rss("https://www.lemonde.fr/rss/une.xml", 15), "saint": saint, "naissances": births,
        "marches": markets,
        "ia_fr": lambda: gnews("intelligence artificielle when:2d", 20),
        "ia_en": lambda: gnews("artificial intelligence OpenAI OR Anthropic OR Google when:2d", 15, "en"),
        "eco": lambda: rss("https://www.lemonde.fr/economie/rss_full.xml", 12),
        "politique": lambda: rss("https://www.lemonde.fr/politique/rss_full.xml", 10),
        "inter": lambda: rss("https://www.lemonde.fr/international/rss_full.xml", 10),
        "action": lambda: quote(ticker, "1y"),
        "action_news": lambda: gnews(f"{action} bourse when:14d", 8),
        "sgo": lambda: quote("SGO.PA", "1mo"),
        "sgo_news": lambda: gnews("Saint-Gobain when:3d", 12),
        "ia_innov": lambda: gnews("nouveau modèle IA lancement innovation when:3d", 12),
        "ia_labos": lambda: gnews("(OpenAI OR Anthropic OR \"Google DeepMind\" OR Gemini OR Meta OR xAI OR Mistral) "
                                  "(launches OR releases OR unveils OR announces) model when:2d", 15, "en"),
        "ia_chine": lambda: gnews("(DeepSeek OR Qwen OR Alibaba OR Kimi OR Moonshot OR Zhipu OR GLM OR MiniMax OR "
                                  "ByteDance Doubao OR Tencent Hunyuan OR Baidu Ernie) AI model when:4d", 12, "en"),
        "ia_outils": lambda: gnews("(new feature OR now lets OR now can OR rolls out) (ChatGPT OR Claude OR Gemini OR "
                                   "Copilot OR Perplexity OR Cursor OR agent) when:3d", 12, "en"),
        "ia_hn": hn_ai, "ia_hf": hf_trending, "ia_papers": hf_papers,
        "ia_decoder": lambda: atom("https://the-decoder.com/feed/", 12),
        "ia_simon": lambda: atom("https://simonwillison.net/atom/everything/", 10),
        "batiment": lambda: gnews("innovation bâtiment construction BTP when:7d", 12),
    }
    if NOW.weekday() == 0 or os.environ.get("PREVIEW") == "1":
        jobs["github"] = github_week
    data = {}
    with ThreadPoolExecutor(12) as ex:
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
    past_ia = [t["title"] for t in hist.get("ia", [])[-80:]]
    past_ia_news = [t["title"] for t in hist.get("ia_news", [])[-60:]]
    extra = ""
    if NOW.weekday() in (1, 4):  # mardi et vendredi
        extra += ("8 bis. Innovation bâtiment (environ 120 mots) : une innovation récente du bâtiment ou de la construction tirée "
                  "des titres [BÂTIMENT], expliquée concrètement pour un conducteur de travaux.\n")
    if (NOW.weekday() == 0 or os.environ.get("PREVIEW") == "1") and isinstance(data.get("github"), str) and data["github"] != "(indisponible)":
        extra += ("8 ter. Le GitHub de la semaine (environ 150 mots) : présente les 3 meilleurs connecteurs ou projets IA de la "
                  "semaine tirés de [GITHUB], à quoi ils servent concrètement et pour qui. Dis les noms simplement, sans le "
                  "nom du compte s'il est imprononçable.\n")
    EXTRA = extra
    GITHUB_DATA = f"[GITHUB - nouveautés de la semaine]\n{data['github']}\n\n" if "github" in data else ""
    g = data["sgo"]
    sgo_data = (f"Cours {round(g['last'])} euros, {pct(g['day'])} sur la dernière séance, {pct(g['range'])} sur un mois."
                if isinstance(g, dict) else "(cours indisponible)")

    revision_txt = (f"\n   Puis une petite RÉVISION : demande à Jean s'il se souvient du sens de ces mots déjà vus : "
                    f"{', '.join(revise)}. Laisse un court suspense, puis redonne la définition de chacun.") if revise else ""

    prompt = f"""Tu écris le texte d'une matinale audio personnelle pour Jean, conducteur de travaux à Paris, curieux d'économie, de bourse, d'entrepreneuriat, de crypto, d'intelligence artificielle, de domotique et de projets techniques.
Nous sommes le {DATE_LONGUE}. Le texte sera LU À VOIX HAUTE par une voix de synthèse d'animateur radio, juste après une chanson de réveil.

RÈGLES D'ÉCRITURE (très important) :
- Français parlé, chaleureux, vivant, phrases courtes. Tutoie Jean.
- AUCUN markdown, aucune puce, aucun emoji, aucun astérisque, aucun titre, aucune indication de mise en scène entre crochets. Uniquement du texte à dire, en paragraphes séparés par une ligne vide.
- Pour la voix : écris "pour cent" au lieu de %, "euros" et "dollars" en toutes lettres, pas d'abréviations obscures, pas d'URL.
- Écris toujours "ia" en minuscules (jamais "IA" en majuscules), pour que la voix le prononce d'un seul mot.
- BOURSE : AUCUN chiffre après la virgule, jamais. Arrondis tout à l'unité (cours et variations). Une variation qui s'arrondit à zéro se dit "stable".
- Transitions naturelles et courtes entre les rubriques.
- N'invente RIEN de factuel : actus, bourse, saint et anniversaires viennent uniquement des DONNÉES fournies. Pour la culture, n'affirme que ce dont tu es sûr.
- Longueur totale visée : entre 1900 et 2150 mots (environ 14 minutes), pas plus de 2150.

ORDRE ET DURÉE DES RUBRIQUES :
1. Bonjour rapide + météo à Paris (environ 70 mots). Jean va au travail À VÉLO : donne la température à 8h et à 17h, puis dis clairement s'il va pleuvoir ou non sur le trajet du matin (vers 8h) et du soir (vers 17h-18h). Uniquement ces faits : AUCUN conseil vestimentaire ou d'équipement (pas de gants, veste, cape, etc.). Ne parle PAS du vent, ni du lever ou du coucher du soleil.
1 bis. Vélib (une ou deux phrases) : juste après la météo, le nombre de Vélib verts (mécaniques) et bleus (électriques) disponibles à la station Fédération-Suffren, et le nombre de places libres, en précisant l'heure du relevé (c'est la situation au moment où le briefing a été préparé). Si la donnée est indisponible, n'en parle pas.
2. Les actus de la veille (environ 260 mots) : les 4 à 6 faits les plus importants.
3. Saint du jour et anniversaires (environ 60 mots, TRÈS rapide) : le saint, puis 2 ou 3 personnalités vraiment connues du grand public nées ce jour.
4. La bourse (environ 45 mots, COURT et factuel) : toujours le CAC 40, le MSCI World (dis simplement "le MSCI World", l'indice des actions mondiales) et le Bitcoin. Les autres indices marqués "SEULEMENT SI ÇA BOUGE FORT" ne sont cités que si leur variation sur la séance atteint 2 pour cent ou plus ; sinon, résume la tendance mondiale en quelques mots. Ne cite JAMAIS le S&P 500 par son nom sauf mouvement d'au moins 2 pour cent.
5. Saint-Gobain (environ 120 mots), CHAQUE JOUR : le cours et sa variation (arrondis à l'unité), puis les actualités récentes de l'entreprise tirées des titres fournis (contrats, résultats, acquisitions, avis d'analystes, marché de la construction). S'il n'y a vraiment rien de neuf, dis-le en une phrase et rappelle un enjeu de fond du groupe.
6. L'info rigolote (environ 80 mots) : une ou deux anecdotes vraies, surprenantes et drôles.
7. La veille ia de pointe (environ 420 mots), CHAQUE JOUR, la rubrique la plus importante : Jean veut être EN AVANCE sur son entourage, pas répéter ce que dit le journal de 20 heures. Ton d'expert qui briefe un initié, précis et concret. Appuie-toi en priorité sur les sources spécialisées [IA - LABOS], [IA - MODÈLES CHINOIS], [IA - NOUVELLES FONCTIONS], [IA - HACKER NEWS], [IA - HUGGING FACE], [IA - RECHERCHE], [IA - THE DECODER] et [IA - SIMON WILLISON] ; la presse généraliste sert seulement de complément. Structure, en enchaînant naturellement :
   a) Les nouveaux modèles : ce qui est sorti ou a été annoncé (laboratoires américains, Mistral, et TOUJOURS un œil sur la Chine : DeepSeek, Qwen, Kimi, GLM, MiniMax... s'il y a du neuf). Nomme le modèle exactement, dis qui le fait, ce qu'il fait mieux que les autres (raisonnement, code, vidéo, prix, taille, ouvert ou fermé) et où il se situe face à la concurrence.
   b) Les nouvelles capacités : une fonction ou un outil que l'on peut utiliser dès maintenant, et ce que ça permet concrètement de faire.
   c) Le signal faible : une chose que seuls les initiés regardent (un projet qui monte sur Hugging Face ou Hacker News, un papier de recherche, une tendance), et pourquoi ça compte.
   d) À tester aujourd'hui : une action concrète, simple et réaliste que Jean peut faire en quelques minutes avec un outil grand public qui existe vraiment (ChatGPT, Claude, Gemini, Mistral Le Chat, Perplexity, NotebookLM...), de préférence liée à une nouveauté citée, pour garder une longueur d'avance. Rien d'inventé, pas d'installation technique compliquée.
   Priorité aux nouveautés de la veille ; si elles sont maigres, reprends une nouveauté un peu plus ancienne mais importante qui n'a pas encore été traitée. Précise toujours si c'est d'hier ou de quelques jours. Ne cite que des faits présents dans les données ; si une source est vide, n'en parle pas. Actus ia déjà traitées les jours précédents, à NE PAS répéter sauf vrai rebondissement : {' | '.join(past_ia_news) or 'aucune'}.
8. L'IA expliquée (environ 180 mots), CHAQUE JOUR : explique simplement UNE notion d'IA, en variant chaque jour entre une entreprise (son histoire, son modèle), une théorie ou un concept (réseau de neurones, transformeur, apprentissage par renforcement, hallucination, test de Turing...), une définition de vocabulaire (token, prompt, agent, fine-tuning, RAG...) ou un usage concret au travail. Avec une image ou une analogie du quotidien. Notions déjà expliquées, à NE PAS reprendre : {' | '.join(past_ia) or 'aucune'}.
{EXTRA}9. Économie mondiale et macroéconomie (environ 150 mots) : une ou deux informations, avec le mécanisme expliqué.
10. Politique française et internationale (environ 200 mots).
11. L'action du jour : {action} (environ 130 mots) : métier, modèle économique, forces et risques, contexte récent, comportement du cours. Pas de conseil d'achat ou de vente.
12. Le sujet du jour (environ 400 mots), forcément DIFFÉRENT de l'action du jour, de Saint-Gobain et de la notion d'IA : {sujet_mode()}
   Sujets déjà traités, à NE PAS reprendre : {' | '.join(past_topics) or 'aucun'}.
13. Le mot du jour (environ 70 mots) : un mot français soutenu, rare ou utile, sa définition et un exemple. Mots déjà vus, à NE PAS reprendre : {', '.join(past_words[-100:]) or 'aucun'}.{revision_txt}
14. Conclusion d'une phrase, motivante.

DONNÉES DU JOUR :
[MÉTÉO]
{data['meteo']}

[VÉLIB]
{data['velib']}

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

[IA - innovations récentes]
{data['ia_innov']}

[IA - LABOS (annonces des grands laboratoires, anglais)]
{data['ia_labos']}

[IA - MODÈLES CHINOIS]
{data['ia_chine']}

[IA - NOUVELLES FONCTIONS (ChatGPT, Claude, Gemini, agents...)]
{data['ia_outils']}

[IA - HACKER NEWS (le plus voté chez les développeurs, dernières 36 h)]
{data['ia_hn']}

[IA - HUGGING FACE (modèles ouverts en tendance)]
{data['ia_hf']}

[IA - RECHERCHE (papiers du jour les plus votés)]
{data['ia_papers']}

[IA - THE DECODER (site spécialisé)]
{data['ia_decoder']}

[IA - SIMON WILLISON (blog de référence sur les LLM)]
{data['ia_simon']}

[BÂTIMENT - innovations]
{data['batiment']}

{GITHUB_DATA}[SAINT-GOBAIN]
{sgo_data}
Titres récents :
{data['sgo_news']}

Réponds en JSON avec : "script" (le texte complet à dire), "mot" (le mot du jour), "definition" (sa définition courte), "sujet_titre" (titre court du sujet du jour), "ia_titre" (titre court de la notion d'IA expliquée), "ia_actus" (liste de 3 à 5 titres très courts des actus ia citées dans la rubrique 7, par exemple « sortie de DeepSeek V4 »)."""

    out, model = write_script(prompt)
    script = re.sub(r"[*#_`]|\[[^\]]{0,40}\]", "", str(out["script"]))
    script = re.sub(r"\n{3,}", "\n\n", script).strip()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "briefing.txt"), "w", encoding="utf-8") as f:
        f.write(script)
    log(f"script : {len(script.split())} mots")
    if os.environ.get("PREVIEW") == "1":
        with open(os.path.join(ROOT, "preview.txt"), "w", encoding="utf-8") as f:
            f.write(f"Aperçu généré le {dt.datetime.now(PARIS).strftime('%d/%m %H:%M')} par {model}, {len(script.split())} mots\n\n{script}")
        log("aperçu écrit")
        return

    secs = to_audio(script)
    log(f"audio : {secs / 60:.1f} minutes")

    hist["vocab"].append({"date": TODAY, "word": out["mot"], "definition": out["definition"]})
    hist["topics"].append({"date": TODAY, "title": out["sujet_titre"]})
    hist["actions"].append({"date": TODAY, "name": action})
    hist.setdefault("ia", []).append({"date": TODAY, "title": out.get("ia_titre", "")})
    hist.setdefault("ia_news", []).extend({"date": TODAY, "title": str(t)} for t in (out.get("ia_actus") or [])[:6])
    hist["last"] = TODAY
    for k in ("vocab", "topics", "actions", "ia", "ia_news"):
        hist[k] = hist[k][-400:]
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    status = {"date": TODAY, "heure": dt.datetime.now(PARIS).strftime("%H:%M"), "modele_texte": model,
              "mots": len(script.split()), "minutes": round(secs / 60, 1), "voix": STATS["voix"],
              "erreurs": STATS["erreurs"][-5:], "donnees_manquantes": [k for k, v in data.items() if v == "(indisponible)"],
              "musique": STATS.get("musique")}
    with open(os.path.join(ROOT, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)

    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Briefing du matin</title><style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px;line-height:1.6;background:#fbfaf7;color:#1d1d1b}}
audio{{width:100%}}@media(prefers-color-scheme:dark){{body{{background:#161615;color:#ecebe6}}}}</style></head><body>
<h1>Briefing du {html.escape(DATE_LONGUE)}</h1><audio controls src="briefing.m4a"></audio>
{'' if not STATS.get('musique') else ''}<p><small>Musique d'intro : « {html.escape(str(STATS.get('musique', '')).replace('_', ' ').replace('.mp3', ''))} », Kevin MacLeod (incompetech.com), licence CC BY 4.0.</small></p>
<p>Mot du jour : <b>{html.escape(out['mot'])}</b> — {html.escape(out['definition'])}</p>
{''.join(f'<p>{html.escape(p)}</p>' for p in script.split(chr(10) + chr(10)))}</body></html>""")
    write_feed(secs, out)
    log("terminé, modèle texte :", model)


BASE_URL = "https://janoleplubo.github.io/briefing-matin/"


def copy_cours():
    """Ajoute au site les épisodes du podcast « Les cours de Jean » (cours/site)."""
    src = os.path.join(ROOT, "cours", "site")
    if os.path.isdir(src):
        for f in os.listdir(src):
            shutil.copy(os.path.join(src, f), os.path.join(OUT, f))


def write_feed(secs, out):
    copy_cours()
    _feed(secs, out, "briefing.m4a", "feed.xml", "Le Réveil de Jean", 1 if STATS.get("musique") else 0)
    _feed(secs, out, "briefing-voix.m4a", "feed-voix.xml", "Le Réveil de Jean (voix seule)", 0)


def _feed(secs, out, src, feed_name, show, extra_min):
    """Flux podcast privé : un seul épisode, celui du jour (pour Apple Podcasts)."""
    import email.utils
    ep = src.replace(".m4a", f"-{TODAY}-{int(time.time())}.m4a")  # nom unique : Apple Podcasts ne garde pas l'ancien en cache
    shutil.copy(os.path.join(OUT, src), os.path.join(OUT, ep))
    size = os.path.getsize(os.path.join(OUT, ep))
    dur = f"{int(secs // 60) + extra_min}:00"
    pub = email.utils.format_datetime(NOW)
    titre = f"Briefing du {DATE_LONGUE}"
    desc = f"Mot du jour : {out['mot']}. Sujet : {out['sujet_titre']}. Musique d'intro : Kevin MacLeod (incompetech.com), CC BY 4.0."
    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<atom:link href="{BASE_URL}{feed_name}" rel="self" type="application/rss+xml"/>
<title>{show}</title>
<link>{BASE_URL}</link>
<language>fr-fr</language>
<description>Briefing audio quotidien personnel.</description>
<itunes:author>Briefing du matin</itunes:author>
<image><url>{BASE_URL}cover.png</url><title>{show}</title><link>{BASE_URL}</link></image>
<itunes:image href="{BASE_URL}cover.png"/>
<itunes:explicit>false</itunes:explicit>
<itunes:category text="News"/>
<itunes:owner><itunes:name>Jean</itunes:name><itunes:email>briefing-bot@users.noreply.github.com</itunes:email></itunes:owner>
<itunes:type>episodic</itunes:type>
<item>
<title>{html.escape(titre)}</title>
<description>{html.escape(desc if STATS.get('musique') else desc.split(" Musique")[0])}</description>
<enclosure url="{BASE_URL}{ep}" length="{size}" type="audio/x-m4a"/>
<guid isPermaLink="false">{feed_name}-{TODAY}-{int(time.time())}</guid>
<pubDate>{pub}</pubDate>
<itunes:duration>{dur}</itunes:duration>
</item>
</channel>
</rss>
"""
    with open(os.path.join(OUT, feed_name), "w", encoding="utf-8") as f:
        f.write(rss_xml)
    cover = os.path.join(ROOT, "cover.png")
    if os.path.exists(cover):
        shutil.copy(cover, os.path.join(OUT, "cover.png"))


if __name__ == "__main__":
    main()
