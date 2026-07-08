import base64
import json
import re
import time
from typing import Any, Dict, List

import fitz #PyMuPDF
from groq import Groq

MIN_TABLE_COLUMNS = 2
MIN_DATA_ROWS = 1
TITLE_BLOCK_KEYWORDS = [
    "intocmit", "întocmit",
    "verificat",
    "sef proiect", "șef proiect", "sef de proiect",
    "proiectant",
    "beneficiar",
    "pagina", "pag.",
    "rev.", "revizie",
]

TABLES_DETECT_PROMPT = """\
This is a page from a technical document (could be in Romanian or English).

Find EVERY genuine piece of itemized TECHNICAL DATA on this page. This comes
in two shapes:

1. GRID tables - a header row of column names with short values organized
   in rows and columns below it. Typical examples: equipment lists, cable
   schedules, parts lists, technical characteristics tables
   ("Antemăsurătoare", "Lista de motoare", "Lista de aparataj", etc.).

2. ITEMIZED TECHNICAL LISTS - a numbered or lettered list where each item,
   even written as a full sentence, names a distinct technical deliverable
   tied to this specific project: an equipment/tablou/installation to
   supply or execute. The decisive test: does this item reference at least
   one concrete project-specific technical identifier or value - an
   equipment/tablou code (e.g. "TE402", "TGD"), an Annex/Anexa reference,
   a quantity, or a rating (kW, kVAR, m of cable, A, etc.)? If yes for
   (almost) every item, it's a genuine itemized list - extract it. If the
   items are instead generic descriptive statements, procedures, safety
   conditions, protecții descriptions, operating-mode explanations, or
   narrative about how a system functions - with no such per-item
   identifiers — it is NOT an itemized list; skip it, it's prose.

For each genuine table or list, return:
- "kind": "grid" or "list" (per the two shapes above).
- "title": the heading text printed directly above it, copied exactly
  (preserve diacritics/casing). Use "" if there is truly no heading above it.
- "headers": for "grid", the column header names left to right, copied
  exactly. For "list", use ["Nr.", "Descriere"] (or the actual numbering
  label used, e.g. "Poz.", if visibly different from "Nr.").
- "identifier_headers": the subset of "headers" (copied exactly, same
  spelling) whose cells hold labels/identifiers rather than measured
  values - e.g. a row number, code, name, or description column. Judge
  this per table, based on what that specific column actually contains
  in THIS document (a "Denumire"/"Descriere" column counts as identifier
  only if it never holds a numeric measurement). Every other header in
  "headers" is implicitly a data/measurement column. For "list" tables,
  this is just the numbering column (e.g. ["Nr."]).
- "estimated_data_rows": your best estimate of how many actual items/rows
  (excluding the header) appear on this page.

Do NOT include:
- Section headings, chapter titles, or a title followed by ordinary body text.
- Signature blocks, letterheads, stamps, or approval sections.
- Numbered/bulleted lists that fail the itemized-list test above (no
  per-item project-specific technical identifier).

A genuine grid needs at least {min_cols} columns AND at least
{min_rows} rows of short, structured data values below the header. A
genuine itemized list needs at least {min_rows} qualifying items.

Return ONLY valid JSON, no markdown fences, no commentary:
{{"tables": [{{"kind": "grid", "title": "EXACT HEADING", "headers": ["Col 1", "Col 2"], "identifier_headers": ["Col 1"], "estimated_data_rows": 5}}]}}

If this page contains no genuine table or itemized list, return: {{"tables": []}}
""".format(min_cols=MIN_TABLE_COLUMNS, min_rows=MIN_DATA_ROWS)


def list_rows_prompt_template(title: str) -> str:
    return f"""\
This page is from a technical document. Extract ALL items from the itemized
technical list titled "{title}".

Return each item as an object with keys "Nr." and "Descriere", plus one
OPTIONAL extra key "Observații".

Rules:
- "Nr.": the item's number or letter exactly as printed.
- "Descriere": the full text of the item, copied exactly (preserve
  diacritics/casing and every character), EXCEPT for any parenthetical or
  trailing reference to an Annex/Anexă (e.g. "(Anexa 3)", "conform ...
  Antemăsurătoare X, Anexa 5"). Strip that specific Annex/Anexă reference
  out of "Descriere" and put it, verbatim, in "Observații" instead.
- If the item mentions no Annex/Anexă reference, omit "Observații"
  entirely for that item - do not invent one.
- Do not skip any item, do not merge multiple numbered items into one,
  and do not paraphrase or shorten the text otherwise.

Return ONLY valid JSON, no markdown fences, no commentary, in exactly this
structure:
{{"rows": [{{"Nr.": "1", "Descriere": "...", "Observații": "Anexa 3"}}]}}
"""


def rows_prompt_template(title: str, headers: List[str]) -> str:
    headers_joined = ", ".join(headers)
    return f"""\
This page is from a technical document. Extract ALL data rows from the table
titled "{title}".

That table's column headers, in left-to-right order, are:
{headers_joined}

Each data row MUST be an object with EXACTLY those keys, plus one OPTIONAL
extra key described below.

IMPORTANT - section-label rows:
Some tables have rows that span the FULL WIDTH of the table and act as a
label for the group of data rows that follow (e.g. a location, a building
section, or a category such as "401 - Siloz grau"). These rows are NOT
genuine data rows — they typically contain a single piece of text and have
no separate values under the other column headers. Do NOT emit such a row
as its own entry in "rows". Instead:
  - Remember its text as the current section label.
  - Add an extra key "Secțiune" to every genuine data row that follows, set
    to that label, until a new section-label row appears (then switch to
    the new label).
  - If this table has no such section-label rows at all, simply omit the
    "Secțiune" key from every row - do not invent one.

Return ONLY valid JSON, no markdown fences, no commentary, in exactly this
structure:
{{"rows": [{{"<header 1>": "<cell value>", "<header 2>": "<cell value>", "Secțiune": "<optional>"}}]}}

Rules:
- Use the header names above as keys, exactly as written.
- Preserve every cell value exactly as written (including diacritics).
- In this kind of technical
  table, '+' is by far the more common symbol in these identifiers/ranges -
  '÷' is rare. So if the printed glyph is visually ambiguous or unclear
  between '+' and '÷', prefer reading it as '+'.
- If a cell says "Idem" (meaning "same as row above"), replace it with the
  full value copied from the most recent non-"Idem" cell in that column.
- Omit a key only if that specific cell is completely empty.
- Do not skip any genuine data row (section-label rows are handled per the
  rule above, not skipped silently - their text must be attached forward).
- If a column only contains row numbers (1, 2, 3...), name that header "Nr. Crt.".
"""


def extract_json(raw: str):
    clean = raw.strip()
    clean = re.sub(r"^```(?:json)?\s*\n?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\n?```\s*$", "", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(raw[start: end + 1])

    raise ValueError(f"Model did not return valid JSON: {raw[:300]}")


def buffer_to_data_url(buf: bytes, mime: str = "image/png"):
    b64 = base64.b64encode(buf).decode("ascii")
    return f"data:{mime};base64,{b64}"


def is_title_block(title: str, headers: List[str]):
    """Detecteaza deterministic un title-block/stampila, care structural poate arata ca un tabel
    mic dar nu contine date tehnice.
    """
    joined = (title + " " + " ".join(headers)).lower()
    hits = sum(1 for kw in TITLE_BLOCK_KEYWORDS if kw in joined)
    return hits >= 2


def extract_retry_delay_ms(error_message: str):
    ms_match = re.search(r"try again in ([\d.]+)ms", error_message)
    if ms_match:
        return int(float(ms_match.group(1))) + 1
    s_match = re.search(r"try again in ([\d.]+)s", error_message)
    if s_match:
        return int(float(s_match.group(1)) * 1000) + 1
    return 2000


def is_rate_limit_error(err: Exception):
    msg = str(err)
    return "429" in msg or "rate_limit_exceeded" in msg


def with_rate_limit_retry(fn, max_retries: int = 5):
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as err:
            last_err = err
            if not is_rate_limit_error(err) or attempt == max_retries:
                raise
            wait_ms = extract_retry_delay_ms(str(err)) + 250
            print(f"Rate limited — waiting {wait_ms}ms before retry {attempt + 1}/{max_retries}")
            time.sleep(wait_ms / 1000)
    raise last_err


def vision_completion(client: Groq, model: str, data_url: str, prompt: str, max_tokens: int):
    def call() -> str:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            temperature=0.1,
            max_tokens=max_tokens,
        )
        return (completion.choices[0].message.content or "").strip()

    return with_rate_limit_retry(call)


def render_pdf_pages(pdf_bytes: bytes, scale: float = 3.0):
    """
    Randeaza fiecare pagina a unui PDF (buffer) in cate un PNG.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    matrix = fitz.Matrix(scale, scale)
    images: List[bytes] = []
    try:
        for page in doc:
            pixmap = page.get_pixmap(matrix=matrix)
            images.append(pixmap.tobytes("png"))
    finally:
        doc.close()
    return images


def is_section_label_row(headers: List[str], row: Dict[str, str], identifier_headers: List[str] | None = None):

    if "Secțiune" in row and row.get("Secțiune"):
        return False

    identifier_headers = identifier_headers or []
    data_columns = [h for h in headers if h not in identifier_headers]

    if not data_columns:
        non_empty = [v for v in row.values() if v]
        return len(non_empty) <= 2

    has_data = any(row.get(h) for h in data_columns)
    return not has_data


def extract_tables_from_page(page_image: bytes, api_key: str, model: str = "meta-llama/llama-4-scout-17b-16e-instruct"):
    """
    Extrage fiecare tabel tehnic genuin de pe o singura pagina randata.
    """
    client = Groq(api_key=api_key)
    data_url = buffer_to_data_url(page_image)

    detect_raw = vision_completion(client, model, data_url, TABLES_DETECT_PROMPT, 1024)
    try:
        parsed = extract_json(detect_raw)
        detected = parsed.get("tables", []) if isinstance(parsed, dict) else []
    except Exception:
        return []

    candidates = [
        t
        for t in detected
        if isinstance(t.get("headers"), list)
           and len([h for h in t["headers"] if h and h.strip()]) >= MIN_TABLE_COLUMNS
           and (t.get("estimated_data_rows") or 0) >= MIN_DATA_ROWS
           and not is_title_block(
            t.get("title") or "", [h for h in t["headers"] if h and h.strip()]
        )
    ]

    results: List[Dict[str, Any]] = []

    for table in candidates:
        kind = table.get("kind") if table.get("kind") in ("grid", "list") else "grid"
        title = (table.get("title") or "").strip()
        headers = [h.strip() for h in table["headers"] if h and h.strip()]

        if kind == "list":
            rows_raw = vision_completion(client, model, data_url, list_rows_prompt_template(title), 8192)
            try:
                parsed = extract_json(rows_raw)
                rows = parsed.get("rows", []) if isinstance(parsed, dict) else []
            except Exception:
                continue

            OBS_KEY = "Observații"
            clean_rows = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                coerced = {
                    "Nr.": str(r.get("Nr.", r.get("Nr", ""))).strip(),
                    "Descriere": str(r.get("Descriere", "")).strip(),
                }
                obs = r.get(OBS_KEY) or r.get("Observatii")
                if obs and str(obs).strip():
                    coerced[OBS_KEY] = str(obs).strip()
                if not coerced["Descriere"]:
                    continue
                clean_rows.append(coerced)

            if len(clean_rows) >= MIN_DATA_ROWS and not is_title_block(title, ["Nr.", "Descriere"]):
                has_obs = any(OBS_KEY in r for r in clean_rows)
                final_headers = ["Nr.", "Descriere"] + ([OBS_KEY] if has_obs else [])
                results.append({"title": title, "headers": final_headers, "rows": clean_rows, "kind": kind})
            continue

        identifier_headers = [
            h.strip() for h in (table.get("identifier_headers") or [])
            if isinstance(h, str) and h.strip() in headers
        ]

        rows_raw = vision_completion(
            client, model, data_url, rows_prompt_template(title, headers), 8192
        )

        try:
            parsed = extract_json(rows_raw)
            rows = parsed.get("rows", []) if isinstance(parsed, dict) else []
        except Exception:
            continue

        SECTION_KEY = "Secțiune"
        clean_rows: List[Dict[str, str]] = []
        current_section: str | None = None

        for r in rows:
            if not isinstance(r, dict):
                continue

            coerced = {h: str(r.get(h, "")).strip() for h in headers}

            section_value = r.get(SECTION_KEY) or r.get("Sectiune")
            if section_value:
                section_str = str(section_value).strip()
                if section_str:
                    current_section = section_str


            if is_section_label_row(headers, coerced, identifier_headers):
                #construieste eticheta de sectiune din valorile existente
                non_empty = [v for v in coerced.values() if v]
                if non_empty:
                    if not current_section:
                        current_section = " - ".join(non_empty)
                    else:
                        pass
                continue

            if current_section and SECTION_KEY not in r:
                coerced[SECTION_KEY] = current_section

            if SECTION_KEY in r and r.get(SECTION_KEY):
                coerced[SECTION_KEY] = str(r.get(SECTION_KEY)).strip()


            if not any(v for v in coerced.values()):
                continue

            clean_rows.append(coerced)

        #only for grids
        all_values = [v for r in clean_rows for v in r.values() if v]
        long_value_count = sum(1 for v in all_values if len(v) > 80)
        looks_like_prose = len(all_values) > 0 and (long_value_count / len(all_values)) > 0.4

        if (len(clean_rows) >= MIN_DATA_ROWS and not looks_like_prose and not is_title_block(title, headers)):
            has_section = any(SECTION_KEY in r for r in clean_rows)
            final_headers = ([SECTION_KEY] if has_section else []) + headers
            results.append({"title": title, "headers": final_headers, "rows": clean_rows, "kind": kind})

    return results