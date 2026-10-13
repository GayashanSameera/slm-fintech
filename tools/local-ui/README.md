# SLM Extraction Bench (local test UI)

A browser page for trying document extraction on your own machine. **Local testing only**: it binds to `127.0.0.1`, has no login, and is not part of the deployed platform.

## Two modes

| Mode | What happens | Available |
|---|---|---|
| **Model directly (Ollama)** | The page prepares the document in your browser (PDF text or page images, DOCX text, resized photos), sends it to Ollama with the JSON schema, then runs the consistency checks and masking | Now |
| **Platform API** | The page uploads the file to `POST /v1/extractions?wait=40` and calls `GET …?wait=40` again on `202` | After phase 0 is built |

## Run

```bash
# 1. Ollama with the model (see docs/local-setup.md, section 3)
docker start slm-ollama              # or the docker run command from the guide

# 2. Libraries (already present after checkout; re-run to restore or verify them)
tools/local-ui/fetch-vendor.sh

# 3. Serve the page
python3 tools/local-ui/serve.py      # http://localhost:8090
```

Open http://localhost:8090, choose a file and a document type, select **Extract**.

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

- Documents are read in the browser and sent only to `localhost`.
- Results are kept in memory for the open tab only. Reloading clears them.
- Settings are saved in the browser; the platform API key is kept for the current tab only.

## Limits compared with the platform

- DOCX headers and footers are not read.
- Masking and checks run in the browser in Ollama mode; in Platform API mode the server's results are shown as returned.
