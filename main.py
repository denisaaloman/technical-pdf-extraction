"""
Aplicatia principala FastAPI:
  POST /api/extract  — primeste 1 PDF, il randeaza pagina cu pagina, extrage
                        tabelele via Groq vision, uneste tabelele cu acelasi
                        titlu aparute pe pagini diferite.
  POST /api/export    — primeste rezultatele deja extrase (din frontend) +
                        formatul dorit, returneaza fisierul CSV/XLSX.
  /                   — serveste frontend-ul static (index.html, styles.css, app.js).

Paginile se proceseaza SECVENTIAL (nu in paralel), la fel ca in varianta
Node: Groq (planul gratuit) are o limita stricta de tokens/minut, si
paralelizarea ar insemna doar sa lovim limita mai repede. Retry-ul cu
backoff pe rate limit e in extraction.py.
"""

import os
from typing import Any, Dict, List, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import io

from extraction import extract_tables_from_page, render_pdf_pages
from export_utils import build_wide_rows, to_csv, to_xlsx

import os
from dotenv import load_dotenv

load_dotenv()
app = FastAPI(title="Extractor Tabele Tehnice")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)) -> Dict[str, Any]:
    if not GROQ_API_KEY:
        raise HTTPException(
            status_code=500, detail="GROQ_API_KEY is not configured on the server."
        )

    filename = file.filename or "document.pdf"
    buffer = await file.read()

    result: Dict[str, Any] = {"filename": filename, "status": "no_tables", "tables": []}

    try:
        print(f"[{filename}] Rendering PDF pages...")
        pages = render_pdf_pages(buffer)
        print(f"[{filename}] Rendered {len(pages)} page(s). Starting extraction...")

        for i, page_image in enumerate(pages):
            try:
                tables = extract_tables_from_page(page_image, GROQ_API_KEY)
                print(f"[{filename}] Page {i + 1}/{len(pages)}: {len(tables)} table(s) found")
            except Exception as err:  # noqa: BLE001
                print(f"[{filename}] Page {i + 1}/{len(pages)} FAILED: {err}")
                tables = []

            #uneste tabelele cu acelasi titlu aparute pe pagini diferite
            #(un tabel poate continua pe pagina urmatoare).
            for t in tables:
                existing = next(
                    (
                        rt
                        for rt in result["tables"]
                        if rt["title"] == t["title"] and t["title"] != ""
                    ),
                    None,
                )
                if existing:

                    for h in t["headers"]:
                        if h not in existing["headers"]:
                            existing["headers"].append(h)
                    existing["rows"].extend(t["rows"])
                else:
                    result["tables"].append(t)

        print(f"[{filename}] Done - {len(result['tables'])} table(s) total.")
        result["status"] = "success" if result["tables"] else "no_tables"
    except Exception as err:
        result["status"] = "error"
        result["errorMessage"] = str(err)
        print(f"[{filename}] Extraction failed: {err}")

    return result


class ExportBody(BaseModel):
    results: List[Dict[str, Any]]
    format: Literal["csv", "xlsx"] = "xlsx"


@app.post("/api/export")
async def export(body: ExportBody):
    columns, rows = build_wide_rows(body.results)

    if body.format == "csv":
        csv_text = to_csv(columns, rows)
        return StreamingResponse(
            io.BytesIO(csv_text.encode("utf-8")),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=technical_tables.csv"},
        )

    xlsx_bytes = to_xlsx(columns, rows)
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=technical_tables.xlsx"},
    )


if not os.path.isdir(STATIC_DIR):
    raise RuntimeError(
        f"Nu gasesc folderul 'static' la calea asteptata: {STATIC_DIR}\n"
        f"Verifica sa ai index.html, styles.css si app.js chiar in acel folder, "
        f"langa main.py (nu intr-un subfolder gresit sau intr-un folder duplicat)."
    )

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")