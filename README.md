# Briefing du matin

Chaque nuit, GitHub Actions :
1. récupère des données gratuites (météo Open-Meteo, flux RSS d'actualité, Yahoo Finance, CoinGecko, Nominis, Wikipédia) ;
2. fait rédiger un script d'environ 11 minutes par Gemini ;
3. le fait lire par une voix d'animateur (Gemini TTS) ;
4. publie `briefing.m4a` et `briefing.txt` sur GitHub Pages.

- Lancer à la main : onglet **Actions** → *Briefing du matin* → **Run workflow**.
- Changer de voix : *Settings → Secrets and variables → Actions → Variables* → `VOICE` (ex. `Puck`, `Charon`, `Fenrir`, `Orus`, `Kore`).
- `history.json` garde les mots de vocabulaire, les sujets et les actions déjà traités.

## Musique d'intro
Chaque matin, un morceau disco-funk/house tiré au hasard dans `music/` ouvre le briefing : montée très douce sur 45 secondes, puis la voix entre par-dessus et la musique s'efface progressivement en fond (façon radio).
Morceaux de **Kevin MacLeod (incompetech.com)**, sous licence Creative Commons Attribution 4.0 : http://creativecommons.org/licenses/by/4.0/
