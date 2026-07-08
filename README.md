# Extractor Tabele Tehnice din PDF (Python / FastAPI)

Aplicație web: încarci documente PDF (scanate sau digitale), extrage automat
tabelele tehnice (liste de motoare, aparataj, antemăsurători, cabluri etc.)
folosind un model de vision (Groq/Llama), bifezi ce vrei, exporți CSV sau
Excel într-un format **wide** (un rând extras = un rând în output; coloanele
sunt reuniunea tuturor parametrilor întâlniți, gol acolo unde lipsesc).

Poți deschide oricând documentul original apăsând pe numele lui — se
deschide direct din memoria browserului, fără storage extern.

## Cum funcționează

```
PDF (orice tip, scanat sau digital)
   │
   ▼
Fiecare pagină → imagine PNG (PyMuPDF)
   │
   ▼
Pas 1: Groq vision detectează tabelele genuine (titlu + coloane + nr. rânduri estimat)
       — filtrează liste numerotate / titluri de secțiune care nu sunt tabele reale
   │
   ▼
Pas 2: pentru fiecare tabel detectat, un apel dedicat extrage toate rândurile
   │
   ▼
Filtru final: respinge tabelele care ies totuși ca proză (propoziții lungi,
prea puține rânduri) — al doilea nivel de siguranță împotriva falselor pozitive
   │
   ▼
Rezultat wide, agregat pe toate documentele selectate
   │
   ▼
Export CSV / Excel
```

## Structura proiectului

```
main.py               — FastAPI: /api/extract, /api/export, servește frontend-ul static
extraction.py          — randare PDF -> PNG (PyMuPDF) + apeluri Groq vision (2 pași + filtre)
export_utils.py        — construiește formatul wide și generează CSV/XLSX
static/
  index.html           — UI-ul principal
  styles.css           — stiluri (identice cu varianta anterioară)
  app.js               — logica frontend (vanilla JS, fără framework)
requirements.txt
.env.example
```

## Setup local

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# editează .env și pune cheia ta Groq:
# GROQ_API_KEY=gsk_...

# incarca variabilele din .env (sau seteaza manual GROQ_API_KEY in shell)
export $(cat .env | xargs)      # Windows: seteaza manual variabila de mediu

uvicorn main:app --reload --port 3000
```

Deschide http://localhost:3000

## Deploy

Spre deosebire de varianta Next.js, aplicația asta e un server Python de
lungă durată (nu funcții serverless individuale), deci **Vercel nu e cea mai
naturală alegere** — suportul lui pentru Python e limitat la funcții
serverless separate, nu la o aplicație FastAPI completă cu fișiere statice.
Opțiuni mai simple pentru acest tip de aplicație:

- **Render** (render.com) — "Web Service", build command
  `pip install -r requirements.txt`, start command
  `uvicorn main:app --host 0.0.0.0 --port $PORT`. Plan gratuit disponibil.
- **Railway** (railway.app) — detectează automat `requirements.txt`,
  aceleași comenzi ca mai sus.
- **Fly.io** — necesită un `Dockerfile`, dar oferă control mai fin.

În toate cazurile, adaugă `GROQ_API_KEY` ca variabilă de mediu în
dashboard-ul platformei alese.

**Notă despre durată**: documentele cu multe pagini pot dura câteva minute
la extracție, pentru că fiecare pagină + fiecare tabel detectat înseamnă un
apel separat către Groq, procesate secvențial (nu în paralel, ca să evităm
rate-limit-ul de la Groq). Render/Railway nu au un timeout la fel de strict
ca funcțiile serverless Vercel (300s pe Hobby), dar verifică planul ales
dacă procesezi documente foarte mari.

## Reglaje pe care le poți ajusta

- **`extraction.py`**, parametrul `scale` din `render_pdf_pages` — dacă
  textul din tabele e mic/dens și modelul face greșeli de citire, crește la
  2.5 sau 3 (imagini mai mari = citire mai clară, dar și request-uri
  mai mari/mai lente).
- **`extraction.py`**, constanta `MIN_DATA_ROWS` — câte rânduri minime
  trebuie să aibă un tabel ca să fie acceptat (implicit 2). Crește dacă încă
  vezi liste scurte confundate cu tabele.
- **`extraction.py`**, modelul folosit (`meta-llama/llama-4-scout-17b-16e-instruct`) —
  Groq își schimbă des lista de modele disponibile și deprecia modele vechi
  relativ rapid. Dacă la un moment dat primești eroare `model_decommissioned`,
  verifică console.groq.com/docs/models pentru numele curent al unui model
  de vision.

## Caveat cunoscut

Dacă modelul scoate headere ușor diferite pentru același parametru logic
(`"Nr. Motor"` vs `"Nr Motor"`), ele devin coloane separate în export, nu se
unesc automat — nu există normalizare (trim/lowercase/fără diacritice) pe
numele coloanelor momentan. E o modificare mică de adăugat în
`export_utils.py` (`build_wide_rows`) dacă observi problema des în practică.
