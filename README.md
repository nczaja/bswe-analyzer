# BSWE URL Analyzer — Backend

Kleines FastAPI-Backend, das eine Unterkunfts-URL entgegennimmt, die Seite fetcht und per Claude API strukturierte Daten extrahiert.

## Lokal testen

```bash
cd bswe-analyzer
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# API-Key setzen
export ANTHROPIC_API_KEY=sk-ant-...

uvicorn main:app --reload
```

Testen:
```bash
curl -X POST http://localhost:8000/analyze-url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.traum-ferienwohnungen.de/73900/"}'
```

## Auf Railway deployen

1. **GitHub-Repo anlegen** und diesen `bswe-analyzer/`-Ordner pushen (oder das ganze Projekt-Repo).

2. Auf [railway.app](https://railway.app) einloggen → **New Project → Deploy from GitHub Repo** → Repo auswählen.

3. Railway erkennt Python automatisch. Unter **Settings → Start Command** eintragen:
   ```
   uvicorn main:app --host 0.0.0.0 --port $PORT
   ```

4. Unter **Variables** die Umgebungsvariable setzen:
   ```
   ANTHROPIC_API_KEY = sk-ant-...
   ```

5. Deploy abwarten. Railway zeigt eine URL nach dem Muster `https://deine-app.railway.app`.

6. Diese URL in `bswe-planner.html` eintragen:
   ```js
   const ANALYZER_URL = 'https://deine-app.railway.app/analyze-url';
   ```

## Endpunkte

| Methode | Pfad | Beschreibung |
|---------|------|--------------|
| POST | `/analyze-url` | URL analysieren, JSON zurückgeben |
| GET | `/health` | Healthcheck für Railway |

## Rückgabe-Schema

```json
{
  "name": "Ferienhaus Waldblick",
  "price": 490,
  "beds": 6,
  "sauna": false,
  "bigTable": true,
  "sqm": 180,
  "location": "Kellerwald, Hessen",
  "trainStation": "Treysa (12 km)",
  "rooms": ["2× Doppelzimmer", "1× Einzelzimmer"],
  "notes": "Kaminholz inklusive. Parkplatz.",
  "photos": ["https://..."]
}
```

Felder können `null` sein, wenn die Info auf der Seite nicht gefunden wurde.
