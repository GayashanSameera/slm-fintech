"""Identity document schema (requirement FR-3.4).

Aadhaar numbers are ALWAYS masked (FR-4.1). Identity documents also get the
strict rule FR-4.2: ANY 12-digit Aadhaar-like number in ANY field is masked.
Identity documents are never kept for training by default (FR-6.2).
"""

from __future__ import annotations

from typing import Literal

from .base import Address, DocBase, IsoDate, Text

SCHEMA_VERSION = "1.0"

IdType = Literal["aadhaar", "pan", "passport", "driving_licence", "voter_id", "other"]


class IdentityDocument(DocBase):
    id_type: IdType | None = None
    id_number: Text = None
    name: Text = None
    father_or_spouse_name: Text = None
    date_of_birth: IsoDate = None
    gender: Text = None
    address: Address | None = None
    issue_date: IsoDate = None
    expiry_date: IsoDate = None
    issuer: Text = None
