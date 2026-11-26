"use strict";
/* Talks to webapp.Api through pywebview (window.pywebview.api). Open index.html?demo to use demo.js instead. */
(() => {
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
let api = null, V = null, logNext = 0, pollTimer = null, lastPrompt = null, screen = "tune";

// ------------------------------------------------------------------ helpers
function toast(text, ok = false) {
  const t = $("toast");
  t.textContent = text; t.className = "toast" + (ok ? " ok" : ""); t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => { t.hidden = true; }, ok ? 4000 : 9000);
}
async function call(name, ...args) {
  try { return await api[name](...args); }
  catch (e) { toast("Something went wrong: " + (e && e.message || e)); return {ok: false}; }
}
/** Modal dialog. Resolves true for OK, false for Cancel/Escape. */
function dialog({title, html, ok = "OK", cancel = "Cancel", prompt = false}) {
  const d = $("dlg");
  d.className = prompt ? "prompt" : "";
  $("dlg-title").textContent = title;
  $("dlg-body").innerHTML = html;
  $("dlg-ok").textContent = ok || "";
  $("dlg-ok").hidden = !ok;
  $("dlg-cancel").textContent = cancel || "";
  $("dlg-cancel").hidden = !cancel;
  return new Promise(res => {
    const done = v => { d.close(); $("dlg-ok").onclick = $("dlg-cancel").onclick = d.oncancel = null; res(v); };
    $("dlg-ok").onclick = () => done(true);
    $("dlg-cancel").onclick = () => done(false);
    d.oncancel = e => { e.preventDefault(); if (cancel) done(null); };  // Escape: neither button
    d.showModal();
    $("dlg-ok").focus();
  });
}
function show(name) {
  screen = name;
  for (const s of ["tune", "results", "backups"]) {
    $(s).hidden = s !== name;
    document.querySelector(`.nav[data-screen=${s}]`).setAttribute("aria-current", s === name);
  }
  if (name === "results") drawChart();
}
const mins = s => s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`;

// ------------------------------------------------------------------ render
function render(v) {
  V = v;
  if (v.error) { toast(v.error); return; }
  $("machine").textContent = v.machine || "";
  $("version").textContent = v.version ? "Version " + v.version : "";
  $("version-top").textContent = v.version ? "v" + v.version.split(" ")[0] : "";
  $("version-top").title = v.version || "";
  $("subtitle").textContent = v.subtitle;
  $("readouts").innerHTML = v.readouts.map(r =>
    `<div><small>${esc(r.label)}</small><b>${esc(r.value)}</b><em class="${r.label === "Limit" ? "mute" : ""}">${esc(r.note)}</em></div>`).join("");
  const bad = v.findings.find(f => f.level === "bad" && f.action);
  $("alert").hidden = !bad;
  if (bad) {
    $("alert").innerHTML = `<p><strong>${esc(bad.title)}.</strong> ${esc(bad.detail)}</p><button class="btn small" data-action="${esc(bad.action.id)}">${esc(bad.action.label)}</button>`;
  }
  $("finds").innerHTML = v.findings.filter(f => f !== bad).map(f =>
    `<li><span class="dot ${esc(f.level)}"></span><div><strong>${esc(f.title)}</strong>${f.detail ? `<p>${esc(f.detail)}</p>` : ""}</div>` +
    (f.action ? `<button class="ghost" data-action="${esc(f.action.id)}">${esc(f.action.label)}</button>` : "<span></span>") + "</li>").join("");
  $("go").textContent = v.next.label;
  $("go-why").textContent = v.next.why;
  const fv = v.focus.value;
  $("focus").value = fv;
  setPick(fv);
  if (v.notice) toast(v.notice, true);
  // results
  $("headline").textContent = v.headline;
  $("summary").innerHTML = v.summary.map(p => `<p>${esc(p)}</p>`).join("");
  $("tests").innerHTML = v.tests.length ? v.tests.map(t =>
    `<tr${t.stale ? ' class="stale"' : ""}><td class="nowrap">${esc(t.when)}</td><td>${esc(t.test)}${t.stale ? ' <span class="tag info">Other settings</span>' : ""}</td><td class="n">${esc(t.low1)}</td><td class="n">${esc(t.p99)}</td>` +
    `<td><span class="tag ${esc(t.level)}">${esc(t.verdict)}</span></td>` +
    `<td class="n"><button class="ghost" data-report="${esc(t.id)}">Report</button></td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">No tests yet.</td></tr>`;
  if (screen === "results") drawChart();
  renderBackups(v.backups);
  if (v.job) showJob(v.job, true);
}
function setPick(n) {
  $("pick-label").textContent = V ? V.focus_labels[n] : "";
  $("pick-text").textContent = V ? V.focus.pick : "";
}
function renderBackups(b) {
  if (!b || !b.available) {
    $("bk-rows").innerHTML = `<tr><td colspan="4" class="empty">${esc(b ? b.text : "")}</td></tr>`;
    $("bk-now").hidden = true;
    return;
  }
  $("bk-now").hidden = false;
  $("bk-rows").innerHTML = b.items.length ? b.items.map(i =>
    `<tr><td>${esc(i.when)}</td><td>${esc(i.why)}${i.original ? ' <span class="tag good">Original</span>' : ""}</td>` +
    `<td>${esc(i.files.join(", "))}</td><td class="n"><button class="ghost" data-restore="${esc(i.id)}">Restore</button></td></tr>`).join("")
    : `<tr><td colspan="4" class="empty">No backups yet.</td></tr>`;
  $("bk-note").textContent = (b.locked ? "This version never changes iRacing settings. " : "") +
    "Restoring saves your current settings first, so it can be undone too. Settings folder: " + b.folder;
}

// ------------------------------------------------------------------ chart (1% low per setup, in test order)
function drawChart() {
  const s = $("chart");
  const pts = (V && V.chart) || [];
  const W = s.clientWidth, H = s.clientHeight;
  if (!W) return;
  if (!pts.length) { s.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">No measurements yet</text>`; return; }
  const l = 44, r = 24, t = 30, b = 58;
  const top = Math.max(50, Math.ceil(Math.max(...pts.map(p => p.value)) * 1.15 / 25) * 25);
  const n = pts.length, step = (W - l - r) / Math.max(1, n);
  const x = i => l + step * (i + 0.5), y = v => t + (H - t - b) * (1 - v / top);
  let g = "";
  for (let v = 0; v <= top; v += top / 4) {
    g += `<line x1="${l}" x2="${W - r}" y1="${y(v)}" y2="${y(v)}" class="grid"/><text x="${l - 8}" y="${y(v) + 4}" text-anchor="end">${Math.round(v)}</text>`;
  }
  // bars, not a line: setups are separate measurements, not a time series
  const bw = Math.min(56, step * 0.55);
  const lab = (txt, max) => txt.length > max ? txt.slice(0, max - 1) + "…" : txt;
  const maxChars = Math.max(6, Math.floor(step / 7.5));
  pts.forEach((p, i) => {
    // "best" only among tests on the current settings; older settings are drawn faded
    const best = !p.stale && p.value === Math.max(...pts.filter(q => !q.stale).map(q => q.value));
    g += `<rect x="${x(i) - bw / 2}" y="${y(p.value)}" width="${bw}" height="${y(0) - y(p.value)}" rx="4" class="bar${best ? " best" : ""}${p.stale ? " stale" : ""}"><title>${esc(p.label)}: ${p.value} fps (${esc(p.when)})${p.stale ? ", other graphics settings" : ""}</title></rect>`;
    g += `<text x="${x(i)}" y="${y(p.value) - 8}" text-anchor="middle" class="val">${Math.round(p.value)}</text>`;
    g += `<text x="${x(i)}" y="${H - 34}" text-anchor="middle">${esc(lab(p.label, maxChars))}</text><text x="${x(i)}" y="${H - 16}" text-anchor="middle">${esc(lab(p.when, maxChars))}</text>`;
  });
  s.innerHTML = g;
}

// ------------------------------------------------------------------ tests
function chosenTest() { return document.querySelector("input[name=test]:checked").value; }
function lengths() {
  return {seconds: Number($("len-seconds").value), runs: Number($("len-runs").value), warmup: Number($("len-warmup").value)};
}
async function startTest(test, extra = {}) {
  const opts = {...lengths(), ...extra};
  const auto = test === "auto";  // first run: test busy apps straight away if there are any, else measure
  if (auto || test === "background") {
    test = "background";
    const d = $("dlg");
    dialog({title: "Looking for busy apps", html: "<p>Measuring which apps use the processor. This takes 5 seconds…</p>", ok: "", cancel: ""});
    const r = startTest.scan = await call("scan_background");
    d.close();
    if (!r.ok) { toast(r.error || "The scan failed."); return; }
    if (!r.apps.length && auto) test = "repeat";
    else if (!r.apps.length) { await dialog({title: "Nothing to test", html: "<p>No busy background apps were found, so pausing apps can't help.</p>", cancel: ""}); return; }
  }
  if (test === "background") {
    const r = startTest.scan;
    const go = await dialog({title: "Pause these apps for half the runs?",
      // tick only apps that use real CPU: tiny ones can't explain stutter and pausing them only adds risk
      html: "<ul>" + r.apps.map(a => `<li><label><input type="checkbox" ${a.cores >= Math.max(0.25, 0.2 * r.apps[0].cores) ? "checked" : ""} value="${esc(a.name)}"> ${esc(a.name)} <span class="mute">(${a.cores} cores)</span></label></li>`).join("") + "</ul>" +
        "<p>Small ones are left unticked. Tick them only if you suspect them. While paused they stop responding, for example SimHub dashes and wheel LEDs. They are resumed straight after each run and at the end, even if the app crashes.</p>",
      ok: "Start the test", cancel: auto ? "Just measure" : "Cancel"});
    if (go === false && auto) return startTest("repeat", extra);
    if (!go) return;
    opts.names = [...$("dlg-body").querySelectorAll("input:checked")].map(i => i.value);
    if (!opts.names.length) { toast("No apps chosen."); return; }
  }
  if (test === "guided") {
    opts.a = extra.a || $("ab-a").value; opts.b = extra.b || $("ab-b").value;
  }
  let r = await call("start", test, opts);
  if (!r.ok && r.fix === "settings_mismatch") {
    if (!await dialog({title: "Replay and race graphics differ", html: `<p>${esc(r.error)}</p>`, ok: "Test anyway"})) return;
    opts.ignore_settings = true;
    r = await call("start", test, opts);
  }
  if (!r.ok) {
    if (r.fix === "relaunch_admin") {
      if (await dialog({title: "Restart as administrator?", html: `<p>${esc(r.error)}</p>`, ok: "Restart as administrator"})) relaunch();
    } else toast(r.error || "The test didn't start.");
    return;
  }
  logNext = 0; $("run-log").innerHTML = "";
  showJob({title: r.title, state: "running", progress: 0, elapsed_s: 0, eta_s: r.eta_min * 60, logs: []});
  poll();
}
function showJob(j, fromView = false) {
  if (fromView && j.state !== "running") return;
  $("start-card").hidden = true; $("run-card").hidden = false;
  $("run-title").textContent = j.title;
  const pct = Math.round((j.progress || 0) * 100);
  $("run-bar").firstElementChild.style.width = pct + "%";
  $("run-bar").setAttribute("aria-valuenow", pct);
  const running = j.state === "running";
  $("run-eta").textContent = running ? `${mins(j.elapsed_s)} of about ${mins(j.eta_s)}. Leave the PC alone unless the app asks you to switch something.`
    : {done: "Finished.", stopped: "Stopped. Nothing was changed.", error: "The test failed. See the last line below."}[j.state] || "";
  $("run-stop").hidden = !running; $("run-close").hidden = running;
  if (j.logs && j.logs.length) {
    const ul = $("run-log");
    ul.insertAdjacentHTML("beforeend", j.logs.map(l => `<li><span>${esc(l.t)}</span>${esc(l.text)}</li>`).join(""));
    while (ul.children.length > 200) ul.firstElementChild.remove();
    ul.scrollTop = ul.scrollHeight;
  }
  if (j.next !== undefined) logNext = j.next;
  if (running && !pollTimer) pollTimer = setTimeout(poll, pollEvery());
}
async function poll() {
  pollTimer = null;
  const r = await call("poll", logNext);
  const j = r && r.job;
  if (!j) return;
  showJob(j);
  if (j.prompt && j.prompt.id !== lastPrompt) {
    lastPrompt = j.prompt.id;
    dialog({title: j.prompt.title, html: `<p>${esc(j.prompt.text)}</p>`, ok: "Done", cancel: "Stop the test", prompt: true})
      .then(ok => ok ? call("answer", j.prompt.id) : call("stop"));
  }
  if (!j.prompt && lastPrompt && $("dlg").open && $("dlg").classList.contains("prompt")) { $("dlg").close(); lastPrompt = null; }
  if (j.state === "running") { pollTimer = setTimeout(poll, pollEvery()); return; }
  if (j.state === "done" && j.result) {
    refresh().then(() => toast((j.result.lines || []).join(" ") || "Test finished.", true));
  }
}
// The app must not cost the sim CPU while it measures: slow polling, slower still when hidden behind iRacing.
const pollEvery = () => document.hidden ? 5000 : 2000;

// ------------------------------------------------------------------ updates
async function watchUpdate(tries = 0) {
  const u = await call("update_status");
  if (u.state === "checking" && tries < 20) { setTimeout(() => watchUpdate(tries + 1), 3000); return; }
  $("update").hidden = !u.available;
}
async function installUpdate() {
  const ok = await dialog({title: "Install the newest build?",
    html: "<p>Downloads it from this app's GitHub page, checks it against the published checksum, replaces this exe and restarts. Your results and backups are kept.</p>",
    ok: "Update and restart"});
  if (!ok) return;
  $("update").innerHTML = "<p>Downloading the newest build…</p>";
  const r = await call("update_install");
  if (!r.ok) { toast(r.error || "Update failed."); watchUpdate(); }
}

async function relaunch() {
  const r = await call("relaunch_admin");
  if (!r.ok) toast(r.error || "Couldn't restart as administrator.");
}
async function refresh() { render(await call("view")); }

// ------------------------------------------------------------------ events
function wire() {
  document.querySelectorAll(".nav").forEach(b => b.addEventListener("click", () => show(b.dataset.screen)));
  $("focus").addEventListener("input", e => setPick(+e.target.value));
  $("focus").addEventListener("change", e => call("set_focus", +e.target.value));
  $("go").addEventListener("click", () => {
    const n = V.next;
    startTest(n.test, n.test === "guided" ? {a: n.a, b: n.b} : {});
  });
  $("other-tests").addEventListener("click", e => {
    const box = $("tests-box"); box.hidden = !box.hidden;
    e.target.setAttribute("aria-expanded", !box.hidden);
  });
  document.querySelectorAll("input[name=test]").forEach(r => r.addEventListener("change", () => { $("ab-names").hidden = chosenTest() !== "guided"; }));
  $("go-chosen").addEventListener("click", () => startTest(chosenTest()));
  $("run-stop").addEventListener("click", () => call("stop"));
  $("run-close").addEventListener("click", () => { $("run-card").hidden = true; $("start-card").hidden = false; });
  $("update-go").addEventListener("click", installUpdate);
  $("classic").addEventListener("click", async () => {
    const r = await call("open_classic");
    if (!r.ok) toast(r.error || "Couldn't open the classic window.");
  });
  $("open-folder").addEventListener("click", () => call("open_results_folder"));
  $("bk-now").addEventListener("click", async () => {
    const r = await call("backup_now");
    r.ok ? (toast("Backup saved.", true), refresh()) : toast(r.error || "Backup failed.");
  });
  document.body.addEventListener("click", async e => {
    const a = e.target.closest("[data-action]"), rep = e.target.closest("[data-report]"), rs = e.target.closest("[data-restore]");
    if (a) {
      const id = a.dataset.action;
      if (id === "relaunch_admin") relaunch();
      else if (id.startsWith("test:")) {
        const [, test, A, B] = id.split(":");
        startTest(test, test === "guided" ? {a: A, b: B} : {});
      }
    } else if (rep) {
      const r = await call("open_report", rep.dataset.report);
      if (!r.ok) toast(r.error || "Couldn't open the report.");
    } else if (rs) {
      const item = V.backups.items.find(i => i.id === rs.dataset.restore);
      if (await dialog({title: "Restore these settings?", html: `<p>Puts back your iRacing settings from ${esc(item.when)}. Close iRacing first.</p><p>Your current settings are saved first, so this can be undone.</p>`, ok: "Restore"})) {
        const r = await call("restore", item.id);
        r.ok ? (toast("Settings restored.", true), refresh()) : toast(r.error || "Restore failed.");
      }
    }
  });
  new ResizeObserver(() => { if (screen === "results") drawChart(); }).observe($("chart"));
}

async function boot() {
  wire();
  if (location.search.includes("demo")) {
    await new Promise(res => { const s = document.createElement("script"); s.src = "demo.js"; s.onload = res; document.body.appendChild(s); });
    api = window.DEMO_API;
  } else {
    if (!window.pywebview || !window.pywebview.api) await new Promise(res => window.addEventListener("pywebviewready", res, {once: true}));
    api = window.pywebview.api;
  }
  await refresh();
  watchUpdate();
  const m = location.hash.slice(1);
  if (["tune", "results", "backups"].includes(m)) show(m);
}
boot();
})();
