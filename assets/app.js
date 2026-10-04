/* HireRadar — client-side filtering over data/jobs.json */
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

  // --- description beautifier: turns wall-of-text postings into readable HTML ---
  const HEADER_WORDS = [
    "position summary", "role and responsibilities", "responsibilities",
    "skills and qualifications", "qualifications", "requirements",
    "preferred qualifications", "minimum qualifications", "basic requirements",
    "about us", "about the company", "about the role", "about the team",
    "benefits", "what you'll do", "what you will do", "nice to have",
    "job description", "overview", "the role", "your responsibilities",
    "equal opportunity", "compensation", "salary range", "how to apply",
    "job duties", "key responsibilities", "education and experience"
  ];
  const MULTIWORD_HEADERS = HEADER_WORDS.filter(h => h.split(" ").length > 1);

  function isHeaderSentence(s) {
    const clean = s.replace(/[:.\s]+$/, "").trim();
    if (!clean || clean.split(/\s+/).length > 8) return false;
    const low = clean.toLowerCase();
    if (HEADER_WORDS.includes(low)) return true;
    if (/^[A-Z][A-Za-z'’&/()\- ]{2,60}:$/.test(s.trim())) return true;   // Title Case + colon
    if (/^[A-Z][A-Z'’&/()\- ]{2,60}$/.test(clean)) return true;           // ALL CAPS
    return false;
  }

  function isBulletSentence(s) {
    return /^[•·▪●○\-*–—]\s*/.test(s) || /^\d{1,2}[.)]\s+/.test(s) || /^\(\d{1,2}\)\s*/.test(s);
  }

  function stripBullet(s) {
    return s.replace(/^[•·▪●○\-*–—]\s*/, "").replace(/^\d{1,2}[.)]\s+/, "").replace(/^\(\d{1,2}\)\s*/, "");
  }

  function sentencesOf(block) {
    // split on sentence boundaries without lookbehind (broad browser support)
    const parts = block.split(/([.!?;]+)\s+(?=[A-Z0-9("“‘•\-*])/);
    const out = [];
    for (let i = 0; i < parts.length; i += 2)
      out.push(((parts[i] || "") + (parts[i + 1] || "")).trim());
    return out.filter(Boolean);
  }

  function formatBlocks(raw) {
    let t = String(raw || "");
    // 1. HTML -> text: block tags become newlines, the rest stripped
    t = t.replace(/<\s*(?:br|p|div|li|ul|ol|h[1-6]|tr|table)[^>]*>/gi, "\n").replace(/<[^>]+>/g, "");
    t = t.replace(/&nbsp;/gi, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<")
         .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#0?39;/g, "'");
    // 2. inline bullets onto their own lines
    t = t.replace(/[ \t]+([•·▪●○])/g, "\n$1");
    // 3. inline numbered items "(1) ... (2) ..." -> bullets
    if ((t.match(/\(\d{1,2}\)/g) || []).length >= 2) t = t.replace(/\s*\(\d{1,2}\)\s*/g, "\n• ");
    // 4. multi-word section headers buried in the text -> marked header lines
    const mw = MULTIWORD_HEADERS.map(h => h.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
    t = t.replace(new RegExp("\\b(" + mw + ")\\b\\s*:?", "gi"), (m, h, off, str) => {
      const after = (str.slice(off + m.length).trimStart()[0]) || "";
      if (off === 0 || (/[A-Z]/.test(after) && after === after.toUpperCase()))
        return "\n##" + h.trim() + "\n";
      return m;
    });
    t = t.replace(/\n{3,}/g, "\n\n");

    const chunks = t.split("\n").map(s => s.trim()).filter(Boolean);
    const blocks = [];
    let para = [], list = [];
    const flushPara = () => {
      if (!para.length) return;
      for (let i = 0; i < para.length; i += 3)  // max 3 sentences per paragraph
        blocks.push(`<p>${esc(para.slice(i, i + 3).join(" "))}</p>`);
      para = [];
    };
    const flushList = () => {
      if (!list.length) return;
      blocks.push(`<ul>${list.map(li => `<li>${esc(li)}</li>`).join("")}</ul>`);
      list = [];
    };
    for (const block of chunks) {
      if (block.startsWith("##")) {
        flushPara(); flushList();
        blocks.push(`<h4>${esc(block.slice(2).trim())}</h4>`);
        continue;
      }
      for (const s of sentencesOf(block)) {
        if (isHeaderSentence(s)) {
          flushPara(); flushList();
          blocks.push(`<h4>${esc(s.replace(/[:.\s]+$/, ""))}</h4>`);
        } else if (isBulletSentence(s)) {
          flushPara();
          list.push(stripBullet(s));
        } else {
          flushList();
          para.push(s);
        }
      }
    }
    flushPara(); flushList();
    return blocks;
  }

  function formatDescription(raw) {
    const blocks = formatBlocks(raw);
    return blocks.length ? blocks.join("")
      : `<p>${esc(String(raw || "").replace(/\s+/g, " ").trim().slice(0, 4000))}</p>`;
  }

  const fmtCache = new Map();  // descriptions don't change between renders
  function formattedBlocks(job) {
    const key = job.id || job.apply_url;
    if (!fmtCache.has(key)) fmtCache.set(key, formatBlocks(job.description));
    return fmtCache.get(key);
  }

  function cardHTML(job, idx) {
    const tags = (job.tags || []).map(t =>
      `<span class="tag ${tagClass(t)}">${esc(t)}</span>`).join("");
    const blocks = formattedBlocks(job);
    const PREVIEW_BLOCKS = 3;  // show the beautified text right away; "more" reveals the rest
    const preview = blocks.slice(0, PREVIEW_BLOCKS).join("");
    const rest = blocks.slice(PREVIEW_BLOCKS).join("");
    const desc = rest
      ? `<div class="desc desc-full">${preview} <span class="more" data-i="${idx}">more</span></div>
         <div class="desc desc-full" data-full="${idx}" hidden>${preview}${rest}</div>`
      : `<div class="desc desc-full">${preview || formatDescription(job.description)}</div>`;
    const email = job.recruiter_email
      ? `<a href="mailto:${esc(job.recruiter_email)}">${esc(job.recruiter_email)}</a>` : "";
    const phone = job.recruiter_phone
      ? `<a href="tel:${esc(job.recruiter_phone.replace(/[^+\d]/g, ""))}">${esc(job.recruiter_phone)}</a>` : "";
    const contact = (email || phone)
      ? `<div class="contact"><span>Recruiter: ${email}${email && phone ? " · " : ""}${phone}</span></div>`
      : `<div class="contact"><span class="nolink">No recruiter contact listed</span></div>`;
    const isNew = job.first_seen_at &&
      (Date.now() - new Date(job.first_seen_at).getTime()) < 3 * 86400000;
    return `<article class="card">
      <h2><a href="${esc(job.apply_url)}" target="_blank" rel="noopener">${esc(job.title)}</a></h2>
      <p class="company">${esc(job.company)}${job.remote ? " · Remote" : ""}</p>
      <div class="meta"><span>${esc(job.location || "Location not listed")}</span><span>Posted ${esc(fmtDate(job.posted_at))}</span>${isNew ? `<span class="new-badge">New</span>` : ""}</div>
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

  function tier(job) {
    // Priority order: Cincinnati first, then remote, then everything else.
    if ((job.location || "").toLowerCase().includes("cincinnati")) return 0;
    if (job.remote) return 1;
    return 2;
  }

  function render() {
    const jobs = state.jobs.filter(matches)
      .sort((a, b) => tier(a) - tier(b) || new Date(b.posted_at || 0) - new Date(a.posted_at || 0));
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
