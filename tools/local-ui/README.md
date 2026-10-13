# SLM Extraction Bench (local test UI)

A browser page for trying document extraction on your own machine. **Local testing only**: it binds to `127.0.0.1`, has no login, and is not part of the deployed platform.

## Two modes

| Mode | What happens | Saved? |
|---|---|---|
| **Local server (default)** | The page uploads the file to `POST /v1/extractions`. The FastAPI server prepares it, calls Ollama, checks and masks the result, and returns it | Yes, under `data/jobs/<job_id>/` |
| **Model directly (Ollama)** | The page prepares the document in the browser and calls Ollama itself. Useful to see exactly what the model receives | No |

## Run

The easiest way: start the API server and open http://localhost:8000. It serves this page at `/ui`.

```bash
uv run uvicorn app.asgi:app --port 8000
```

For "Model directly" mode without the API server, serve the page on its own:

```bash
tools/local-ui/fetch-vendor.sh       # verifies the bundled pdf.js and mammoth.js
python3 tools/local-ui/serve.py      # http://localhost:8090
```

Ollama accepts requests from `http://localhost:*` pages by default, so no extra setting is needed.

## What each tab shows

| Tab | Content |
|---|---|
| Fields | The extracted values as tables, masked like the platform (Aadhaar always) |
| JSON | The same result as JSON, with a copy button |
| Model input | The route taken, the text and the images that were sent to the model |
| Run details | Timings, token counts, output speed; the raw output and prompt when something fails |

## Files

| File | Purpose |
|---|---|
| `index.html`, `styles.css`, `app.js` | The page |
| `schemas.js` | The five document types, consistency checks and masking rules (mirrors requirements FR-3 and FR-4) |
| `serve.py` | Local server on 127.0.0.1 |
| `fetch-vendor.sh`, `vendor.sha256` | Download and verify pdf.js 4.10.38 (Apache-2.0) and mammoth 1.8.0 (BSD-2-Clause) |

## Data handling

- Documents are sent only to `localhost` (the local server or Ollama).
- The page keeps its run list in memory only; in Local server mode the server also saves each result under `data/jobs/`.
- Settings are saved in the browser.

## Limits compared with the platform

- In Model directly mode, DOCX headers and footers are not read (the server reads them).
- In Local server mode the server does masking and checks; in Model directly mode the browser does them.
