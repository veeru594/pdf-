# OfferVerify — Quick Start

## 1. Configure

Add your Anthropic API key to `.env`:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

## 2. Start the server

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8003
```

## 3. Open the UI

[http://localhost:8003](http://localhost:8003)

Upload a PDF offer letter → click **Analyze** → view results.

---

## Endpoints

| Endpoint   | Method | Purpose          |
|------------|--------|------------------|
| `/`        | GET    | Web UI           |
| `/health`  | GET    | Health check     |
| `/analyze` | POST   | Analyze PDF      |
| `/docs`    | GET    | Swagger API docs |

---

## Docker

```powershell
docker build -t offerverify .
docker run -p 8003:8003 --env-file .env offerverify
```

---

## Troubleshooting

| Error | Fix |
|---|---|
| Port already in use | `taskkill /F /IM python.exe /T` |
| ModuleNotFoundError | `.\.venv\Scripts\pip.exe install -r requirements.txt` |
| API Key not set | Add `ANTHROPIC_API_KEY` to `.env` |

---

## Architecture

```
Browser → FastAPI (main.py)
              ├─ pdf_reader.py   — text, images, metadata, page renders
              ├─ ai_client.py    — Claude API: field extraction + scoring
              ├─ checker.py      — DNS + company online presence
              └─ rules.py        — pillar scores, verdict, hard gate
```
