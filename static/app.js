const $ = (s, el = document) => el.querySelector(s);
const h = (tag, props = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
};
const show = id => document.querySelectorAll(".view").forEach(v => (v.hidden = v.id !== id));
const STAGES = [["read", "Reading"], ["cites", "Citations"], ["digest", "Digesting"], ["summary", "Summary"], ["review", "Reviewing"], ["global", "Cross-check"], ["verdict", "Verdict"]];
const SEV = ["critical", "major", "minor"];
const RECS = { "accept": "accept", "minor revision": "minor", "major revision": "major", "reject": "reject" };

let files = [], R = null, activeJobId = null, activeBatchId = null, batchItems = [], qaHistory = [], activeCitation = null, sevFilter = "all", catFilter = "all", showAll = false, uploadWarning = "";
let uploadLimits = null;
const ACTIVE_KEY = "reacher.active";
const UPLOAD_REQUEST_KEY = "reacher.upload-request";

function uploadIdempotencyKey() {
  const fingerprint = JSON.stringify({
    files: files.map(file => ({ filename: file.name, size: file.size })),
    verify_online: $("#online").checked
  });
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(UPLOAD_REQUEST_KEY) || "null"); }
  catch { saved = null; }
  if (saved?.fingerprint === fingerprint && /^[a-f0-9]{32}$/.test(saved.key)) return saved.key;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const key = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  sessionStorage.setItem(UPLOAD_REQUEST_KEY, JSON.stringify({ fingerprint, key }));
  return key;
}

function jobHeaders(id) {
  const token = sessionStorage.getItem(`reacher.job.${id}`);
  return token ? { "X-Job-Token": token } : {};
}

function persistActive() {
  sessionStorage.setItem(ACTIVE_KEY, JSON.stringify({
    jobId: activeJobId,
    batchId: activeBatchId,
    batchToken: activeBatchId ? sessionStorage.getItem(`reacher.batch.${activeBatchId}`) : null
  }));
}

async function pollHealth() {
  try {
    const s = await (await fetch("/api/health")).json();
    const m = $("#model");
    m.className = "model " + s.state;
    $("span", m).textContent = s.state === "ready" ? `${s.model} · ${s.device}` : s.state === "error" ? "API key / model problem · hover" : `loading ${s.model}…`;
    if (s.limits) {
      uploadLimits = { ...s.limits, cloud_backend: s.cloud_backend };
      if (!files.length) $("#drop-sub").textContent = `${s.limits.max_file_mb} MB per paper · up to ${s.limits.max_batch_files} papers / ${s.limits.max_batch_mb} MB per bundle`;
    }
    if (s.state === "error") m.title = s.error;
    if (s.state === "loading" || s.state === "idle") setTimeout(pollHealth, 2000);
  } catch { setTimeout(pollHealth, 3000); }
}
pollHealth();

const drop = $("#drop"), input = $("#file");
function pick(selection) {
  files = Array.from(selection || []);
  if (!files.length) return;
  drop.classList.add("has");
  const totalMb = files.reduce((total, f) => total + f.size, 0) / 1048576;
  $("#drop-title").textContent = files.length === 1 ? files[0].name : `${files.length} papers selected`;
  $("#drop-sub").textContent = `${totalMb.toFixed(1)} MB total · ready`;
  const tooMany = uploadLimits && files.length > uploadLimits.max_batch_files;
  const fileTooLarge = uploadLimits && files.find(f => f.size > uploadLimits.max_file_mb * 1048576);
  const bundleTooLarge = uploadLimits && files.length > 1 && totalMb > uploadLimits.max_batch_mb;
  const error = tooMany
    ? `Select no more than ${uploadLimits.max_batch_files} papers.`
    : fileTooLarge
      ? `${fileTooLarge.name} is larger than ${uploadLimits.max_file_mb} MB.`
      : bundleTooLarge
        ? `The selected papers exceed the ${uploadLimits.max_batch_mb} MB bundle limit.`
        : "";
  $("#err").textContent = error;
  $("#go").disabled = Boolean(error);
}
input.addEventListener("change", () => pick(input.files));
["dragenter", "dragover"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", ev => pick(ev.dataTransfer.files));
$("#go").addEventListener("click", start);
function resetUpload() {
  files = []; R = null; activeJobId = null; activeBatchId = null; batchItems = []; qaHistory = []; activeCitation = null;
  sessionStorage.removeItem(ACTIVE_KEY);
  sessionStorage.removeItem(UPLOAD_REQUEST_KEY);
  drop.classList.remove("has"); $("#drop-title").textContent = "Drop papers here";
  $("#drop-sub").textContent = "PDF, DOCX, TXT or MD · select one paper or a bundle";
  $("#go").disabled = true; input.value = ""; $("#err").textContent = "";
  $("#batch-overview").replaceChildren(); $("#batch-overview").hidden = true; $("#review-content").hidden = false;
  show("v-upload");
}
$("#x-new").addEventListener("click", resetUpload);
$("#tabs").addEventListener("click", e => { const t = e.target.dataset.t; if (t) tab(t); });
$("#ask-form").addEventListener("submit", askPaper);
$("#ask-messages").addEventListener("click", e => {
  const button = e.target.closest("[data-passage]");
  if (button) focusCitation(+button.dataset.start, +button.dataset.end);
});

function tab(t) {
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.t === t));
  ["review", "summary", "cites", "ask"].forEach(n => ($("#t-" + n).hidden = n !== t));
}

async function start() {
  if (!files.length) return;
  if (uploadLimits?.cloud_backend === "google") return startCloudUpload();
  const fd = new FormData();
  fd.append("verify_online", $("#online").checked);
  const isBatch = files.length > 1;
  const stages = isBatch ? [...STAGES, ["papers", "Papers"]] : STAGES;
  for (const f of files) fd.append(isBatch ? "files" : "file", f);
  $("#rail").replaceChildren(...stages.map(([k, l]) => h("li", { dataset: { k } }, l)));
  $("#log").textContent = "";
  $("#log").textContent = isBatch ? `Uploading ${files.length} papers…` : `Uploading ${files[0].name}…`;
  setProgress(0, "read");
  $("#go").disabled = true;
  show("v-run");
  try {
    const r = await fetch(isBatch ? "/api/analyze-batch" : "/api/analyze", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Upload failed.");
    if (isBatch) {
      activeBatchId = data.batch_id;
      pollBatch(activeBatchId);
    } else {
      activeJobId = data.job_id;
      poll(activeJobId);
    }
  } catch (error) {
    show("v-upload"); $("#err").textContent = error.message || "Upload failed."; $("#go").disabled = false;
  }
}

function putUpload(url, file, contentType, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", url);
    request.setRequestHeader("Content-Type", contentType);
    request.upload.onprogress = event => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) resolve();
      else reject(new Error("Cloud Storage rejected the upload. Request a new upload link and try again."));
    };
    request.onerror = () => reject(new Error("Network interruption during upload. You can retry the upload."));
    request.onabort = () => reject(new Error("Upload was interrupted."));
    request.send(file);
  });
}

function uploadRequest(url, headers, body, progressStart, total, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", url);
    Object.entries(headers).forEach(([name, value]) => request.setRequestHeader(name, value));
    request.upload.onprogress = event => {
      if (event.lengthComputable) onProgress(Math.min(total, progressStart + event.loaded) / total);
    };
    request.onload = () => resolve({
      status: request.status,
      range: request.getResponseHeader("Range")
    });
    request.onerror = () => reject(new Error("Network interruption during upload."));
    request.send(body);
  });
}

function nextUploadOffset(range) {
  const match = /^bytes=0-(\d+)$/.exec(range || "");
  return match ? Number(match[1]) + 1 : 0;
}

async function resumableUpload(url, file, contentType, onProgress) {
  const chunkSize = 8 * 1024 * 1024;
  let offset = 0, retries = 0;
  while (offset < file.size) {
    const end = Math.min(offset + chunkSize, file.size);
    try {
      const response = await uploadRequest(url, {
        "Content-Type": contentType,
        "Content-Range": `bytes ${offset}-${end - 1}/${file.size}`
      }, file.slice(offset, end), offset, file.size, onProgress);
      if (response.status === 200 || response.status === 201) return;
      if (response.status !== 308) throw new Error("Cloud Storage rejected the upload.");
      const next = nextUploadOffset(response.range);
      if (next <= offset) throw new Error("Upload progress could not be confirmed.");
      offset = next;
      retries = 0;
    } catch (error) {
      if (++retries > 5) throw new Error("Upload could not resume after repeated network failures.");
      await new Promise(resolve => setTimeout(resolve, Math.min(1000 * (2 ** (retries - 1)), 8000)));
      const status = await uploadRequest(url, {
        "Content-Range": `bytes */${file.size}`
      }, null, offset, file.size, onProgress);
      if (status.status === 200 || status.status === 201) return;
      if (status.status !== 308) throw error;
      const next = nextUploadOffset(status.range);
      if (next < offset) throw new Error("Cloud Storage reported an invalid upload offset.");
      offset = next;
    }
  }
}

async function startCloudUpload() {
  const isBatch = files.length > 1;
  const stages = isBatch ? [...STAGES, ["papers", "Papers"]] : STAGES;
  let authorization = null;
  uploadWarning = "";
  const completedUploads = new Set();
  $("#rail").replaceChildren(...stages.map(([key, label]) => h("li", { dataset: { k: key } }, label)));
  $("#log").textContent = "Preparing secure direct upload…";
  setProgress(0, "read");
  $("#go").disabled = true;
  show("v-run");
  try {
    const idempotencyKey = uploadIdempotencyKey();
    const response = await fetch("/api/uploads/authorize", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        verify_online: $("#online").checked,
        files: files.map(file => ({ filename: file.name, size: file.size }))
      })
    });
    authorization = await response.json();
    if (!response.ok) throw new Error(authorization.detail || "Could not authorize the uploads.");
    activeBatchId = authorization.batch_id;
    if (activeBatchId) sessionStorage.setItem(`reacher.batch.${activeBatchId}`, authorization.batch_token);
    authorization.uploads.forEach(upload => sessionStorage.setItem(`reacher.job.${upload.job_id}`, upload.token));

    for (let index = 0; index < authorization.uploads.length; index++) {
      const upload = authorization.uploads[index], file = files[index];
      if (upload.status !== "uploading") {
        completedUploads.add(upload.job_id);
        continue;
      }
      activeJobId = upload.job_id;
      persistActive();
      $("#log").textContent = `Uploading ${upload.filename}… 0%`;
      const progress = fraction => {
        $("#log").textContent = `Uploading ${upload.filename}… ${Math.round(fraction * 100)}%`;
      };
      if (upload.upload_mode === "resumable") {
        await resumableUpload(upload.upload_url, file, upload.content_type, progress);
      } else {
        await putUpload(upload.upload_url, file, upload.content_type, progress);
      }
      const complete = await fetch(`/api/jobs/${upload.job_id}/complete`, {
        method: "POST",
        headers: jobHeaders(upload.job_id)
      });
      const completion = await complete.json();
      if (!complete.ok) throw new Error(completion.detail || `Could not queue ${upload.filename}.`);
      completedUploads.add(upload.job_id);
    }

    if (activeBatchId) {
      activeJobId = null;
      persistActive();
      pollBatch(activeBatchId);
    } else {
      activeJobId = authorization.uploads[0].job_id;
      persistActive();
      poll(activeJobId);
    }
  } catch (error) {
    if (authorization?.uploads) {
      const cleanupErrors = (await Promise.all(authorization.uploads
        .filter(upload => !completedUploads.has(upload.job_id))
        .map(async upload => {
          try {
            const response = await fetch(`/api/jobs/${upload.job_id}/fail-upload`, {
              method: "POST",
              headers: jobHeaders(upload.job_id)
            });
            return response.ok ? null : upload.filename;
          } catch {
            return upload.filename;
          }
        }))).filter(Boolean);
      if (cleanupErrors.length) {
        uploadWarning = ` Could not update upload status for: ${cleanupErrors.join(", ")}.`;
      }
      if (activeBatchId) {
        activeJobId = null;
        persistActive();
        $("#log").textContent = `An upload failed: ${error.message || "network error"}.${uploadWarning} Showing available paper results…`;
        show("v-run");
        pollBatch(activeBatchId);
        return;
      }
    }
    show("v-upload");
    $("#err").textContent = (error.message || "Upload failed.") + uploadWarning;
    $("#go").disabled = false;
  }
}

function setProgress(p, stage) {
  $("#pct").textContent = Math.round(p * 100);
  $("#bar").style.width = p * 100 + "%";
  const steps = [...document.querySelectorAll("#rail li")];
  let idx = stage === "done" ? steps.length : steps.findIndex(li => li.dataset.k === stage);
  if (idx < 0) idx = 0;
  steps.forEach((li, i) => {
    li.className = i < idx ? "done" : i === idx ? "now" : "";
  });
}

async function poll(id) {
  try {
    const response = await fetch(`/api/jobs/${id}`, { headers: jobHeaders(id) });
    const s = await response.json();
    if (!response.ok) throw new Error(s.detail || "Could not check the review status.");
    setProgress(s.progress, s.stage);
    $("#log").textContent = s.log.map(l => "› " + l).join("\n");
    if (["uploading", "queued", "processing", "running"].includes(s.status)) return setTimeout(() => poll(id), 2000);
    if (["failed", "error"].includes(s.status)) throw new Error(s.error || "The review failed.");
    if (!["completed", "done"].includes(s.status)) throw new Error("The review returned an unexpected status.");
    const result = await fetch(`/api/jobs/${id}/result`, { headers: jobHeaders(id) });
    const data = await result.json();
    if (!result.ok) throw new Error(data.detail || "Could not load the review.");
    activeJobId = id;
    R = data;
    persistActive();
    render();
  } catch (error) {
    if (error instanceof TypeError) {
      $("#log").textContent = "Connection interrupted. Reconnecting…";
      return setTimeout(() => poll(id), 4000);
    }
    show("v-upload");
    $("#err").textContent = error.message || "Could not check the review status.";
    $("#go").disabled = false;
  }
}

async function pollBatch(id) {
  try {
    const response = await fetch(`/api/batches/${id}`, {
      headers: { "X-Job-Token": sessionStorage.getItem(`reacher.batch.${id}`) || "" }
    });
    const status = await response.json();
    if (!response.ok) throw new Error(status.detail || "Could not check the bundle status.");
    setProgress(status.progress, status.status === "completed" ? "done" : status.stage);
    $("#log").textContent = [
      `Completed ${status.completed} of ${status.total} papers.`,
      uploadWarning,
      ...status.log.map(line => "› " + line)
    ].join("\n");
    if (["uploading", "queued", "processing", "running"].includes(status.status)) return setTimeout(() => pollBatch(id), 2000);
    batchItems = status.items;
    renderBatchOverview();
    const firstSuccess = batchItems.find(item => ["completed", "done"].includes(item.status));
    if (firstSuccess) await openBatchPaper(firstSuccess.job_id);
    else { $("#review-content").hidden = true; show("v-result"); }
  } catch (error) {
    if (error instanceof TypeError) {
      $("#log").textContent = "Connection interrupted. Reconnecting…";
      return setTimeout(() => pollBatch(id), 4000);
    }
    show("v-upload"); $("#err").textContent = error.message || "Could not check the bundle."; $("#go").disabled = false;
  }
}

function renderBatchOverview() {
  const overview = $("#batch-overview"), successful = batchItems.filter(item => ["completed", "done"].includes(item.status)).length;
  const failed = batchItems.length - successful;
  overview.hidden = false;
  overview.replaceChildren(
    h("div", { class: "batch-head" }, h("h2", {}, "Paper bundle"),
      h("button", { class: "btn ghost", type: "button", onclick: resetUpload }, "Choose new papers")),
    h("p", {}, `${successful} of ${batchItems.length} papers reviewed${failed ? ` · ${failed} failed` : ""}. Reviews are processed one paper at a time.`),
    h("p", { class: "err", id: "batch-error", role: "alert" }),
    h("div", { class: "batch-list" }, batchItems.map(item =>
      h("article", { class: "batch-item" },
        h("div", {}, h("b", {}, item.name), h("small", {}, ["completed", "done"].includes(item.status) ? "Review complete" : ["failed", "error"].includes(item.status) ? `Could not review: ${item.error}` : "Waiting for review")),
        ["completed", "done"].includes(item.status) && h("button", {
          class: "btn ghost",
          type: "button",
          onclick: () => openBatchPaper(item.job_id)
        }, item.job_id === activeJobId ? "Viewing" : "Open review")
      )
    ))
  );
  $("#batch-error").textContent = uploadWarning;
}

async function openBatchPaper(id) {
  const item = batchItems.find(paper => paper.job_id === id);
  if (!item || !["completed", "done"].includes(item.status)) return;
  try {
    const response = await fetch(`/api/jobs/${id}/result`, { headers: jobHeaders(id) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not load this review.");
    activeJobId = id;
    R = result;
    persistActive();
    renderBatchOverview();
    render();
  } catch (error) {
    $("#batch-error").textContent = error.message || "Could not load this review.";
  }
}

function render() {
  $("#review-content").hidden = false;
  $("#err").hidden = true;
  if (!activeBatchId) $("#batch-overview").hidden = true;
  const F = R.review.findings, ok = F.filter(f => f.grounded);
  sevFilter = "all"; catFilter = "all"; showAll = false; qaHistory = []; activeCitation = null;
  $("#r-file").textContent = `${R.meta.filename} · ${R.meta.words.toLocaleString()} words · ${R.meta.model}`;
  $("#r-title").textContent = R.summary.title;
  $("#r-counts").replaceChildren(...SEV.map(s => h("div", { class: "pill" }, h("i", { style: `background:var(--${s === "critical" ? "crit" : s})` }), h("b", {}, ok.filter(f => f.severity === s).length), s)));
  const rec = R.review.recommendation;
  const st = $("#r-stamp"); st.textContent = rec; st.className = "stamp " + RECS[rec];
  renderVerdict(); renderFilters(); renderCards(); renderPaper(); renderSummary(); renderCites(); renderQA();
  tab("review");
  show("v-result");
}

function renderVerdict() {
  const v = R.review, el = $("#verdict");
  el.replaceChildren(
    h("h3", {}, "Overall assessment"),
    h("p", {}, v.overall || "No overall assessment was produced."),
    v.strengths.length && h("h3", {}, "Strengths"), v.strengths.length && h("ul", {}, v.strengths.map(s => h("li", {}, s))),
    v.priorities.length && h("h3", {}, "Fix these first"), v.priorities.length && h("ol", {}, v.priorities.map(s => h("li", {}, s)))
  );
}

function renderFilters() {
  const cats = [...new Set(R.review.findings.map(f => f.category))];
  const unv = R.review.findings.filter(f => !f.grounded).length;
  $("#filters").replaceChildren(
    ...["all", ...SEV].map(s => h("button", { class: "chip" + (sevFilter === s ? " on" : ""), onclick: () => { sevFilter = s; renderFilters(); renderCards(); } }, s)),
    h("select", { onchange: e => { catFilter = e.target.value; renderCards(); } }, ["all", ...cats].map(c => h("option", { value: c, selected: c === catFilter }, c))),
    unv > 0 && h("button", { class: "chip" + (showAll ? " on" : ""), title: "Findings whose quoted text could not be found in the paper", onclick: () => { showAll = !showAll; renderFilters(); renderCards(); } }, `unverified (${unv})`)
  );
}

function renderCards() {
  const list = R.review.findings.filter(f => (showAll || f.grounded) && (sevFilter === "all" || f.severity === sevFilter) && (catFilter === "all" || f.category === catFilter));
  $("#cards").replaceChildren(...(list.length ? list.map(card) : [h("div", { class: "empty" }, "No findings match. The reviewer found nothing it could anchor to the text.")]));
}

function card(f) {
  return h("article", { class: "card" + (f.grounded ? "" : " dim"), dataset: { id: f.id, sev: f.severity }, onclick: () => focusFinding(f.id, true) },
    h("header", {}, h("span", { class: "num" }, f.id), h("div", {}, h("h4", {}, f.title),
      h("div", { class: "meta" }, h("span", {}, f.severity), h("span", {}, f.category), f.section && h("span", {}, f.section), !f.grounded && h("span", { class: "warn" }, "quote not located – verify manually")))),
    h("div", { class: "body" }, f.quote && h("blockquote", {}, f.quote), h("p", {}, f.explanation), f.fix && h("div", { class: "fix" }, h("b", {}, "Suggested fix"), f.fix)));
}

function focusFinding(id, fromCard) {
  document.querySelectorAll(".card").forEach(c => c.classList.toggle("open", +c.dataset.id === id && (fromCard ? !c.classList.contains("open") : true)));
  const mk = $(`mark[data-id="${id}"]`);
  const paper = $("#paper"), panel = $(".panel");
  if (mk) { paper.scrollTo({ top: mk.offsetTop - paper.clientHeight / 2, behavior: "smooth" }); mk.classList.remove("pulse"); void mk.offsetWidth; mk.classList.add("pulse"); }
  const cd = $(`.card[data-id="${id}"]`);
  if (!fromCard && cd) panel.scrollTo({ top: cd.offsetTop - 12, behavior: "smooth" });
}

function renderPaper() {
  const T = R.text, rank = { critical: 0, major: 1, minor: 2 };
  const marks = [];
  for (const f of [...R.review.findings].filter(f => f.grounded && f.span).sort((a, b) => rank[a.severity] - rank[b.severity]))
    if (!marks.some(m => f.span[0] < m.e && f.span[1] > m.s)) marks.push({ s: f.span[0], e: f.span[1], f });
  const heads = R.meta.heads.filter(([s, e]) => !marks.some(m => s < m.e && e > m.s)).map(([s, e]) => ({ s, e }));
  if (activeCitation) {
    const overlaps = marks.some(m => activeCitation.start < m.e && activeCitation.end > m.s);
    if (!overlaps) marks.push({ s: activeCitation.start, e: activeCitation.end, citation: true });
  }
  const items = [...marks, ...heads].sort((a, b) => a.s - b.s);
  const out = []; let pos = 0;
  for (const it of items) {
    if (it.s < pos) continue;
    out.push(T.slice(pos, it.s));
    const txt = T.slice(it.s, it.e);
    out.push(it.f
      ? h("mark", { class: "hl", dataset: { id: it.f.id, sev: it.f.severity }, onclick: () => focusFinding(it.f.id, false) }, txt, h("sup", { class: "pin" }, it.f.id))
      : it.citation
        ? h("mark", { class: "qa-highlight" }, txt)
      : h("span", { class: "head" }, txt));
    pos = it.e;
  }
  out.push(T.slice(pos));
  $("#paper").replaceChildren(...out);
}

function renderQA() {
  const messages = $("#ask-messages");
  if (!qaHistory.length) {
    messages.replaceChildren(h("p", { class: "ask-empty" }, "Ask about the research question, methods, results, limitations, or any detail in the paper."));
    return;
  }
  messages.replaceChildren(...qaHistory.map(item => {
    const content = [h("p", {}, item.content)];
    if (item.role === "assistant" && item.evidence?.length) {
      content.push(h("div", { class: "ask-evidence" }, ...item.evidence.map(citation =>
        h("button", {
          class: "evidence-link",
          type: "button",
          dataset: { passage: citation.passage_id, start: citation.start, end: citation.end }
        },
        h("b", {}, "Paper passage"),
        h("q", {}, citation.text),
        citation.reason && h("small", {}, citation.reason),
        h("span", {}, "Open in paper →"))
      )));
    }
    return h("article", { class: "ask-message " + item.role }, h("b", {}, item.role === "user" ? "You" : "Reacher"), ...content);
  }));
  messages.scrollTop = messages.scrollHeight;
}

async function askPaper(event) {
  event.preventDefault();
  const input = $("#ask-input"), question = input.value.trim(), submit = $("#ask-submit");
  if (!question || !activeJobId || submit.disabled) return;
  const prior = qaHistory.map(({ role, content }) => ({ role, content }));
  qaHistory.push({ role: "user", content: question });
  renderQA();
  $("#ask-error").textContent = "";
  submit.disabled = true;
  submit.textContent = "Thinking…";
  try {
    const response = await fetch(`/api/jobs/${activeJobId}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...jobHeaders(activeJobId) },
      body: JSON.stringify({ question, history: prior.slice(-8) })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not answer that question.");
    qaHistory.push({ role: "assistant", content: result.answer, evidence: result.evidence });
    input.value = "";
  } catch (error) {
    $("#ask-error").textContent = error.message || "Could not answer that question.";
  } finally {
    submit.disabled = false;
    submit.textContent = "Ask Reacher";
    renderQA();
  }
}

function focusCitation(start, end) {
  tab("review");
  activeCitation = { start, end };
  renderPaper();
  const mark = $(".qa-highlight", $("#paper"));
  if (mark) {
    $("#paper").scrollTo({ top: mark.offsetTop - $("#paper").clientHeight / 2, behavior: "smooth" });
    return;
  }
  const finding = R.review.findings.find(f => f.span && start < f.span[1] && end > f.span[0]);
  if (finding) focusFinding(finding.id, false);
}

function renderSummary() {
  const s = R.summary, list = a => h("ul", {}, a.length ? a.map(x => h("li", {}, x)) : h("li", {}, "Not stated"));
  $("#t-summary").replaceChildren(
    h("p", { class: "tldr" }, s.tldr || "No summary was produced."),
    h("div", { class: "grid" },
      h("div", { class: "sec" }, h("h3", {}, "Research question"), h("p", {}, s.research_question || "Not stated")),
      h("div", { class: "sec" }, h("h3", {}, "Approach"), h("p", {}, s.approach || "Not stated")),
      h("div", { class: "sec" }, h("h3", {}, "Key findings"), list(s.key_findings)),
      h("div", { class: "sec" }, h("h3", {}, "Contributions"), list(s.contributions)),
      h("div", { class: "sec" }, h("h3", {}, "Limitations the authors state"), list(s.stated_limitations)),
      h("div", { class: "sec" }, h("h3", {}, "Keywords"), h("div", { class: "kw" }, s.keywords.map(k => h("span", {}, k))))
    ));
}

function renderCites() {
  const c = R.citations, st = c.stats, el = $("#t-cites");
  if (!c.found) { el.replaceChildren(h("div", { class: "empty" }, c.issues[0]?.text || "No references found.")); return; }
  const unconfirmed = (st.unverified || 0) + (st.skipped || 0);
  const tile = (n, l, cls) => h("div", { class: "tile " + (cls || "") }, h("b", {}, n ?? "–"), h("span", {}, l));
  const hist = c.histogram, max = Math.max(1, ...hist.map(x => x[1]));
  const y0 = hist[0]?.[0], y1 = hist.at(-1)?.[0];
  const bars = [];
  if (hist.length) for (let y = y0; y <= y1; y++) { const n = (hist.find(x => x[0] === y) || [0, 0])[1]; bars.push(h("div", { style: `height:${Math.max(n ? 8 : 1, n / max * 100)}%`, dataset: { t: `${y}: ${n}` } })); }
  el.replaceChildren(
    h("div", { class: "tiles" },
      tile(st.references, "references"), tile(st.in_text, "in-text citations"),
      tile(st.dangling, "citations with no entry", st.dangling ? "bad" : "good"),
      tile(st.uncited, "never cited", st.uncited ? "bad" : "good"),
      st.online && tile(st.verified, "verified online", "good"),
      st.online && tile(st.mismatch, "mismatched", st.mismatch ? "bad" : ""),
      st.online && tile(unconfirmed, "not confirmed online", unconfirmed ? "bad" : ""),
      st.recent_share != null && tile(Math.round(st.recent_share * 100) + "%", "from the last 5 years")),
    hist.length > 1 && h("div", { class: "sec" }, h("h3", {}, `Publication years · ${c.style} style`), h("div", { class: "hist" }, bars), h("div", { class: "axis" }, h("span", {}, y0), h("span", {}, "median " + st.median_year), h("span", {}, y1))),
    c.issues.length > 0 && h("div", { class: "sec", style: "margin-top:14px" }, h("h3", {}, "Issues"), c.issues.slice(0, 40).map(i => h("div", { class: "issue" }, h("code", {}, i.type), h("span", {}, i.text)))),
    h("p", { class: "note" }, st.online ? "Verified requires matching title, first author, and year (and a resolving DOI when supplied). “Unverified” means Crossref could not confirm a close match; “skipped” means a lookup failed or reached its limit. Neither status proves a citation is fabricated." : "Online verification was off, so only internal consistency was checked."),
    h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Reference", "Year", "Cited", "Status"].map(x => h("th", {}, x)))),
      h("tbody", {}, c.references.map(r => h("tr", {},
        h("td", {}, r.n),
        h("td", { class: "raw" }, r.raw,
          r.match && h("small", {}, `Crossref candidate: ${r.match.title || "title unavailable"} (${r.match.year ?? "year unavailable"}) · title match ${Math.round((r.match.title_score || 0) * 100)}%${r.match.first_author ? ` · first author ${r.match.first_author}${r.match.author_match ? " matches" : " differs"}` : ""} · ${r.match.reason}`),
          [...new Set(r.flags.filter(flag => !r.match || flag !== r.match.reason))].length > 0
            && h("small", {}, [...new Set(r.flags.filter(flag => !r.match || flag !== r.match.reason))].join(" · "))),
        h("td", {}, r.year ?? "–"), h("td", {}, r.cites + "×"),
        h("td", {}, h("span", { class: "badge " + r.status }, r.status))))))));
}

function download(name, type, body) {
  const a = h("a", { href: URL.createObjectURL(new Blob([body], { type })), download: name });
  a.click(); URL.revokeObjectURL(a.href);
}
function exportBaseName() {
  return (R.meta.filename || "paper").replace(/\.[^.]+$/, "").replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 80) || "paper";
}
$("#x-json").addEventListener("click", () => { const { text, ...rest } = R; download(`${exportBaseName()}-review.json`, "application/json", JSON.stringify(rest, null, 2)); });
$("#x-md").addEventListener("click", () => {
  const s = R.summary, v = R.review, c = R.citations.stats;
  const L = a => (a.length ? a.map(x => `- ${x}`).join("\n") : "- Not stated");
  const md = [`# Review: ${s.title}`, `*${R.meta.filename} · ${R.meta.model}* · **Recommendation: ${v.recommendation}**`, "",
    "## Summary", s.tldr, "", `**Research question:** ${s.research_question}`, "", `**Approach:** ${s.approach}`, "", "### Key findings", L(s.key_findings), "",
    "## Overall assessment", v.overall, "", "### Fix these first", v.priorities.map((p, i) => `${i + 1}. ${p}`).join("\n"), "",
    "## Findings", ...v.findings.filter(f => f.grounded).map(f => `### ${f.id}. ${f.title}\n*${f.severity} · ${f.category}*\n\n> ${f.quote}\n\n${f.explanation}\n\n**Fix:** ${f.fix}\n`),
    "## Citations", `${c.references ?? 0} references, ${c.in_text ?? 0} in-text citations, ${c.dangling ?? 0} dangling, ${c.uncited ?? 0} uncited.`,
    ...R.citations.issues.map(i => `- ${i.text}`)].join("\n");
  download(`${exportBaseName()}-review.md`, "text/markdown", md);
});

try {
  const active = JSON.parse(sessionStorage.getItem(ACTIVE_KEY) || "null");
  if (active?.batchId) {
    activeBatchId = active.batchId;
    show("v-run");
    pollBatch(activeBatchId);
  } else if (active?.jobId && sessionStorage.getItem(`reacher.job.${active.jobId}`)) {
    activeJobId = active.jobId;
    show("v-run");
    poll(activeJobId);
  }
} catch {
  sessionStorage.removeItem(ACTIVE_KEY);
}
