const state = {
  docs: [], // { id, file, selected, status, result }
  format: "xlsx",
  isExtracting: false,
};

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const docsPanel = document.getElementById("docsPanel");
const docList = document.getElementById("docList");
const extractBtn = document.getElementById("extractBtn");
const exportBtn = document.getElementById("exportBtn");
const formatXlsxBtn = document.getElementById("formatXlsxBtn");
const formatCsvBtn = document.getElementById("formatCsvBtn");
const summaryPanel = document.getElementById("summaryPanel");
const summaryContent = document.getElementById("summaryContent");
const modalOverlay = document.getElementById("modalOverlay");
const modalName = document.getElementById("modalName");
const modalIframe = document.getElementById("modalIframe");
const modalClose = document.getElementById("modalClose");

let previewObjectUrl = null;

const STATUS_LABEL = {
  pending: "În așteptare",
  processing: "Se procesează…",
  success: "Extras",
  no_tables: "Fără tabele",
  error: "Eroare",
};

function genId(file) {
  return `${file.name}-${file.size}-${Math.random().toString(36).slice(2, 8)}`;
}



function addFiles(fileList) {
  const pdfFiles = Array.from(fileList).filter((f) => f.type === "application/pdf");
  const entries = pdfFiles.map((file) => ({
    file,
    id: genId(file),
    selected: true,
    status: "pending",
    result: null,
  }));
  state.docs.push(...entries);
  render();
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("active");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("active"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("active");
  addFiles(e.dataTransfer.files);
});
fileInput.addEventListener("change", (e) => addFiles(e.target.files));

formatXlsxBtn.addEventListener("click", () => {
  state.format = "xlsx";
  render();
});
formatCsvBtn.addEventListener("click", () => {
  state.format = "csv";
  render();
});



function openPreview(doc) {
  previewObjectUrl = URL.createObjectURL(doc.file);
  modalName.textContent = doc.file.name;
  modalIframe.src = previewObjectUrl;
  modalOverlay.style.display = "flex";
}

function closePreview() {
  if (previewObjectUrl) {
    URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = null;
  }
  modalIframe.src = "";
  modalOverlay.style.display = "none";
}

modalClose.addEventListener("click", closePreview);
modalOverlay.addEventListener("click", (e) => {
  if (e.target === modalOverlay) closePreview();
});
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && modalOverlay.style.display !== "none") closePreview();
});



extractBtn.addEventListener("click", async () => {
  if (state.isExtracting) return; // garda: ignora click-uri suplimentare cat timp extractia deja ruleaza
  state.isExtracting = true;
  render();

  const selected = state.docs.filter((d) => d.selected);

  for (const doc of selected) {
    doc.status = "processing";
    render();

    try {
      const formData = new FormData();
      formData.append("file", doc.file);
      const res = await fetch("/api/extract", { method: "POST", body: formData });
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      const result = await res.json();
      doc.status = result.status;
      doc.result = result;
    } catch (err) {
      doc.status = "error";
      doc.result = {
        filename: doc.file.name,
        status: "error",
        errorMessage: err && err.message ? err.message : String(err),
        tables: [],
      };
    }
    render();
  }

  state.isExtracting = false;
  render();
});



exportBtn.addEventListener("click", async () => {
  const results = state.docs
    .filter((d) => d.selected && d.result && d.result.status === "success")
    .map((d) => d.result);

  if (results.length === 0) return;

  const res = await fetch("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ results, format: state.format }),
  });

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = state.format === "csv" ? "technical_tables.csv" : "technical_tables.xlsx";
  a.click();
  URL.revokeObjectURL(url);
});


function render() {
  docsPanel.style.display = state.docs.length > 0 ? "block" : "none";
  docList.innerHTML = "";

  for (const doc of state.docs) {
    const li = document.createElement("li");
    li.className = "doc-row";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = doc.selected;
    checkbox.addEventListener("change", () => {
      doc.selected = !doc.selected;
      render();
    });

    const infoDiv = document.createElement("div");
    infoDiv.style.flex = "1";
    infoDiv.style.minWidth = "0";

    const nameDiv = document.createElement("div");
    nameDiv.className = "doc-name clickable";
    nameDiv.title = "Apasă pentru a vedea documentul original";
    nameDiv.textContent = doc.file.name;
    nameDiv.addEventListener("click", (e) => {
      e.stopPropagation();
      openPreview(doc);
    });
    infoDiv.appendChild(nameDiv);

    if (doc.result && doc.result.tables && doc.result.tables.length > 0) {
      const tablesDiv = document.createElement("div");
      tablesDiv.className = "doc-tables";
      tablesDiv.textContent = doc.result.tables
        .map((t) => t.title || "(tabel fără titlu)")
        .join(" · ");
      infoDiv.appendChild(tablesDiv);
    }

    if (doc.status === "error" && doc.result && doc.result.errorMessage) {
      const errDiv = document.createElement("div");
      errDiv.className = "doc-tables";
      errDiv.textContent = doc.result.errorMessage;
      infoDiv.appendChild(errDiv);
    }

    const statusSpan = document.createElement("span");
    statusSpan.className = `doc-status ${doc.status}`;
    statusSpan.textContent = STATUS_LABEL[doc.status];

    const removeBtn = document.createElement("button");
    removeBtn.className = "remove-btn";
    removeBtn.title = "Elimină";
    removeBtn.textContent = "✕";
    removeBtn.addEventListener("click", () => {
      state.docs = state.docs.filter((d) => d.id !== doc.id);
      render();
    });

    li.appendChild(checkbox);
    li.appendChild(infoDiv);
    li.appendChild(statusSpan);
    li.appendChild(removeBtn);
    docList.appendChild(li);
  }

  formatXlsxBtn.classList.toggle("selected", state.format === "xlsx");
  formatCsvBtn.classList.toggle("selected", state.format === "csv");

  const selectedCount = state.docs.filter((d) => d.selected).length;
  extractBtn.textContent = state.isExtracting ? "Se extrage…" : `Extrage (${selectedCount})`;
  extractBtn.disabled = state.isExtracting || selectedCount === 0;

  const hasExtractedResults = state.docs.some(
    (d) => d.selected && d.result && d.result.status === "success"
  );
  exportBtn.disabled = !hasExtractedResults;
  exportBtn.textContent = `Descarcă ${state.format === "csv" ? "CSV" : "Excel"}`;

  const docsWithTables = state.docs.filter(
    (d) => d.result && d.result.tables && d.result.tables.length > 0
  );
  summaryPanel.style.display = docsWithTables.length > 0 ? "block" : "none";
  summaryContent.innerHTML = "";
  for (const d of docsWithTables) {
    const docDiv = document.createElement("div");
    docDiv.className = "summary-doc";

    const nameDiv = document.createElement("div");
    nameDiv.className = "summary-doc-name";
    nameDiv.textContent = d.file.name;
    docDiv.appendChild(nameDiv);

    for (const t of d.result.tables) {
      const tDiv = document.createElement("div");
      tDiv.className = "summary-table-title";
      tDiv.textContent = `✔ ${t.title || "(tabel fără titlu)"} — ${t.rows.length} rânduri`;
      docDiv.appendChild(tDiv);
    }

    summaryContent.appendChild(docDiv);
  }
}

render();