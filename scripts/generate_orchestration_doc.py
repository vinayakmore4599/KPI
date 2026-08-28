#!/usr/bin/env python3
"""Generate docs/KPI-Engine-Orchestration-Guide.docx from markdown source."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from docx import Document  # noqa: E402
from docx_helpers import add_title_page, configure_document  # noqa: E402
from generate_onboarding_docs import markdown_to_docx  # noqa: E402

SOURCE = ROOT / "docs" / "kpi-engine-orchestration-guide.md"
OUTPUT = ROOT / "docs" / "KPI-Engine-Orchestration-Guide.docx"
TITLE = "KPI Engine — Orchestration Flow and Design"
SUBTITLE = "How context JSON and YAML formulas become JSON results"


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)

    doc = Document()
    configure_document(doc)
    add_title_page(
        doc,
        TITLE,
        SUBTITLE,
        audience="Architecture team, tech leads, platform engineers",
        version=date.today().strftime("%B %Y"),
    )
    markdown_to_docx(SOURCE, doc)
    doc.save(OUTPUT)
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
