/* Visa Jobs Board — client-side filtering over data/jobs.json */
(function () {
  "use strict";

  const state = { jobs: [], meta: {} };
  const els = {
    q: document.getElementById("q"),
    loc: document.getElementById("loc"),
    pills: document.getElementById("tag-pills"),
    source: document.getElementById("source"),
    age: document.getElementById("age"),
    remoteOnly: document.getElementById("remote-only"),
    hasContact: document.getElementById("has-contact"),
    reset: document.getElementById("reset"),
    results: document.getElementById("results"),
    empty: document.getElementById("empty"),
    count: document.getElementById("job-count"),
    updated: document.getElementById("updated-at"),
    dice: document.getElementById("dice-link"),
    monster: document.getElementById("monster-link"),
  };
  const activeTags = new Set();

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function tagClass(t) {
    return { "C2C": "c2c", "W-2": "w2", "H-1B": "h1b", "OPT": "opt", "STEM OPT": "stem" }[t] || "";
  }

  function fmtDate(iso) {
    if (!iso) return "date unknown";
    const d = new Date(iso);
    if (isNaN(d)) return "date unknown";
    const days = Math.floor((Date.now() - d.getTime()) / 86400000);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    return days + " days ago";
  }

  function snippet(text, maxLen) {
    const t = (text || "").replace(/\s+/g, " ").trim();
    if (t.length <= maxLen) return { short: t, long: null };
    const cut = t.lastIndexOf(" ", maxLen);
    return { short: t.slice(0, cut > 0 ? cut : maxLen), long: t };
  }

  function cardHTML(job, idx) {
    const tags = (job.tags || []).map(t =>
      `<span class="tag ${tagClass(t)}">${esc(t)}</span>`).join("");
    const sn = snippet(job.description, 220);
    const desc = sn.long
      ? `<p class="desc">${esc(sn.short)}… <span class="more" data-i="${idx}">more</span></p>
         <p class="desc" data-full="${idx}" hidden>${esc(sn.long)}</p>`
      : `<p class="desc">${esc(sn.short)}</p>`;
    const email = job.recruiter_email
      ? `<a href="mailto:${esc(job.recruiter_email)}">${esc(job.recruiter_email)}</a>` : "";
    const phone = job.recruiter_phone
      ? `<a href="tel:${esc(job.recruiter_phone.replace(/[^+\d]/g, ""))}">${esc(job.recruiter_phone)}</a>` : "";
    const contact = (email || phone)
      ? `<div class="contact"><span>Recruiter: ${email}${email && phone ? " · " : ""}${phone}</span></div>`
      : `<div class="contact"><span class="nolink">No recruiter contact listed</span></div>`;
    return `<article class="card">
      <h2><a href="${esc(job.apply_url)}" target="_blank" rel="noopener">${esc(job.title)}</a></h2>
      <p class="company">${esc(job.company)}${job.remote ? " · Remote" : ""}</p>
      <div class="meta"><span>${esc(job.location || "Location not listed")}</span><span>${esc(fmtDate(job.posted_at))}</span></div>
      <div class="tags">${tags}</div>
      ${desc}
      ${contact}
      <div class="apply-row">
        <a class="apply" href="${esc(job.apply_url)}" target="_blank" rel="noopener">Apply</a>
        <span class="src">via ${esc(job.source || "job board")}</span>
      </div>
    </article>`;
  }

  function matches(job) {
    const q = els.q.value.trim().toLowerCase();
    if (q) {
      const hay = `${job.title} ${job.company} ${job.description}`.toLowerCase();
      if (!q.split(/\s+/).every(w => hay.includes(w))) return false;
    }
    const loc = els.loc.value.trim().toLowerCase();
    if (loc && !(job.location || "").toLowerCase().includes(loc) &&
        !(loc === "remote" && job.remote)) return false;
    if (activeTags.size && ![...activeTags].every(t => (job.tags || []).includes(t))) return false;
    if (els.source.value && job.source !== els.source.value) return false;
    const ageDays = parseInt(els.age.value, 10);
    if (ageDays && job.posted_at) {
      const d = new Date(job.posted_at);
      if (!isNaN(d) && (Date.now() - d.getTime()) > ageDays * 86400000) return false;
    }
    if (els.remoteOnly.checked && !job.remote) return false;
    if (els.hasContact.checked && !(job.recruiter_email || job.recruiter_phone)) return false;
    return true;
  }

  function updateDeepLinks() {
    const q = els.q.value.trim();
    const loc = els.loc.value.trim();
    if (q || loc) {
      els.dice.href = "https://www.dice.com/jobs?q=" + encodeURIComponent(q) +
        (loc ? "&location=" + encodeURIComponent(loc) : "");
      els.monster.href = "https://www.monster.com/jobs/search?q=" + encodeURIComponent(q) +
        (loc ? "&where=" + encodeURIComponent(loc) : "");
    }
  }

  function render() {
    const jobs = state.jobs.filter(matches)
      .sort((a, b) => new Date(b.posted_at || 0) - new Date(a.posted_at || 0));
    els.results.innerHTML = jobs.map(cardHTML).join("");
    els.empty.hidden = jobs.length > 0;
    els.count.textContent = jobs.length.toLocaleString();
    updateDeepLinks();
  }

  els.pills.addEventListener("click", e => {
    const btn = e.target.closest("button[data-tag]");
    if (!btn) return;
    const t = btn.dataset.tag;
    if (activeTags.has(t)) { activeTags.delete(t); btn.setAttribute("aria-pressed", "false"); }
    else { activeTags.add(t); btn.setAttribute("aria-pressed", "true"); }
    render();
  });

  ["q", "loc"].forEach(id => els[id].addEventListener("input", render));
  [els.source, els.age].forEach(el => el.addEventListener("change", render));
  [els.remoteOnly, els.hasContact].forEach(el => el.addEventListener("change", render));

  els.reset.addEventListener("click", () => {
    els.q.value = ""; els.loc.value = ""; els.source.value = "";
    els.age.value = "7"; els.remoteOnly.checked = false; els.hasContact.checked = false;
    activeTags.clear();
    els.pills.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", "false"));
    render();
  });

  els.results.addEventListener("click", e => {
    const more = e.target.closest(".more");
    if (!more) return;
    const full = els.results.querySelector(`[data-full="${more.dataset.i}"]`);
    if (full) { full.hidden = false; more.parentElement.hidden = true; }
  });

  fetch("data/jobs.json")
    .then(r => { if (!r.ok) throw new Error("jobs.json not found"); return r.json(); })
    .then(data => {
      state.jobs = data.jobs || [];
      state.meta = data.meta || {};
      const sources = [...new Set(state.jobs.map(j => j.source).filter(Boolean))].sort();
      sources.forEach(s => {
        const o = document.createElement("option");
        o.value = s; o.textContent = s;
        els.source.appendChild(o);
      });
      els.updated.textContent = state.meta.generated_at
        ? new Date(state.meta.generated_at).toLocaleString() : "never";
      render();
    })
    .catch(() => {
      els.results.innerHTML = "";
      els.empty.hidden = false;
      els.empty.textContent = "Job data is not available yet — the first scheduled refresh will populate it.";
    });
})();
