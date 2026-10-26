"""Diagnostic : teste chaque modèle Gemini avec une petite requête et écrit diag.json."""
import json, os, urllib.request, urllib.error, base64
KEY = os.environ["GEMINI_API_KEY"]
def call(model, body):
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", "x-goog-api-key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            j = json.loads(r.read())
        c = j.get("candidates", [{}])[0]
        parts = c.get("content", {}).get("parts", [])
        audio = sum(len(base64.b64decode(p["inlineData"]["data"])) for p in parts if "inlineData" in p)
        text = "".join(p.get("text", "") for p in parts)
        return {"ok": bool(parts), "finish": c.get("finishReason"), "audio_s": round(audio / 48000, 1), "text": text[:120],
                "feedback": j.get("promptFeedback"), "usage": j.get("usageMetadata")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "err": e.read()[:300].decode(errors="replace")}
    except Exception as e:
        return {"ok": False, "err": str(e)[:300]}
out = {}
for m in ["gemini-flash-latest", "gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-flash-lite-latest"]:
    out[m] = call(m, {"contents": [{"parts": [{"text": "Dis bonjour en une phrase."}]}]})
STYLE = open("tools/style.txt").read() if os.path.exists("tools/style.txt") else ""
txt = "Bonjour Jean ! Il est sept heures, et aujourd'hui à Paris, on attend un grand soleil."
for m in ["gemini-3.1-flash-tts-preview", "gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-2.5-flash-preview-tts"]:
    for label, prompt in (("brut", txt), ("court", "Dis avec enthousiasme, comme un animateur radio : " + txt)):
        out[f"{m} [{label}]"] = call(m, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {
            "responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Puck"}}}}})
json.dump(out, open("diag.json", "w"), ensure_ascii=False, indent=1)
