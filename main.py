import os
import re
import json
from groq import Groq
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

app = FastAPI(title="BSWE URL Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)


class UrlRequest(BaseModel):
    url: str


PROMPT_TEMPLATE = """Analysiere diesen Text einer Ferienhaus-Website und extrahiere folgende Informationen als JSON.
Antworte ausschließlich mit gültigem JSON, ohne Markdown-Codeblöcke, ohne Erklärungen.

{
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
}

Regeln:
- price: Gesamtpreis in Euro als Zahl, null wenn unklar
- beds: Anzahl Schlafplätze als Zahl, null wenn unklar
- sauna: true/false/null
- bigTable: Großer Esstisch (>=6 Personen), true/false/null
- sqm: Wohnfläche in m² als Zahl, null wenn unklar
- trainStation: Nächster Bahnhof mit Entfernung, null wenn unbekannt
- rooms: Array von Zimmertypen
- notes: Max. 120 Zeichen, nur relevante Hinweise
- photos: Absolute URLs zu echten Fotos der Unterkunft (max. 6), leeres Array wenn keine

Seiteninhalt:
{text}"""


def extract_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:15000]


async def fetch_with_playwright(url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", None),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            # Kurz warten damit lazy-loaded Inhalte sichtbar werden
            await page.wait_for_timeout(2000)
            content = await page.content()
        finally:
            await browser.close()
        return content


@app.post("/analyze-url")
async def analyze_url(req: UrlRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Ungültige URL")

    try:
        html = await fetch_with_playwright(url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Seite nicht erreichbar: {str(e)}")

    text = extract_text(html)
    prompt = PROMPT_TEMPLATE.replace("{text}", text)

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1024,
        )
        result = json.loads(completion.choices[0].message.content.strip())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Kein gültiges JSON zurückbekommen")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Groq-Fehler: {str(e)}")

    return result


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
