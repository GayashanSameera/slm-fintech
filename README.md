# SLM Fintech: Local Proof of Concept

Extracts structured JSON from financial documents (invoices, bank statements, payslips, identity documents, KYC forms) with a small vision-language model running on your own machine. No cloud, no AWS, no data leaves the machine.

```
Browser UI ──upload──► FastAPI (localhost:8000) ──► Ollama + Qwen3-VL 4B (localhost:11434)
                              │
                              └──► ./data/jobs/<job_id>/  input.pdf · result.json · job.json
```

## Run it

```bash
./start.sh            # starts the AI model and the server, opens http://localhost:8000
```

Docker must be running (`sudo systemctl start docker`) and [uv](https://docs.astral.sh/uv/) installed. The first start downloads the model (3.3 GB). Press `Ctrl+C` to stop.

**How it all works:** [docs/local-poc.md](docs/local-poc.md). Installation and troubleshooting: [docs/local-setup.md](docs/local-setup.md).

## What it does with each upload

1. Detects the real file type from its bytes (PDF, DOCX, JPG, PNG).
2. Turns it into what the model reads: text for digital PDFs and Word files, page images for scans and photos.
3. Asks the model for JSON that must match the document type's schema.
4. Validates every field (Indian date and amount formats are normalised), runs arithmetic checks, and masks sensitive numbers (Aadhaar always).
5. Saves the file and the result under `data/jobs/<job_id>/` and returns the JSON.

## Code map

| Path | What it is |
|---|---|
| `app/modules/extraction/` | API routes, the pipeline, preprocessing per file type, schemas, masking |
| `app/gateway/` | The only code that talks to the model (Ollama's OpenAI-compatible API) |
| `app/storage/local.py` | Reads and writes `data/jobs/` |
| `tools/local-ui/` | The browser test page, served at `/ui` |
| `tests/` | 65 tests; run with `uv run pytest` (no Docker or model needed) |

## Design documents

The documents in [docs/](docs/) describe the full multi-tenant platform planned for AWS (requirements, architecture, processing flow, cost, implementation plan). This repository currently contains only the local proof of concept.

Never commit real documents: `data/` is in `.gitignore`.
