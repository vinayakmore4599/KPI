#!/usr/bin/env python3
"""Generate docs/hooks-vs-ops-guide.docx from docs/hooks-vs-ops-guide.md."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from docx import Document  # noqa: E402
from docx_helpers import add_title_page, configure_document  # noqa: E402
from generate_onboarding_docs import markdown_to_docx  # noqa: E402

SOURCE = ROOT / "docs" / "hooks-vs-ops-guide.md"
OUTPUT = ROOT / "docs" / "hooks-vs-ops-guide.docx"
TITLE = "Hooks vs Ops — Conceptual Guide"
SUBTITLE = "When to use ops, hooks, and the hook op bridge"


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)

    doc = Document()
    configure_document(doc)
    add_title_page(doc, TITLE, SUBTITLE, version=date.today().strftime("%B %Y"))
    markdown_to_docx(SOURCE, doc)
    doc.save(OUTPUT)
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
