"""SLM Fintech local proof of concept: extract JSON from financial documents.

Layout:
    app/config.py        settings from SLM_* environment variables
    app/main.py          builds the FastAPI application
    app/asgi.py          entry point for the web server (uvicorn)
    app/deps.py          shared objects (settings, job store, the AI model)
    app/core/            cross-cutting pieces: errors, logging, ids
    app/storage/         reads and writes the data folder (files on disk)
    app/gateway/         talks to the AI model (Ollama)
    app/modules/         features: extraction
"""
