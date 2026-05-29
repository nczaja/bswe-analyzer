import os
import re
import json
import httpx
import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-1.5-flash")

app = FastAPI(title="BSWE URL Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In Produktion auf deine Domain einschränken
    allow_methods=["POST"],
    allow_headers=["*"],
)


class UrlRequest(BaseModel):
    url: str


PROMPT_TEMPLATE = """Analysiere diesen Text einer Ferienhaus-Website und extrahiere folgende Informationen als JSON.
Antworte ausschließlich mit gültigem JSON, ohne Markdown-Codeblöcke, ohne Erklärungen.

{{
  "name": "Name der Unterkunft",
  "price": 450,
  "beds": 6,
  "sauna": true,
  "bigTable": true,
  "sqm": 160,
  "location": "Waldbröl, NRW",
  "trainStation": "Waldbröl (8 km)",
  "rooms": ["2× Doppelzimmer", "1× Einzelzimmer"],
  "notes": "Kamin vorhanden. Parkplatz inklusive.",
  "photos": ["https://..."]
}}

Regeln:
- price: Gesamtpreis in Euro als Zahl, null wenn unklar
- beds: Anzahl Schlafplätze als Zahl, null wenn unklar
- sauna: true/false/null
- bigTable: Großer Esstisch (≥6 Personen), true/false/null
- sqm: Wohnfläche in m² als Zahl, null wenn unklar
- trainStation: Nächster Bahnhof mit Entfernung, null wenn unbekannt
- rooms: Array von Zimmertypen
- notes: Max. 120 Zeichen, nur relevante Hinweise
- photos: Absolute URLs zu Fotos (max. 6), leeres Array wenn keine

Seiteninhalt:
{text}"""


def extract_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:15000]


@app.post("/analyze-url")
async def analyze_url(req: UrlRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Ungültige URL")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as http:
            response = await http.get(url, headers=headers)
            response.raise_for_status()
            html = response.text
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Seite nicht erreichbar: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Fetch fehlgeschlagen: {str(e)}")

    text = extract_text(html)
    prompt = PROMPT_TEMPLATE.replace("{text}", text)

    try:
        response = model.generate_content(prompt)
        result = json.loads(response.text.strip())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Kein gültiges JSON zurückbekommen")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini-Fehler: {str(e)}")

    return result


@app.get("/health")
async def health():
    return {"status": "ok"}
