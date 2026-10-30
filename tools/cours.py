#!/usr/bin/env python3
"""Podcast « Les cours de Jean » : transforme chaque texte de cours/textes/ en épisode audio
(voix Gemini TTS) et construit le flux cours/site/feed-cours.xml.

Format d'un texte : première ligne = titre de l'épisode, puis une ligne vide, puis le texte à lire.
Les fichiers déjà convertis (présents dans cours/episodes.json) ne sont pas refaits.
"""
import email.utils
import html
import json
import os
import subprocess
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import generate as g  # noqa: E402

# Les cours sont longs : la limite de 32 min du briefing du matin ne s'applique pas ici.
g.DEADLINE = 110 * 60

TEXTES = os.path.join(ROOT, "cours", "textes")
SITE = os.path.join(ROOT, "cours", "site")
EPISODES = os.path.join(ROOT, "cours", "episodes.json")
BASE = g.BASE_URL


def load():
    if os.path.exists(EPISODES):
        with open(EPISODES, encoding="utf-8") as f:
            return json.load(f)
    return []


def make_audio(text, dest):
    silence = b"\x00\x00" * int(24000 * 0.6)
    pcm = b""
    skipped = 0
    for i, c in enumerate(g.chunks(text)):
        g.log(f"voix, morceau {i + 1} ({len(c)} caractères)")
        try:
            pcm += g.tts_chunk(c) + silence
        except Exception as e:
            # Morceau refusé ou en échec : on réessaie paragraphe par paragraphe.
            g.log(f"morceau {i + 1} en échec ({str(e)[:200]}), découpage en paragraphes")
            for j, para in enumerate([p for p in c.split("\n\n") if p.strip()]):
                time.sleep(10)
                try:
                    pcm += g.tts_chunk(para) + silence
                except Exception as e2:
                    skipped += 1
                    if skipped > 3:
                        raise RuntimeError("trop de paragraphes en échec, épisode abandonné") from e2
                    g.log(f"PARAGRAPHE IGNORÉ (morceau {i + 1}, paragraphe {j + 1}) : {str(e2)[:200]} :: {para[:120]}")
        time.sleep(22)
    wav = dest + ".wav"
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-af", "loudnorm=I=-16:TP=-1.5",
                    "-ar", "44100", "-ac", "1", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", dest], check=True)
    os.remove(wav)
    return len(pcm) / 48000


def feed(eps):
    items = ""
    for e in sorted(eps, key=lambda x: x["pub"], reverse=True):
        items += f"""<item>
<title>{html.escape(e['title'])}</title>
<description>{html.escape(e['title'])}</description>
<enclosure url="{BASE}{e['file']}" length="{e['size']}" type="audio/x-m4a"/>
<guid isPermaLink="false">cours-{e['slug']}</guid>
<pubDate>{email.utils.formatdate(e['pub'])}</pubDate>
<itunes:duration>{int(e['secs'] // 60)}:{int(e['secs'] % 60):02d}</itunes:duration>
</item>
"""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<atom:link href="{BASE}feed-cours.xml" rel="self" type="application/rss+xml"/>
<title>Les cours de Jean</title>
<link>{BASE}</link>
<language>fr-fr</language>
<description>Cours audio personnels : tech, IA et projets.</description>
<itunes:author>Les cours de Jean</itunes:author>
<image><url>{BASE}cover-cours.png</url><title>Les cours de Jean</title><link>{BASE}</link></image>
<itunes:image href="{BASE}cover-cours.png"/>
<itunes:explicit>false</itunes:explicit>
<itunes:category text="Education"/>
<itunes:owner><itunes:name>Jean</itunes:name><itunes:email>briefing-bot@users.noreply.github.com</itunes:email></itunes:owner>
<itunes:type>episodic</itunes:type>
{items}</channel>
</rss>
"""
    with open(os.path.join(SITE, "feed-cours.xml"), "w", encoding="utf-8") as f:
        f.write(xml)


def main():
    os.makedirs(SITE, exist_ok=True)
    eps = load()
    done = {e["slug"] for e in eps}
    for name in sorted(os.listdir(TEXTES)):
        if not name.endswith(".txt"):
            continue
        slug = name[:-4]
        if slug in done:
            continue
        with open(os.path.join(TEXTES, name), encoding="utf-8") as f:
            raw = f.read().strip()
        title, _, body = raw.partition("\n")
        body = body.strip()
        fname = f"cours-{slug}.m4a"
        g.log("épisode :", title, len(body.split()), "mots")
        secs = make_audio(body, os.path.join(SITE, fname))
        eps.append({"slug": slug, "title": title.strip(), "file": fname, "secs": round(secs),
                    "size": os.path.getsize(os.path.join(SITE, fname)), "pub": int(time.time())})
        with open(EPISODES, "w", encoding="utf-8") as f:
            json.dump(eps, f, ensure_ascii=False, indent=1)
        g.log(f"ok : {secs / 60:.1f} minutes")
    feed(eps)


if __name__ == "__main__":
    main()
