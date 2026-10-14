"""Reserved chat routes (requirement FR-8.1).

They exist, require authentication, and return 501 Not Implemented. When the
chatbot is built, it will reuse the same model gateway, tenancy and storage
layers (FR-8.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import require_scope
from app.auth.principal import Principal
from app.core.errors import ApiError

router = APIRouter()


def _not_implemented():
    raise ApiError(501, "not_implemented", "The chat API is not available yet.")


@router.post("/chat/sessions")
def create_session(principal: Principal = Depends(require_scope("extractions:read"))):
    _not_implemented()


@router.post("/chat/sessions/{session_id}/messages")
def send_message(session_id: str, principal: Principal = Depends(require_scope("extractions:read"))):
    _not_implemented()
