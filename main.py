import os
import re
import json
import html as html_lib
import asyncio
from xml.etree import ElementTree as ET
from groq import Groq
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"]) if "GROQ_API_KEY" in os.environ else None

app = FastAPI(title="BSWE URL Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
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


def extract_photos(html: str, base_url: str = "") -> list[str]:
    """Extract image URLs from rendered HTML before tag stripping."""
    # Find all img src / data-src / data-lazy-src / data-original attributes
    srcs = re.findall(
        r'<img[^>]+(?:src|data-src|data-lazy-src|data-original|data-lazy|data-image)=["\']([^"\']+)["\']',
        html, flags=re.IGNORECASE
    )
    # Also catch srcset (first URL only)
    srcsets = re.findall(r'<img[^>]+srcset=["\']([^\s"\']+)', html, flags=re.IGNORECASE)
    srcs += srcsets
    photos = []
    for src in srcs:
        # Skip tiny images, icons, SVGs, data URIs, tracking pixels
        if src.startswith("data:"):
            continue
        lower = src.lower()
        if any(x in lower for x in ["icon", "logo", "pixel", "spinner", "avatar", "flag", ".svg", "1x1", "blank"]):
            continue
        # Make absolute URL
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/") and base_url:
            from urllib.parse import urlparse
            parsed = urlparse(base_url)
            src = f"{parsed.scheme}://{parsed.netloc}{src}"
        if src.startswith("http"):
            photos.append(src)
    # Deduplicate preserving order, limit to 8
    seen = set()
    unique = []
    for p in photos:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:8]


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
            # Scroll to trigger lazy loading
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await page.wait_for_timeout(1500)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(1000)
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

    photos = extract_photos(html, url)
    text = extract_text(html)
    prompt = PROMPT_TEMPLATE.replace("{text}", text)

    if not groq_client:
        raise HTTPException(status_code=503, detail="GROQ_API_KEY nicht konfiguriert")
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

    # Override photos with directly extracted ones (more reliable than LLM extraction)
    if photos:
        result["photos"] = photos

    return result


BGG_BASE = "https://boardgamegeek.com/xmlapi2"


async def bgg_fetch(url: str) -> str:
    """Navigate to BGG XML API URL with real browser to bypass Cloudflare."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", None),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if response and response.status == 202:
                return "__RETRY__"
            body = await response.body()
            return body.decode("utf-8", errors="replace")
        finally:
            await browser.close()


@app.get("/bgg/search")
async def bgg_search(q: str = Query(..., min_length=1)):
    try:
        url = f"{BGG_BASE}/search?query={q}&type=boardgame"
        xml_text = await bgg_fetch(url)
        root = ET.fromstring(xml_text)
        results = []
        for item in root.findall("item"):
            name_el = item.find("name[@type='primary']") or item.find("name")
            year_el = item.find("yearpublished")
            results.append({
                "id": item.get("id"),
                "name": name_el.get("value") if name_el is not None else "?",
                "year": year_el.get("value") if year_el is not None else None,
            })
        results.sort(key=lambda x: int(x["year"] or 0), reverse=True)
        return results[:15]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"BGG search error: {type(e).__name__}: {e}")


@app.get("/bgg/thing")
async def bgg_thing(id: str = Query(...)):
    try:
        xml_text = "__RETRY__"
        for attempt in range(3):
            url = f"{BGG_BASE}/thing?id={id}&stats=1&videos=1"
            xml_text = await bgg_fetch(url)
            if xml_text == "__RETRY__":
                await asyncio.sleep(2)
                continue
            break
        if xml_text == "__RETRY__":
            raise HTTPException(status_code=502, detail="BGG returned 202 after 3 retries")
        root = ET.fromstring(xml_text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"BGG thing error: {type(e).__name__}: {e}")
    item = root.find("item")
    if item is None:
        raise HTTPException(status_code=404, detail="Spiel nicht gefunden")

    name_el = item.find("name[@type='primary']")
    name = name_el.get("value") if name_el is not None else "?"

    desc_el = item.find("description")
    desc = html_lib.unescape(desc_el.text or "") if desc_el is not None else ""
    if len(desc) > 420:
        desc = desc[:420].rsplit(" ", 1)[0] + "…"

    def img(tag):
        t = (item.findtext(tag) or "").strip()
        return ("https:" + t) if t.startswith("//") else t

    def intval(tag):
        el = item.find(tag)
        if el is None:
            return None
        try:
            return int(el.get("value", 0))
        except Exception:
            return None

    minp = intval("minplayers")
    maxp = intval("maxplayers")
    mintime = intval("minplaytime") or 0
    maxtime = intval("maxplaytime") or 0
    playtime = maxtime or mintime
    duration_cat = "kurz" if playtime <= 90 else ("mittel" if playtime <= 180 else "lang")

    # Best player count from community poll
    best_players = None
    poll = item.find("poll[@name='suggested_numplayers']")
    if poll is not None:
        best_count = 0
        for results_el in poll.findall("results"):
            num = results_el.get("numplayers", "")
            if "+" in num:
                continue
            for res in results_el.findall("result"):
                if res.get("value") == "Best":
                    votes = int(res.get("numvotes", 0))
                    if votes > best_count:
                        best_count = votes
                        best_players = num

    # Rating
    rating = None
    avg_el = item.find("statistics/ratings/average")
    if avg_el is not None:
        try:
            rating = round(float(avg_el.get("value", 0)), 1)
        except Exception:
            pass

    # Instructional video preferred
    video_url = None
    videos_el = item.find("videos")
    if videos_el is not None:
        first = None
        for v in videos_el.findall("video"):
            link = v.get("link", "")
            cat = v.get("category", "").lower()
            if not first:
                first = link
            if cat == "instructional":
                video_url = link
                break
        if not video_url:
            video_url = first

    return {
        "id": id,
        "name": name,
        "description": desc,
        "thumbnail": img("thumbnail"),
        "image": img("image"),
        "minPlayers": minp,
        "maxPlayers": maxp,
        "bestPlayers": best_players,
        "minPlaytime": mintime,
        "maxPlaytime": maxtime,
        "durationCat": duration_cat,
        "rating": rating,
        "videoUrl": video_url,
        "rulesUrl": f"https://boardgamegeek.com/boardgame/{id}/files",
        "bggUrl": f"https://boardgamegeek.com/boardgame/{id}",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
