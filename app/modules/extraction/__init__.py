"""Document extraction: from an uploaded file to validated, masked JSON.

Files in this package, in the order a document passes through them
(docs/processing-flow.md shows the same flow as diagrams):

    api.py          HTTP routes: POST /v1/extractions, GET, list, delete, document types
    service.py      one upload end to end: create job, save file, run pipeline, save result
    pipeline.py     the steps below, with no storage or HTTP concerns
      preprocess/   detect the real file type; turn PDF/DOCX/JPG/PNG into text and/or images
      prompts.py    the instructions and document content sent to the model
      (app/gateway) the call to the AI model
      schemas/      what each document type must contain; consistency checks
      masking.py    hide sensitive numbers (Aadhaar always)
    models.py       the Job record and API response shapes
    errors.py       ExtractionError and its codes
"""
