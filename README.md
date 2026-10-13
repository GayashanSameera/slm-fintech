# SLM Fintech Platform

Multi-tenant API that extracts structured JSON from financial documents (invoices, bank statements, payslips, identity documents, KYC forms) using a small vision-language model hosted in our own AWS account (ap-south-1, Mumbai). A chatbot API is planned for a later phase.

## Status

Design complete, implementation not started. A local test UI is available for trying the model on real documents.

## Start here

| | |
|---|---|
| [docs/requirements.md](docs/requirements.md) | What the platform must do |
| [docs/architecture.md](docs/architecture.md) | How it is built on AWS |
| [docs/processing-flow.md](docs/processing-flow.md) | What happens to each file type |
| [docs/implementation-guide.md](docs/implementation-guide.md) | Build order, phases, runbooks |
| [docs/local-setup.md](docs/local-setup.md) | Run it on your machine |
| [docs/cost.md](docs/cost.md) | Monthly AWS cost at 1,000 pages/day |
| [tools/local-ui/](tools/local-ui/) | SLM Extraction Bench: upload a document, see the extracted JSON |

## Try the model locally

```bash
sudo systemctl start docker
docker run -d --name slm-ollama -p 127.0.0.1:11434:11434 -v slm-ollama:/root/.ollama \
  -e OLLAMA_CONTEXT_LENGTH=16384 -e OLLAMA_NUM_PARALLEL=1 -e OLLAMA_KEEP_ALIVE=30m \
  --restart unless-stopped ollama/ollama:0.12.7
docker exec slm-ollama ollama pull qwen3-vl:4b
python3 tools/local-ui/serve.py        # open http://localhost:8090
```

Never commit real documents. See `.gitignore`.
