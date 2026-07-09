import base64
import json
import re
import time
from typing import Any, Dict, List

import fitz  # PyMuPDF
from groq import Groq

MIN_TABLE_COLUMNS = 2
MIN_DATA_ROWS = 1

IDEM_PATTERN = re.compile(r"^\s*idem\s*$", re.IGNORECASE)


TOC_TITLE_PATTERNS = [
    r"\bcuprins\b",
    r"\btable of contents\b",
    r"\bcontents\b",
    r"\bsommaire\b",
    r"\bindice\b",
    r"\binhaltsverzeichnis\b",
]

TOC_HEADER_KEYWORDS = {"pagina", "pag.", "page", "pag"}

TECHNICAL_UNITS_PATTERN = re.compile(
    r'\b(?:kW|kVAR|kVA|kV|mA|A|V|Hz|mm2|mm|m\b|buc|kg|W|Ohm|÷|\+)\b',
    re.IGNORECASE
)


def is_toc(title: str, headers: List[str], rows: List[Dict[str, str]] | None = None) -> bool:
    title_lower = title.lower().strip()


    if any(re.search(pat, title_lower) for pat in TOC_TITLE_PATTERNS):
        return True

    headers_lower = [h.lower().strip() for h in headers]
    has_page_header = any(
        any(kw in h for kw in TOC_HEADER_KEYWORDS) for h in headers_lower
    )
    if has_page_header and len(headers) <= 3:
        if rows:
            page_col = None
            for h in headers:
                if any(kw in h.lower() for kw in TOC_HEADER_KEYWORDS):
                    page_col = h
                    break
            if page_col:
                numeric_count = sum(
                    1 for r in rows
                    if re.fullmatch(r"\s*\d+\s*", str(r.get(page_col, "")))
                )
                if len(rows) > 0 and numeric_count / len(rows) >= 0.8:
                    return True

    return False

def looks_like_toc_content(headers: List[str], rows: List[Dict[str, str]]) -> bool:
    """
    Analizeaza structura randurilor pentru a determina daca e un Cuprins
    Functioneaza in doua moduri:
    A) Tabel cu coloana de pagina explicita (Pagina / Page / Pag.)
    B) Lista simpla "Nr. + Descriere" fara coloana de pagina, unde
       descrierea e chiar un nume de capitol/anexa.
    """
    if not rows or not headers:
        return False

    all_text = ""
    for r in rows:
        for h in headers:
            all_text += str(r.get(h, "")) + " "

    #textul contine unitati de masura tehnice e tabel real
    if TECHNICAL_UNITS_PATTERN.search(all_text):
        return False

    #A. cautam o coloana de pagina numerica
    potential_page_col = None
    for h in headers:
        numeric_values = []
        for r in rows:
            val = str(r.get(h, "")).strip()
            if re.fullmatch(r"\d+(\.\d+)*", val):
                numeric_values.append(val)
        if len(numeric_values) / len(rows) >= 0.8:
            potential_page_col = h
            break

    other_cols = [h for h in headers if h != potential_page_col] if potential_page_col else headers

    #text = nume capitol
    CHAPTER_LABEL_KEYWORDS = re.compile(
        r"\b(anexa\d*|schema|capitol|cuprins|caiet de sarcini|memoriu|listă|lista)\b",
        re.IGNORECASE,
    )
    TRAILING_FILE_COUNT = re.compile(r"\(\s*\d+\s*fil[ae]\s*\)\s*$", re.IGNORECASE)

    chapter_pattern_count = 0
    keyword_hit_count = 0
    for r in rows:
        for h in other_cols:
            val = str(r.get(h, "")).strip()
            if not val:
                continue
            if re.match(r"^\s*(\d+(\.\d+)*\.?|[A-Z]\.)\s", val):
                chapter_pattern_count += 1
            if CHAPTER_LABEL_KEYWORDS.search(val) or TRAILING_FILE_COUNT.search(val):
                keyword_hit_count += 1

    if len(rows) > 0 and (chapter_pattern_count / len(rows) >= 0.5 or keyword_hit_count / len(rows) >= 0.5):
        return True

    if potential_page_col and not other_cols:
        return True

    if re.search(r'\.{3,}|\…{2,}', all_text):
        return True

    return False


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
Find every genuine piece of itemized TECHNICAL DATA. Two shapes:

1. GRID tables - header row of column names, then rows of short values.
2. ITEMIZED TECHNICAL LISTS - numbered/lettered list where each item names
   a project-specific technical deliverable (equipment, tablou, installation)
   and references a concrete identifier (equipment code, Annex reference,
   quantity, rating in kW/kVAR/m/A).

For each, return:
- "kind": "grid" or "list"
- "title": the nearest section heading above this table/list, copied exactly,
even if there is introductory prose text between the heading and the
first data row. Use "" only if there is truly no heading anywhere above.
- "headers": for "grid", column names left to right, copied exactly. For
  "list", use ["Nr.", "Descriere"] (or the actual numbering label, e.g.
  "Poz.", if visibly different).
- "identifier_headers": the subset of "headers" (copied exactly) whose cells
  hold labels/identifiers rather than measured values (row number, code,
  name, description). For "list" tables this is just ["Nr."].
- "category": classify the table/list itself before deciding anything else.
  Return "ADMINISTRATIV" if the table's content is about identifying the
  document, signatures/stamps, or data about the designers/beneficiary/
  verifiers (e.g. firm name, project name, document type, page number,
  "Intocmit"/"Verificat"/"Sef proiect"/"Proiectant"/"Beneficiar" rows,
  revision info). Return "TEHNIC" if it describes objects, equipment,
  installations, materials, or technical quantities. Only "TEHNIC" tables
  should ever be returned in the final "tables" array below - if you decide
  a table is "ADMINISTRATIV", leave it out of the array entirely rather
  than including it with that label.
- "estimated_data_rows": your best estimate of items/rows below the header.

Do NOT include: section headings, signature blocks/stamps, body text,
table of contents / Cuprins pages (entries that are document section names
paired with page numbers are structural navigation, not technical data).

Also do NOT include the recurring page LETTERHEAD/title-block that appears
at the top of nearly every page of this kind of document - a small table
naming the designer firm ("Proiectant: ..."), the beneficiary/project name,
the document type (e.g. "CAIET DE SARCINI"), and a page number ("Pag. X/Y"),
often followed by an "Obiectiv:" row describing the project in one long
sentence. This is administrative page framing, repeated identically (or
nearly so) across pages - never technical data, regardless of how
table-like its borders look. This is exactly the "ADMINISTRATIV" category
described above - never include it, no matter where on the page it appears.

Also do NOT include reference lists of LEGAL NORMS OR STANDARDS - e.g. a
checklist (✓ or bullet) citing standard/norm codes such as "I7 - 2002",
"SR EN 60439-1", "PE 116/94", "I20 - 2000", "STAS ...", "ISO ...", "IEC ..."
followed by the title of that norm. These codes look like identifiers but
are NOT project-specific technical deliverables - they're citations of
legislation/standards the project must comply with. Skip this kind of list
entirely, even if it's numbered or bulleted and even if the codes contain
digits, exactly like an equipment code would.

Also do NOT include prose SCOPE-OF-WORK bullet lists - e.g. a short intro
sentence like "Se va asigura proiectarea și execuția următoarelor
instalații:" followed by a few bullet/dash items that are themselves plain
descriptive sentences with NO per-item identifier, code, quantity, or
rating attached (e.g. "- Completarea și extinderea instalației de
împământare;", "- Instalație de paratrăsnet pentru protecția silozului de
grâu."). This is regular body text describing what will be done, not
itemized technical data - skip it even if bulleted/dashed and even if it
mentions installations by name, UNLESS each item also carries its own
concrete identifier or measured value (equipment code, kW/kVAR/m/A rating,
quantity) the way the TE402/TE403 example above does.

By contrast, DO include a bulleted/arrow list (e.g. using ">", "-", or no
marker at all) where each item names a project equipment/tablou by its own
code followed by an installed rating, such as:
  > TE402 - Siloz de grâu și precurățare .... 48 kW
  > TE403 și TFC modificat și completat ... 121 kW
This is a genuine itemized technical list (equipment code + kW rating per
item) even though it's short and has no visible numbering - extract it as
kind "list", category "TEHNIC".

A genuine grid needs at least {min_cols} columns and at least {min_rows}
rows of short, structured data values below the header. A genuine itemized
list needs at least {min_rows} qualifying items.

Return ONLY valid JSON, no markdown fences, no commentary:
{{"tables": [{{"kind": "grid", "title": "EXACT HEADING", "headers": ["Col 1", "Col 2"], "identifier_headers": ["Col 1"], "category": "TEHNIC", "estimated_data_rows": 5}}]}}

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
- This document uses '+' to separate values in a range or list (e.g. "10+16"
  meaning "10 to 16"). Always transcribe this separator as '+', even if the
  printed glyph looks like '÷'. Do not use '÷' anywhere in your output.
- "Idem" handling: a cell that says "Idem" means "same as the cell above
  in the same column". Replace it with the FULL value copied from the most
  recent non-"Idem" cell in that column. Do not leave "Idem" as-is.

  Example - what you see in the table:
    | 1 | Întrerupător automat tripolar, protecţie motorare | 0,68kW (1,3 - 2A) | Buc. | 2 |
    | 2 | Idem                                              | 1,5kW (2,5 - 4A)   | Buc. | 5 |
    | 3 | Idem                                              | 5,5kW (9 - 12A)    | Buc. | 2 |

  Example - what you must return (Idem replaced with the full value):
    {{"Nr. Crt.": "1", "Denumire": "Întrerupător automat tripolar, protecţie motorare", "Caracteristici": "0,68kW (1,3 - 2A)", "UM": "Buc.", "Cantitate": "2"}},
    {{"Nr. Crt.": "2", "Denumire": "Întrerupător automat tripolar, protecţie motorare", "Caracteristici": "1,5kW (2,5 - 4A)",   "UM": "Buc.", "Cantitate": "5"}},
    {{"Nr. Crt.": "3", "Denumire": "Întrerupător automat tripolar, protecţie motorare", "Caracteristici": "5,5kW (9 - 12A)",    "UM": "Buc.", "Cantitate": "2"}},

- VERTICALLY MERGED CELLS: a value is printed ONCE in a tall cell that
  visually spans multiple rows below it - no text repeated on those
  rows, no "Idem" written, the cell just has a tall border. TREAT IT AS
  IF the same value were printed on every row it spans. Fill in the
  same value for every covered row, not just the first.

  Example - what you see in the table:
    | 1 | 402TE → Motoare valturi | CYAbY-F 3x1,5 | m | 120 |
    | 2 |                         | CYAbY-F 4x1,5 | m | 320 |
    | 3 |                         | CYAbY-F 4x2,5 | m | 120 |

  Example - what you must return (first column value REPEATED on every row):
    {{"Nr. Crt.": "1", "Poziționare cablu": "402TE → Motoare valturi", "Tip / Secțiune": "CYAbY-F 3x1,5", "UM": "m", "Lungime estimativă": "120"}},
    {{"Nr. Crt.": "2", "Poziționare cablu": "402TE → Motoare valturi", "Tip / Secțiune": "CYAbY-F 4x1,5", "UM": "m", "Lungime estimativă": "320"}},
    {{"Nr. Crt.": "3", "Poziționare cablu": "402TE → Motoare valturi", "Tip / Secțiune": "CYAbY-F 4x2,5", "UM": "m", "Lungime estimativă": "120"}},

  Detect merges from the table's BORDER LAYOUT (tall cells with no inner
  horizontal lines), not from any text repetition.
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


def strip_diacritics(text: str) -> str:
    pairs = [
        ("ă", "a"), ("â", "a"), ("î", "i"),
        ("ș", "s"), ("ş", "s"),
        ("ț", "t"), ("ţ", "t"),
        ("Ă", "A"), ("Â", "A"), ("Î", "I"),
        ("Ș", "S"), ("Ş", "S"),
        ("Ț", "T"), ("Ţ", "T"),
    ]
    table = str.maketrans({src: dst for src, dst in pairs})
    return text.translate(table)


def is_title_block(title: str, headers: List[str]):
    joined = strip_diacritics((title + " " + " ".join(headers)).lower())
    hits = sum(1 for kw in TITLE_BLOCK_KEYWORDS if strip_diacritics(kw) in joined)
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

    detect_raw = vision_completion(client, model, data_url, TABLES_DETECT_PROMPT, 3072)
    try:
        parsed = extract_json(detect_raw)
        detected = parsed.get("tables", []) if isinstance(parsed, dict) else []
    except Exception:
        print(f"[detect_json_parse_failed] raw response was:\n{detect_raw[:2000]}")
        return []


    candidates = [
        t
        for t in detected
        if isinstance(t.get("headers"), list)
           and len([h for h in t["headers"] if h and h.strip()]) >= MIN_TABLE_COLUMNS
           and (t.get("estimated_data_rows") or 0) >= MIN_DATA_ROWS
           and str(t.get("category", "TEHNIC")).strip().upper() != "ADMINISTRATIV"
           and not is_toc(t.get("title") or "", [h for h in t["headers"] if h and h.strip()])
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
                # Imediat dupa ce primesti rows de la model, inainte de orice filtrare:
                print(f"[RAW rows for {title!r}] count={len(rows)}")
                for i, r in enumerate(rows):
                    print(f"  raw[{i}]: {r}")

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



            if len(clean_rows) >= MIN_DATA_ROWS and not is_toc(title, ["Nr.", "Descriere"]):
                if looks_like_toc_content(["Nr.", "Descriere"], clean_rows):
                    print(f"Skipping TOC-like list: {title}")
                    continue
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
            # Imediat dupa ce primesti rows de la model, inainte de orice filtrare:
            print(f"[RAW rows for {title!r}] count={len(rows)}")
            for i, r in enumerate(rows):
                print(f"  raw[{i}]: {r}")
        except Exception:
            continue

        SECTION_KEY = "Secțiune"
        clean_rows: List[Dict[str, str]] = []
        current_section: str | None = None

        last_seen = {}
        for r in rows:

            if not isinstance(r, dict):
                continue

            coerced = {h: str(r.get(h, "")).strip() for h in headers}

            # Rezolva "Idem" determinist - nu te baza doar pe model
            for h in headers:
                val = coerced.get(h, "")
                if IDEM_PATTERN.match(val):
                    coerced[h] = last_seen.get(h, val)
                elif val:
                    last_seen[h] = val

            section_value = r.get(SECTION_KEY) or r.get("Sectiune")
            if section_value:
                section_str = str(section_value).strip()
                if section_str:
                    current_section = section_str

            if is_section_label_row(headers, coerced, identifier_headers):
                non_empty = [v for v in coerced.values() if v]
                if non_empty:
                    current_section = " - ".join(non_empty)
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


        if looks_like_toc_content(headers, clean_rows):
            print(f"Skipping TOC-like table: {title}")
            continue


        if (len(clean_rows) >= MIN_DATA_ROWS and not looks_like_prose):
            has_section = any(SECTION_KEY in r for r in clean_rows)
            final_headers = ([SECTION_KEY] if has_section else []) + headers
            results.append({"title": title, "headers": final_headers, "rows": clean_rows, "kind": kind})

    return results