"""Scanned versions of real-layout statements: 25 different bank layouts, truth proven by the PDF text.

    AWS_PROFILE=lyndev uv run python -m eval.real_scans --dir ~/Downloads/sample_bank_statement_documents_12_layouts

For every sample PDF that the deterministic parser accepts (so its rows were reconciled with the
PDF's own text, line by line, and are TRUSTED), the pages are rendered to images, degraded like a
phone scan, and fed to the scanned-statement path (page-by-page AI reading + second reading).
The parser's rows are the answer key. This tests the AI path on layouts nobody tuned it for.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import io
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _aws import prefer_ipv4  # noqa: E402

prefer_ipv4()

import pypdfium2 as pdfium  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from app.gateway.bedrock import BedrockGateway  # noqa: E402
from app.gateway.fake import FakeGateway  # noqa: E402
from app.modules.extraction.parsers import try_rung0  # noqa: E402
from app.modules.extraction.preprocess import Prepared, preprocess  # noqa: E402
from app.modules.extraction.preprocess.image import prepare_image  # noqa: E402
from app.modules.extraction.profiles import load_profile  # noqa: E402

from eval.generate import Case  # noqa: E402
from eval.run import cost_breakdown, run_case, table  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402


def degrade(img: Image.Image, rng: random.Random, hard: bool) -> bytes:
    """Rotate a little, blur, add noise and JPEG-compress a rendered page, like a phone scan."""
    img = img.convert("L").rotate(rng.uniform(-1.2, 1.2) * (2 if hard else 1), resample=Image.BICUBIC, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(0.9 if hard else 0.5))
    img = Image.blend(img, Image.effect_noise(img.size, 22 if hard else 12), 0.10 if hard else 0.05)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=55 if hard else 72)
    return out.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--hard", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--variants", default="current")
    args = ap.parse_args()

    base = load_profile("default")
    cases = []
    for path in sorted(glob.glob(os.path.join(os.path.expanduser(args.dir), "*.pdf")))[: args.limit or None]:
        data = open(path, "rb").read()
        prepared = preprocess(data)
        outcome, _calls, _tried = try_rung0(data=data, prepared=prepared, gateway=FakeGateway(), verifier_model="x",
                                            verification_mode="checks_only", job_id="key")
        if outcome is None:
            print("skipped (parser declines, so no trusted answer key):", os.path.basename(path)[:40])
            continue
        truth = outcome.statement.model_dump(mode="json")
        # Render each page at ~150 dpi and degrade it.
        pdf = pdfium.PdfDocument(data)
        rng = random.Random(os.path.basename(path))
        pages = []
        for i in range(len(pdf)):
            image = pdf[i].render(scale=150 / 72).to_pil()
            pages.append({"text": None, "image": prepare_image(degrade(image, rng, args.hard)), "scanned": True})
        case = Case(os.path.basename(path)[:36], "bank_statement", "scan", data, truth)
        case.prepared = Prepared(kind="png", pages=pages)
        cases.append(case)
    print(f"{len(cases)} scanned statements built")

    gateway = BedrockGateway()
    variants = {"chunk3": dataclasses.replace(base, crossread_enabled=False, scan_chunk_pages=3),
                "off": dataclasses.replace(base, crossread_enabled=False),
                "current": base}
    for name in [v.strip() for v in args.variants.split(",")]:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            scores = list(pool.map(lambda c: run_case(c, variants[name], gateway), cases))
        print(f"\n## Variant: {name}\n{table(scores)}\n\n{cost_breakdown(scores)}")
        for s in scores:
            if s.has_error:
                print(f"  - {s.case_id}: wrong={s.wrong[:6]} (+{max(len(s.wrong) - 6, 0)}) invented={s.invented[:3]} "
                      f"warnings={s.warnings[:4]} failed={s.failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
