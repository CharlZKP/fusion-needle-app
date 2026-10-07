/* Fusion Needle UI. Plain DOM, no dependencies, nothing loaded from the network. */
(function () {
  "use strict";

  const TOKEN = document.querySelector('meta[name="fn-token"]').content;
  const $ = (id) => document.getElementById(id);
  let S = null;                 // last /api/state
  let TOOLS = [];               // catalogue with call_info, from /api/tools
  let toolsKey = "";
  let open = new Set();         // step indexes the user expanded
  let closed = new Set();       // ... and collapsed
  let editing = null;           // {index, calls: [{name, values: {key: text}}]}
  let rewriting = null;         // {index, text}: the rewrite box of that step is showing
  let lastSignature = "";
  let lostContact = 0;
  let HELP = null;              // /api/help: grouped example requests
  let QUICK = [];               // short starter requests, from examples.json ("quick")
  let stopped = false;
  let firstState = true;
  let wasFinished = false;
  let diagOpen = false;         // the Diagnostics window is showing
  let diagCalls = null;         // last /api/diagnostics
  let diagSignature = "";
  let exportNote = "";
  let setupSignature = "";
  let startersSignature = "";
  let waitingKey = "";          // the step + status the page last drew attention to
  let armAt = 0;                // a focused Approve button ignores the keyboard until then
  const openDetails = new Set();        // "details" blocks the user opened; survive re-rendering
  let modalOpener = null;

  // ---- helpers ---------------------------------------------------------
  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "value") node.value = value;
      else if (value === true) node.setAttribute(key, "");
      else node.setAttribute(key, value);
    }
    return add(node, children);
  }

  // append children; arrays may be nested, null / false are skipped
  function add(node, ...children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  async function api(path, body) {
    const options = { headers: { "X-FN-Token": TOKEN } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch("/api/" + path, options);
    let data = {};
    try { data = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
    return data;
  }

  let toastTimer = 0;
  function toast(text) {
    const node = $("toast");
    node.textContent = text.charAt(0).toUpperCase() + text.slice(1);
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 5200);
  }

  async function act(path, body) {
    try {
      const out = await api(path, body || {});
      await poll(true);
      return out;
    } catch (failure) {
      toast(failure.message);
      return null;
    }
  }

  function fmt(value) { return typeof value === "string" ? value : JSON.stringify(value); }
  function words(name) { return String(name === undefined || name === null ? "" : name).replace(/_/g, " "); }
  function capital(text) { return text ? text.charAt(0).toUpperCase() + text.slice(1) : text; }
  function lastLine(text) { return String(text || "").split("\n").map((line) => line.trim()).filter(Boolean).pop() || ""; }

  const toolByName = (name) => TOOLS.find((tool) => tool.name === name);

  // "create_hole" -> "Create hole"; the catalogue's own first sentence says what it does
  function toolTitle(name) { return capital(words(name)); }
  function toolAbout(name) {
    let text = (toolByName(name) || {}).description || "";
    if (!text && HELP) {
      for (const group of HELP.groups) for (const tool of group.tools) if (tool.name === name) text = tool.description;
    }
    const cut = /^(.*?[.;])(\s|$)/.exec(text);
    return (cut ? cut[1] : text).replace(/;$/, ".");
  }

  // the values of a call, as labelled pairs
  function argsList(args) {
    const keys = Object.keys(args || {});
    if (!keys.length) return h("div", { class: "call-args" }, h("span", { class: "none", text: "No values given: the defaults are used." }));
    return h("dl", { class: "call-args" }, keys.map((key) => h("div", {}, h("dt", { text: words(key) }), h("dd", { text: fmt(args[key]) }))));
  }

  function stateChips(line) {
    return String(line || "").split(";").map((part) => part.trim()).filter(Boolean).map((part) => {
      const cut = part.indexOf(":");
      return cut < 0 ? h("span", { class: "chip", text: part })
        : h("span", { class: "chip" }, h("b", { text: words(part.slice(0, cut)) }), part.slice(cut + 1).trim());
    });
  }

  // a collapsed block for the technical side of things; stays open across re-renders once opened
  function details(key, summary, ...children) {
    const node = h("details", { class: "more" }, h("summary", { text: summary }), children);
    node.open = openDetails.has(key);
    node.addEventListener("toggle", () => { if (node.open) openDetails.add(key); else openDetails.delete(key); });
    return node;
  }

  function mark(tone) {
    const glyph = { ok: "✓", bad: "!", warn: "!", wait: "" }[tone];
    return h("span", { class: "mark " + tone, "aria-hidden": "true" }, tone === "wait" ? h("i", { class: "spin" }) : glyph);
  }

  // ---- theme -----------------------------------------------------------
  const THEMES = ["auto", "light", "dark"];
  function themeNow() {
    try { return window.localStorage.getItem("fn-theme") || "auto"; } catch (_) { return document.documentElement.getAttribute("data-theme") || "auto"; }
  }
  function showTheme(mode) {
    if (mode === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", mode);
    $("theme-label").textContent = capital(mode);
    const text = "Theme: " + (mode === "auto" ? "follows the system" : mode) + ". Click to change.";
    $("btn-theme").title = text;
    $("btn-theme").setAttribute("aria-label", text);
  }
  function nextTheme() {
    const current = document.documentElement.getAttribute("data-theme") || "auto";
    const mode = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
    try { window.localStorage.setItem("fn-theme", mode); } catch (_) { /* this launch only */ }
    showTheme(mode);
  }

  // ---- modal -----------------------------------------------------------
  function fillModal(content, wide) {
    const box = $("modal").firstElementChild;
    box.className = "modal-box" + (wide ? " wide" : "");
    box.replaceChildren(h("button", { type: "button", class: "modal-close", "aria-label": "Close", title: "Close (Esc)", onclick: hideModal, text: "×" }),
      ...content.filter(Boolean));                       // a null entry would be shown as the text "null"
    const title = box.querySelector("h1");
    if (title) { title.id = "modal-title"; box.setAttribute("aria-labelledby", "modal-title"); }
    if ($("modal").hidden) modalOpener = document.activeElement;
    $("modal").hidden = false;
    return box;
  }
  function showModal(content, wide, focus) {
    diagOpen = false;
    const box = fillModal(content, wide);
    box.scrollTop = 0;
    $("modal").scrollTop = 0;
    const first = (focus && box.querySelector(focus)) || box.querySelector("input:not([type=checkbox]), select, textarea, button.primary") || box.querySelector("button:not(.modal-close)");
    if (first) first.focus();
  }
  function hideModal() {
    $("modal").hidden = true;
    diagOpen = false;
    if (modalOpener && document.contains(modalOpener)) modalOpener.focus();
    modalOpener = null;
  }
  $("modal").addEventListener("mousedown", (event) => { if (event.target === $("modal")) hideModal(); });
  $("modal").addEventListener("keydown", (event) => {    // keep Tab inside the window
    if (event.key !== "Tab") return;
    const items = [...$("modal").querySelectorAll("button, input, select, textarea, summary, [href]")]
      .filter((node) => !node.disabled && node.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  // ---- top bar ---------------------------------------------------------
  // "downloading the model x.cact: 12.0 of 80.0 MB (15%)" -> the parts, or null
  function downloadInfo(detail) {
    const found = /^downloading the model\s+(.*?):\s*([\d.]+) of ([\d.]+) MB(?: \((\d+)%\))?/.exec(detail || "");
    return found ? { name: found[1], done: found[2], total: found[3], share: found[4] === undefined ? null : Number(found[4]) } : null;
  }

  function renderBar() {
    $("project").textContent = S.app.project + " · v" + S.app.version;
    $("about").textContent = S.app.name + " " + S.app.version + " · " + S.app.project;
    const fusion = $("pill-fusion");
    const linked = S.fusion.state === "connected";
    fusion.className = "pill " + (linked ? "ok" : "bad");
    fusion.querySelector("em").textContent = linked ? "connected" : "not connected";
    fusion.title = (linked ? (S.fusion.server || "connected") + "\n" : "") + S.fusion.url + (S.fusion.detail ? "\n" + S.fusion.detail : "");
    const model = $("pill-model");
    const m = S.model;
    const kind = { ready: "ok", starting: "wait", stopped: "", error: "bad" }[m.state] || "";
    model.className = "pill " + kind;
    const loading = m.state === "starting" ? downloadInfo(m.detail) : null;
    const label = m.state === "ready" ? "ready"
      : loading && loading.share !== null ? "downloading " + loading.share + "%"
        : m.state === "starting" ? (/^(downloading|looking)/.test(m.detail || "") ? m.detail : "starting…") : m.state === "error" ? "needs attention" : "stopped";
    model.querySelector("em").textContent = label;
    model.title = (m.state === "ready" ? m.model + (m.tuned ? "" : " (untuned)") + " · " + m.tools + " tools\n" : "")
      + (m.url || "") + (m.detail ? "\n" + m.detail : "");

    const custom = $("pill-custom");
    custom.hidden = m.source !== "custom";
    custom.querySelector("em").textContent = m.source === "custom" ? m.custom.repo : "";
    $("about").textContent += m.source === "custom" ? " · custom model " + m.custom.repo + " (" + m.custom.revision + ")" : "";

    const banner = $("banner");
    const problems = [];
    if (S.app.settings_error) problems.push(S.app.settings_error);
    if (S.session.message) problems.push(S.session.message);
    for (const text of (S.guard && S.guard.problems) || []) problems.push("Call guard: " + text);
    banner.hidden = !problems.length;
    banner.replaceChildren(...problems.map((text) => h("div", { text })));
  }

  // ---- getting ready: plain words for the model and Fusion ---------------
  // -> [what happened, what to do, "settings" when Settings is the way out]
  function explainModel(m) {
    const d = m.detail || "";
    const external = m.effective_mode === "url";
    if (m.source === "custom") {
      if (/is not the file that was downloaded/.test(d)) return ["The custom model file on this computer has changed since it was downloaded.", "Switch back to the official model, or choose the custom model again to download it afresh."];
      if (/HTTP \d{3}|could not be reached|download did not finish|does not match|is not known|no custom model/.test(d)) {
        return ["The custom model (" + m.custom.repo + ") could not be downloaded.", "Check its address and, for a private model, the access token. Or switch back to the official model."];
      }
      return ["The custom model (" + m.custom.repo + ") did not load.", "It may not be a Needle 3 model built for this app" + (S.app.engine ? " (engine cactus-needle " + S.app.engine + ")" : "") + ". Switch back to the official model, or choose another one."];
    }
    if (/no server URL set/.test(d)) return ["No model server address is set.", "Open Settings and enter the address, or choose “Start the model here”.", "settings"];
    if (/refused the token/.test(d)) return ["The model server did not accept the token.", "Open Settings and enter the server token again.", "settings"];
    if (/does not match the manifest/.test(d)) return ["The downloaded model file is damaged.", "Try again: the file is downloaded afresh."];
    if (/manifest|model repository|download did not finish|^https?:\S+ HTTP \d+/.test(d)) {
      const code = (/HTTP (\d{3})/.exec(d) || [])[1];
      if (code === "401" || code === "403") {
        return ["The model could not be downloaded: the download site refused access.",
          "This is not a problem with your computer. The model may not be published yet, or the site wants a sign-in. Try again later, or pick a model file you already have in Settings (Weights).", "settings"];
      }
      if (code === "404") return ["The model could not be downloaded: it was not found at the download site.", "Try again later, or pick a model file you already have in Settings (Weights).", "settings"];
      return ["The model could not be downloaded.", "Check the internet connection, then try again. The download is needed only once; after it the app works offline."];
    }
    if (/^weights /.test(d)) return ["The model named in Settings was not found.", "Open Settings and check “Weights”.", "settings"];
    if (external) return ["The model server is not answering.", "Start that server or check its address in Settings, then try again.", "settings"];
    if (/exited|could not start|did not come up/.test(d)) return ["The model did not start.", "Try again. If it keeps happening, open Diagnostics, export the report and send it to the people who gave you this app."];
    return ["The model stopped with an error.", "Try again. If it keeps happening, open Diagnostics, export the report and send it to the people who gave you this app."];
  }

  function setupModel() {
    const m = S.model;
    const act_ = (name) => ({ "data-act": name });
    if (m.state === "ready") return h("div", { class: "setup-item" }, mark("ok"), h("strong", { text: "The model is ready" }));
    if (m.state === "starting") {
      const loading = downloadInfo(m.detail);
      return h("div", { class: "setup-item" }, mark("wait"),
        h("strong", { text: loading ? "Downloading the model" : "Getting the model ready…" }),
        loading ? [h("p", { text: loading.done + " of " + loading.total + " MB. This happens once; later starts need no internet." }),
          h("progress", { max: "100", value: loading.share === null ? null : String(loading.share), "aria-label": "Model download" })]
          : h("p", { class: "muted", text: /^looking/.test(m.detail || "") ? "Looking for the model." : "This can take a minute, longer on the very first start." }));
    }
    if (m.state === "error") {
      const [what, todo, way] = explainModel(m);
      return h("div", { class: "setup-item" }, mark("bad"),
        h("strong", { text: what }), h("p", { text: todo }),
        h("div", { class: "row" },
          h("button", Object.assign({ type: "button", class: "primary small", onclick: () => act("model_restart"), text: "Try again" }, act_("model-retry"))),
          m.source === "custom" ? [h("button", Object.assign({ type: "button", class: "small", onclick: useOfficial, text: "Switch back to the official model" }, act_("model-official"))),
            h("button", Object.assign({ type: "button", class: "small ghost", onclick: () => sourceModal("custom"), text: "Choose another model" }, act_("model-source")))] : null,
          way === "settings" ? h("button", Object.assign({ type: "button", class: "small", onclick: settingsModal, text: "Open Settings" }, act_("model-settings"))) : null),
        details("setup-model", "Technical details", h("pre", { class: "raw", text: m.detail || "(no message)" }),
          h("button", Object.assign({ type: "button", class: "link", onclick: modelModal, text: "Show the model log" }, act_("model-log")))));
    }
    return h("div", { class: "setup-item" }, mark("warn"), h("strong", { text: "The model is stopped" }),
      h("div", { class: "row" }, h("button", Object.assign({ type: "button", class: "primary small", onclick: () => act("model_restart"), text: "Start the model" }, act_("model-retry")))));
  }

  function setupFusion() {
    if (S.fusion.state === "connected") {
      return h("div", { class: "setup-item" }, mark("ok"), h("strong", { text: "Fusion is connected" }));
    }
    return h("div", { class: "setup-item" }, mark("bad"),
      h("strong", { text: "Fusion is not connected yet" }),
      h("ol", {},
        h("li", { text: "Open Autodesk Fusion and open a design. A new, empty one is fine." }),
        h("li", { text: "In Fusion, switch on its MCP server. If you cannot find the setting, search Fusion’s help for “MCP server”." })),
      h("p", { class: "muted", text: "This page connects by itself a few seconds later. Nothing needs restarting." }),
      h("div", { class: "row" },
        h("button", { type: "button", class: "small", "data-act": "fusion-check", onclick: () => { diagnosticsModal(); runFusionCheck(); }, text: "Check Fusion" }),
        h("button", { type: "button", class: "small ghost", "data-act": "fusion-settings", onclick: settingsModal, text: "Change the address" })),
      details("setup-fusion", "Technical details", h("div", { class: "kv" },
        h("span", { class: "k", text: "looking at" }), h("span", { class: "v", text: S.fusion.url }),
        h("span", { class: "k", text: "last answer" }), h("span", { class: "v", text: S.fusion.detail || "-" }))));
  }

  function isReady() { return S.backend.ok && S.model.state === "ready" && S.fusion.state === "connected"; }

  function renderSetup() {
    const box = $("setup");
    const signature = JSON.stringify([S.backend, S.model.state, S.model.detail, S.model.effective_mode, S.model.source, S.model.custom, S.fusion.state, S.fusion.url, S.fusion.detail]);
    if (signature === setupSignature) return;
    setupSignature = signature;
    box.hidden = isReady();
    if (box.hidden) { box.replaceChildren(); return; }
    const focused = box.contains(document.activeElement) ? document.activeElement.getAttribute("data-act") : null;
    box.replaceChildren(...[
      h("h2", { text: "Getting ready" }),
      h("p", { class: "hint", text: "Both of these have to be running before a request can be sent." }),
      setupModel(), setupFusion(),
      S.backend.ok ? null : h("div", { class: "setup-item" }, mark("bad"),
        h("strong", { text: "A part of the app did not load" }),
        h("p", { text: "The installation looks incomplete. Install the app again; if that does not help, send the details below to the people who gave you this app." }),
        details("setup-backend", "Technical details", h("pre", { class: "raw", text: "Project backend: " + S.backend.error }))),
    ].filter(Boolean));
    if (focused) { const again = box.querySelector('[data-act="' + focused + '"]'); if (again) again.focus(); }
  }

  function modelModal() {
    const m = S.model;
    const [what, todo] = m.state === "error" ? explainModel(m) : ["", ""];
    showModal([
      h("h1", { text: "Model" }),
      m.state === "error" ? h("div", { class: "note bad" }, h("strong", { text: what }), todo) : null,
      h("p", { class: "hint", text: "The model is a small program on this computer that turns one request into Fusion commands. Nothing you type leaves this computer." }),
      h("h3", { text: "Details" }),
      h("div", { class: "kv" },
        h("span", { class: "k", text: "state" }), h("span", { class: "v", text: m.state + (m.detail ? ": " + m.detail : "") }),
        h("span", { class: "k", text: "mode" }), h("span", { class: "v", text: m.effective_mode + (m.managed ? " (child process of this app)" : "") }),
        h("span", { class: "k", text: "server" }), h("span", { class: "v", text: m.url || "-" }),
        h("span", { class: "k", text: "model" }), h("span", { class: "v", text: (m.model || "-") + (m.model && !m.tuned ? " (untuned)" : "") + (m.needle_version ? " · needle " + m.needle_version : "") }),
        h("span", { class: "k", text: "tools" }), h("span", { class: "v", text: m.tools + " (at most " + m.max_tools_per_step + " per step)" }),
        S.app.custom_model ? [h("span", { class: "k", text: "source" }), h("span", { class: "v", text: sourceText() })] : null),
      S.app.custom_model ? h("div", { class: "row" },
        h("button", { type: "button", class: "small", onclick: () => sourceModal(), text: "Change the model source…" }),
        m.source === "custom" ? h("button", { type: "button", class: "small", onclick: useOfficial, text: "Switch back to the official model" }) : null) : null,
      h("h3", { text: "Server log" }),
      h("pre", { class: "script", text: (m.log || []).join("\n") || "(nothing yet)" }),
      h("div", { class: "row" },
        h("button", { type: "button", onclick: () => { hideModal(); act("model_restart"); }, text: "Restart model" }),
        h("button", { type: "button", class: "ghost", onclick: hideModal, text: "Close" })),
    ], true);
  }

  // ---- model source: the official model, or the user's own from Hugging Face ----
  function sourceText() {
    const m = S.model;
    return m.source === "custom" ? "custom: " + m.custom.repo + ", revision " + m.custom.revision + ", file " + m.custom.file
      + (m.custom_checked ? "" : " (no manifest: not checked against a published checksum)")
      : "official" + (m.official_repo ? " (" + m.official_repo + ")" : "");
  }
  function megabytes(bytes) { return bytes ? (bytes / 1e6).toFixed(bytes < 1e7 ? 1 : 0) + " MB" : "unknown size"; }

  async function useOfficial() {
    const out = await act("model_source_official");
    if (out) { if (!$("modal").hidden) hideModal(); toast("Using the official model again."); }
  }

  function sourceModal(preset) {
    if (!S || !S.app.custom_model) return;
    const m = S.model;
    const kind = h("select", { id: "source-kind" }, [["official", "Official model (recommended)"], ["custom", "Custom Hugging Face model"]]
      .map(([value, label]) => h("option", { value, text: label })));
    kind.value = preset || m.source;
    const address = h("input", { id: "source-address", type: "text", spellcheck: "false", autocomplete: "off",
      value: m.custom.repo ? m.custom.repo : "", placeholder: "owner/name, or the link of the model on huggingface.co" });
    const token = h("input", { id: "source-token", type: "password", autocomplete: "off",
      placeholder: S.settings.model.hf_token_set ? "(saved; type to replace)" : "only for a private model" });
    const result = h("div", { "aria-live": "polite" });
    const go = h("button", { type: "button", class: "primary" });
    const check = h("button", { type: "button", text: "Check" });
    const custom = h("div", { class: "source-form" },
      h("label", { for: "source-address", text: "Hugging Face model id or link" }),
      h("div", { class: "inline" }, address, check),
      h("p", { class: "hint", text: "Accepted: owner/name · a huggingface.co link to the model · a link to its .cact file. Nothing is downloaded when you press Check." }),
      h("label", { for: "source-token", text: "Access token (optional)" }), token,
      h("p", { class: "hint", text: "Kept on this computer only. It is never shown again and never put in the diagnostics export." }),
      result);
    let found = null;                                    // the answer of the last successful check
    const sync = () => {
      custom.hidden = kind.value !== "custom";
      go.textContent = kind.value === "custom" ? "Continue…" : "Use the official model";
      go.disabled = kind.value === "custom" ? !(found && found.file) : m.source !== "custom";
    };
    const show = () => {
      const pickFile = found.choices.length > 1 ? h("select", { "aria-label": "Model file" },
        [found.file ? null : h("option", { value: "", text: "choose a file…" })].concat(
          found.choices.map((item) => h("option", { value: item.file, text: item.file + " (" + megabytes(item.bytes) + ")" })))) : null;
      if (pickFile) {
        pickFile.value = found.file;
        pickFile.addEventListener("change", () => {
          const chosen = found.choices.find((item) => item.file === pickFile.value);
          found = Object.assign({}, found, { file: chosen ? chosen.file : "", bytes: chosen ? chosen.bytes : 0 });
          show();
        });
      }
      result.replaceChildren(h("div", { class: "note" + (found.file ? "" : " warn") },
        h("strong", { text: found.file ? "This is what would be used" : "This repository has several model files. Choose one." }),
        h("div", { class: "kv" },
          h("span", { class: "k", text: "repository" }), h("span", { class: "v", text: found.repo + " (" + found.host + ")" }),
          h("span", { class: "k", text: "revision" }), h("span", { class: "v", text: found.revision }),
          h("span", { class: "k", text: "file" }), pickFile || h("span", { class: "v", text: found.file }),
          h("span", { class: "k", text: "size" }), h("span", { class: "v", text: found.file ? megabytes(found.bytes) : "-" }),
          h("span", { class: "k", text: "checksum" }), h("span", { class: "v", text: found.verified
            ? "listed in the repository’s manifest; the download is checked against it"
            : "none: this repository has no manifest, so the download cannot be checked" }))));
      sync();
    };
    const runCheck = async () => {
      found = null;
      sync();
      result.replaceChildren(h("p", { class: "hint" }, h("i", { class: "spin" }), " Asking Hugging Face…"));
      try {
        found = await api("model_source_check", { text: address.value, token: token.value });
        show();
      } catch (failure) {
        result.replaceChildren(h("div", { class: "note bad", role: "alert" }, h("strong", { text: "This cannot be used" }), failure.message));
      }
    };
    check.addEventListener("click", runCheck);
    address.addEventListener("input", () => { found = null; result.replaceChildren(); sync(); });
    address.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); runCheck(); } });
    kind.addEventListener("change", sync);
    go.addEventListener("click", () => {
      if (kind.value === "custom") warnCustom(found, token.value);
      else useOfficial();
    });
    sync();
    showModal([
      h("h1", { text: "Model source" }),
      h("p", { class: "hint", text: "The official model is made and tested for this app. You can use another Needle 3 model from Hugging Face instead, at your own risk. Now in use: " + sourceText() + "." }),
      h("div", { class: "source-form" }, h("label", { for: "source-kind", text: "Model source" }), kind),
      custom,
      h("div", { class: "row" },
        h("button", { type: "button", class: "ghost", onclick: hideModal, text: "Cancel" }), go),
    ], false, kind.value === "custom" ? "#source-address" : "#source-kind");
  }

  // the warning before a custom model is used; nothing is downloaded or changed until "Use this model"
  function warnCustom(found, token) {
    const engine = found.engine || S.app.engine;
    const use = async () => {
      const out = await act("model_source_use", { repo: found.repo, revision: found.revision, file: found.file, token, confirm: true });
      if (out) { hideModal(); toast("Switching to the custom model. The download starts now."); }
    };
    showModal([
      h("h1", { text: "Use a model that is not checked by this project?" }),
      h("div", { class: "kv" },
        h("span", { class: "k", text: "repository" }), h("span", { class: "v", text: found.repo + " (" + found.host + ")" }),
        h("span", { class: "k", text: "revision" }), h("span", { class: "v", text: found.revision }),
        h("span", { class: "k", text: "file" }), h("span", { class: "v", text: found.file }),
        h("span", { class: "k", text: "size" }), h("span", { class: "v", text: megabytes(found.bytes) })),
      h("ul", { class: "warning-list" },
        h("li", { text: "This model comes from a third party. This project has not made, tested or checked it." }),
        h("li", { text: "A different model can propose wrong or destructive commands for the design that is open in Fusion. The call guard and the confirmation prompts still apply, so read each step before you approve it." }),
        h("li", { text: "It must be a Needle 3 .cact file built for the engine cactus-needle" + (engine ? "==" + engine : " of this app") + " and trained on this app’s tool catalogue. Anything else fails to load or behaves badly." }),
        found.verified ? h("li", { text: "The repository has a manifest: the download is checked against the SHA-256 it lists." })
          : h("li", { text: "The repository has no manifest, so there is no checksum to check the download against. The app records the SHA-256 of the file it downloads and refuses to start if that file changes later." }),
        h("li", { text: "If you continue, a download of " + megabytes(found.bytes) + " from " + found.repo + " starts. The official model stays on this computer; you can switch back with one click." })),
      h("div", { class: "row" },
        h("button", { type: "button", id: "source-cancel", onclick: hideModal, text: "Cancel" }),
        h("button", { type: "button", class: "danger", onclick: use, text: "Use this model" })),
    ], false, "#source-cancel");
  }

  function fusionModal() {
    const linked = S.fusion.state === "connected";
    showModal([
      h("h1", { text: "Fusion" }),
      linked ? h("div", { class: "note ok" }, h("strong", { text: "Fusion is connected" }), "Requests you approve are sent to the design that is open in Fusion.")
        : h("div", { class: "note bad" }, h("strong", { text: "Fusion is not connected yet" }),
          h("ol", {},
            h("li", { text: "Open Autodesk Fusion and open a design. A new, empty one is fine." }),
            h("li", { text: "In Fusion, switch on its MCP server. If you cannot find the setting, search Fusion’s help for “MCP server”." })),
          "This connects by itself a few seconds later."),
      h("h3", { text: "Details" }),
      h("div", { class: "kv" },
        h("span", { class: "k", text: "state" }), h("span", { class: "v", text: S.fusion.state }),
        h("span", { class: "k", text: "MCP server" }), h("span", { class: "v", text: S.fusion.url }),
        h("span", { class: "k", text: "answers as" }), h("span", { class: "v", text: S.fusion.server || "-" })),
      S.fusion.detail ? h("p", { class: "hint", text: S.fusion.detail }) : null,
      h("p", { class: "hint", text: "The app checks every few seconds. A design must be open in Fusion; the address is in Settings." }),
      h("div", { class: "row" },
        h("button", { type: "button", class: "primary", onclick: () => { diagnosticsModal(); runFusionCheck(); }, text: "Check Fusion" }),
        h("button", { type: "button", class: "ghost", onclick: hideModal, text: "Close" })),
    ]);
  }

  // ---- diagnostics: Check Fusion, recent calls, export -------------------
  const CHECK_BADGE = { pass: ["pass", "ok"], warn: ["look", "warn"], fail: ["FAIL", "bad"], skip: ["not run", ""] };

  async function runFusionCheck() {
    const out = await act("check_fusion");
    if (out) toast("Checking Fusion (read-only calls)…");
  }

  async function loadDiagCalls() {
    try { diagCalls = await api("diagnostics"); } catch (_) { /* shown as empty */ }
    diagSignature = "";
    renderDiagnostics();
  }

  async function exportDiagnostics() {
    try {
      const out = await api("diagnostics_export", {});
      exportNote = "Written: " + out.path + ". Send this file back.";
      toast("Diagnostics written.");
    } catch (failure) { exportNote = "Not written: " + failure.message; }
    diagSignature = "";
    await poll(false);
    renderDiagnostics();
  }

  function checkItems(check) {
    return (check.items || []).map((item) => {
      const [label, tone] = CHECK_BADGE[item.status] || [item.status, ""];
      return h("div", { class: "check-item" },
        h("span", { class: "badge " + tone, text: label }),
        h("span", { class: "what", text: item.label + (item.ms !== null && item.ms !== undefined ? " · " + Math.round(item.ms) + " ms" : "") }),
        h("span", { class: "detail", text: item.detail || "" }),
        (item.images || []).map((image) => h("img", { class: "shot", alt: "screenshot returned by Fusion",
          src: "/api/image/" + image.id + "?k=" + encodeURIComponent(TOKEN) })),
        item.raw ? h("pre", { class: "raw", text: item.raw }) : null);
    });
  }

  function renderDiagnostics() {
    if (!diagOpen || !S) return;
    const check = S.fusion_check;
    const signature = JSON.stringify([check, S.session.busy, S.diagnostics, diagCalls && diagCalls.total, exportNote, S.fusion.state]);
    if (signature === diagSignature) return;
    diagSignature = signature;
    const busy = S.session.busy;
    const overall = check && !check.running ? (CHECK_BADGE[check.overall] || [check.overall, ""]) : null;
    const calls = (diagCalls && diagCalls.calls) || [];
    const content = [
      h("h1", { text: "Diagnostics" }),
      h("p", { class: "hint", text: "This app has not been verified against a real Fusion yet. Run the check once Fusion is open with a design, then export and send the file back, whatever the result." }),
      S.app.custom_model ? h("p", { class: "hint", text: "Model source: " + sourceText() + "." }) : null,
      h("h3", { text: "1. Check Fusion" }),
      h("p", { class: "hint", text: "Read-only: nothing is changed in your design and the view is not moved." }),
      h("div", { class: "row" },
        h("button", { type: "button", class: "primary", "data-act": "check", disabled: busy || !S.backend.ok, onclick: runFusionCheck, text: check ? "Check again" : "Check Fusion" }),
        overall ? h("span", { class: "badge " + overall[1], text: "overall: " + overall[0] }) : null,
        check && check.running ? h("span", { class: "muted", text: "running…" }) : null,
        S.fusion.state !== "connected" ? h("span", { class: "muted", text: "Fusion is not connected at " + S.fusion.url }) : null),
      check ? h("div", {}, checkItems(check)) : h("p", { class: "hint", text: "Not run yet in this launch." }),
      h("h3", { text: "2. Send it back" }),
      h("p", { class: "hint", text: "The export is one zip: the raw requests and answers below (scripts included, images replaced by their size), the check result, app and engine versions and the settings. The server token is left out. It does contain your feature texts and file names." }),
      exportNote || S.diagnostics.last_export ? h("div", { class: "note ok", text: exportNote || ("Last export: " + S.diagnostics.last_export) }) : null,
      h("div", { class: "row" },
        h("button", { type: "button", class: "primary", "data-act": "export", onclick: exportDiagnostics, text: "Export diagnostics" }),
        h("button", { type: "button", "data-act": "folder", onclick: () => act("open_folder", { which: "diagnostics" }), text: "Open folder" })),
      h("h3", { text: "Last requests to Fusion (" + (S.diagnostics.calls) + " of the last " + S.diagnostics.capacity + " kept)" }),
      calls.length ? h("div", { class: "scroll-x" }, h("table", { class: "calls-table" },
        h("tr", {}, ["time", "request", "kind", "http", "ms", "error"].map((text) => h("th", { text }))),
        calls.slice(-15).reverse().map((call) => h("tr", { class: call.error ? "bad" : "" },
          h("td", { text: String(call.time).slice(11, 23) }), h("td", { text: call.tool || call.method }),
          h("td", { text: call.kind || "" }), h("td", { text: call.status === null || call.status === undefined ? "-" : String(call.status) }),
          h("td", { text: call.ms === null || call.ms === undefined ? "" : String(Math.round(call.ms)) }),
          h("td", { text: (call.error || "") + (call.repeats ? " (×" + (call.repeats + 1) + ")" : "") })))))
        : h("p", { class: "hint", text: "Nothing sent yet." }),
      h("div", { class: "row" },
        h("button", { type: "button", class: "ghost", "data-act": "refresh", onclick: loadDiagCalls, text: "Refresh list" }),
        h("button", { type: "button", class: "ghost", "data-act": "close", onclick: hideModal, text: "Close" })),
    ];
    const box = $("modal").firstElementChild;
    const top = box.scrollTop, outer = $("modal").scrollTop;
    const focused = box.contains(document.activeElement) ? document.activeElement.getAttribute("data-act") : null;
    const fresh = $("modal").hidden;
    fillModal(content, true);
    box.scrollTop = top;
    $("modal").scrollTop = outer;
    const again = focused ? box.querySelector('[data-act="' + focused + '"]') : fresh ? box.querySelector('[data-act="check"]') : null;
    if (again) again.focus();
  }

  function diagnosticsModal() {
    if (!S) return;
    if (!$("modal").hidden && !diagOpen) hideModal();     // another window was open: start clean
    diagOpen = true;
    diagSignature = "";
    renderDiagnostics();
    loadDiagCalls();
  }

  function settingsModal() {
    if (!S) return;
    const st = S.settings;
    const field = (value, extra) => h("input", Object.assign({ type: "text", value: value || "" }, extra || {}));
    const select = (value, options) => {
      const node = h("select", {}, options.map(([v, label]) => h("option", { value: v, text: label })));
      node.value = value;
      return node;
    };
    const f = {
      fusion_url: field(st.fusion_url),
      mode: select(st.model.mode === "url" ? "url" : "spawn", [["spawn", "Start the model here"],
        ["url", "Use a server that is already running"]]),
      weights: field(st.model.weights, { placeholder: "auto (download the published model), base, or a .cact path" }),
      url: field(st.model.url, { placeholder: "http://127.0.0.1:8765" }),
      token: h("input", { type: "password", autocomplete: "off", placeholder: st.model.token_set ? "(saved; type to replace)" : "STEPSERVER_TOKEN, if the server has one" }),
      toolset: select(st.toolset, [["auto", "Router when the project has one, else Needle's retrieval"],
        ["router", "Project router (up to 5 tool names per step)"], ["catalogue", "Whole catalogue (Needle retrieves 5; slower)"]]),
      confirm_over_calls: h("input", { type: "number", min: "0", max: "4", value: String(st.confirm_over_calls) }),
      save_history: h("input", { type: "checkbox" }),
      export_dir: field(st.export_dir, { placeholder: S.app.export_dir }),
      log_dir: field(st.log_dir, { placeholder: "history folder in the app's data folder" }),
      window: select(st.window, [["auto", "Own window when pywebview is installed, else the browser"],
        ["native", "Own window (pywebview)"], ["browser", "Browser"], ["none", "None (serve only)"]]),
      capture_width: h("input", { type: "number", min: "32", max: "4096", value: String(st.capture_width), "aria-label": "Picture width in pixels" }),
      capture_height: h("input", { type: "number", min: "32", max: "4096", value: String(st.capture_height), "aria-label": "Picture height in pixels" }),
      capture_transparent: h("input", { type: "checkbox" }),
      source: select(S.model.source, [["official", "Official model (recommended)"], ["custom", "Custom Hugging Face model"]]),
    };
    f.source.addEventListener("change", () => sourceModal(f.source.value));     // its own window: it asks before anything changes
    f.save_history.checked = !!st.save_history;
    f.capture_transparent.checked = !!st.capture_transparent;
    // ["§", title] starts a section; [label, control, model modes it belongs to, note]
    const rows = [
      ["§", "Fusion and the model"],
      ["Fusion address (MCP server)", f.fusion_url],
      ["Model", f.mode],
      ["Weights", f.weights, "spawn"],
      S.app.custom_model ? ["Model source", f.source, "spawn"] : null,
      ["Server URL", f.url, "url"],
      ["Server token", f.token, "url"],
      ["§", "Running steps"],
      ["Tools per step", f.toolset],
      ["Ask before running more than", h("div", { class: "inline" }, f.confirm_over_calls, h("span", { class: "muted", text: "calls in one step" }))],
      ["Session history", h("label", { class: "check" }, f.save_history, "Keep a local history of the steps you accept (a file in the app's data folder)")],
      ["§", "Files and pictures"],
      ["Export folder", f.export_dir],
      ["Picture size (capture_view)", h("div", { class: "inline" }, f.capture_width, h("span", { class: "muted", text: "×" }), f.capture_height,
        h("span", { class: "muted", text: "pixels (32 to 4096)" }))],
      ["Picture background", h("label", { class: "check" }, f.capture_transparent, "Transparent (off: opaque, readable when shown here)")],
      ["§", "Used from the next launch"],
      ["Log folder", f.log_dir, "", "next launch"],
      ["Window", f.window, "", "next launch"],
    ];
    const grid = h("div", { class: "form" });
    const dependent = [];
    let number = 0;
    for (const [label, control, modes, note] of rows.filter(Boolean)) {
      if (label === "§") { add(grid, h("h3", { text: control })); continue; }
      const target = control.matches("input, select") ? control : control.querySelector("input, select");
      number += 1;
      if (target && !target.id) target.id = "set-" + number;
      const lab = h("label", { for: target ? target.id : null, text: label + (note ? " (" + note + ")" : "") });
      add(grid, lab, control);
      if (modes) dependent.push([modes.split(" "), lab, control]);
    }
    const sync = () => dependent.forEach(([modes, lab, control]) => {
      const show = modes.includes(f.mode.value);
      lab.hidden = !show; control.hidden = !show;
    });
    f.mode.addEventListener("change", sync);
    sync();
    const save = async () => {
      const model = { mode: f.mode.value, weights: f.weights.value.trim() || "auto", url: f.url.value.trim() };
      if (f.token.value) model.token = f.token.value;
      const changes = { fusion_url: f.fusion_url.value.trim() || st.fusion_url, model,
        toolset: f.toolset.value, confirm_over_calls: Number(f.confirm_over_calls.value) || 0,
        save_history: f.save_history.checked, export_dir: f.export_dir.value.trim(),
        log_dir: f.log_dir.value.trim(), window: f.window.value,
        capture_width: Math.min(4096, Math.max(32, Number(f.capture_width.value) || 1280)),
        capture_height: Math.min(4096, Math.max(32, Number(f.capture_height.value) || 720)),
        capture_transparent: f.capture_transparent.checked };
      const out = await act("settings", changes);
      if (out) { hideModal(); toast("Settings saved."); }
    };
    grid.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target.tagName === "INPUT" && event.target.type !== "checkbox") { event.preventDefault(); save(); }
    });
    showModal([
      h("h1", { text: "Settings" }),
      h("p", { class: "hint", text: "Stored in " + S.app.config + ". Nothing is written to the project." }),
      grid,
      h("div", { class: "row" },
        h("button", { type: "button", class: "ghost", onclick: hideModal, text: "Cancel" }),
        h("button", { type: "button", class: "primary", onclick: save, text: "Save" })),
    ]);
  }

  // ---- "What can I say?" ------------------------------------------------
  function useExample(text) {
    const box = $("goal");
    const current = box.value.replace(/\s+$/, "");
    box.value = current && current !== S.session.goal ? current + "\n" + text : text;
    box.dispatchEvent(new Event("input"));
    if (!$("modal").hidden) hideModal();
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
    toast("Added to your request. Change the numbers to yours, then press Enter.");
  }

  function exampleChip(text, title) {
    return h("button", { type: "button", class: "example" + (/→|->|\bthen\b/.test(text) ? " steps" : ""),
      title: title || "Put this in the request box", onclick: () => useExample(text), text });
  }

  async function loadHelp() {
    try { HELP = await api("help"); } catch (failure) { if (!HELP) throw failure; }
    return HELP;
  }

  async function helpModal() {
    try { await loadHelp(); } catch (failure) { toast(failure.message); return; }
    const groups = HELP.groups.map((group) => h("section", { class: "help-group" },
      h("h3", { text: group.title }),
      group.note ? h("p", { class: "hint", text: group.note }) : null,
      group.tools.map((tool) => h("div", { class: "help-tool" },
        h("div", { class: "help-what" }, h("strong", { text: capital(tool.label) }),
          h("small", { text: tool.description + (tool.needs ? " Needs " + tool.needs + "." : "") })),
        h("div", { class: "help-say" }, tool.examples.length
          ? tool.examples.map((text) => exampleChip(text))
          : h("span", { class: "muted", text: "no example yet (" + tool.name + ")" }))))));
    const nothing = h("p", { class: "hint", hidden: true, text: "Nothing matches. Try another word, such as “hole” or “round”." });
    const search = h("input", { type: "search", class: "help-search", placeholder: "Search, for example: hole, round, export", "aria-label": "Search the examples" });
    const whole = HELP.starters.length ? h("section", { class: "help-group" }, h("h3", { text: "Whole parts to start from" }),
      HELP.starters.map((item) => h("div", { class: "help-tool" },
        h("div", { class: "help-what" }, h("small", { text: item.note })),
        h("div", { class: "help-say" }, exampleChip(item.text, "Put these steps in the request box"))))) : null;
    search.addEventListener("input", () => {
      const needle = search.value.trim().toLowerCase();
      let shown = 0;
      for (const section of [whole, ...groups].filter(Boolean)) {
        let any = 0;
        for (const row of section.querySelectorAll(".help-tool")) {
          row.hidden = !!needle && !row.textContent.toLowerCase().includes(needle);
          if (!row.hidden) any += 1;
        }
        section.hidden = !any;
        shown += any;
      }
      nothing.hidden = shown > 0;
    });
    showModal([
      h("h1", { text: "What can I say?" }),
      h("p", { class: "hint", text: "Write one thing per step, the way you would say it to a colleague. Every size must be written as a number: the model copies values from your text and never guesses or calculates them (“half as thick” does not work, “5 mm thick” does). Click an example to put it in the request box, then change the numbers." }),
      search, nothing, whole,
      ...groups,
      ...(HELP.problems || []).map((text) => h("div", { class: "note warn", text })),
      h("div", { class: "row" }, h("button", { type: "button", class: "ghost", onclick: hideModal, text: "Close" })),
    ].filter(Boolean), true);
  }

  // the starting screen: a few short requests, then whole parts
  function renderStarters() {
    const signature = JSON.stringify([QUICK, HELP && HELP.tools, HELP && HELP.starters]);
    if (signature === startersSignature) return;
    startersSignature = signature;
    const known = new Set();
    const first = [];
    for (const group of (HELP && HELP.groups) || []) {
      for (const tool of group.tools) { known.add(tool.name); if (tool.examples.length) first.push(tool.examples[0]); }
    }
    let quick = QUICK.filter((item) => !item.tool || !HELP || known.has(item.tool)).map((item) => item.text);
    if (!quick.length) quick = first.slice(0, 5);
    $("starters").replaceChildren(...quick.map((text) => exampleChip(text)));
    $("starter-parts").replaceChildren(...(((HELP && HELP.starters) || []).length ? [h("h2", { text: "Or build a whole part, step by step" }),
      HELP.starters.slice(0, 3).map((item) => h("div", { class: "starter-part" }, exampleChip(item.text, "Put these steps in the request box"),
        h("small", { text: item.note })))] : []).flat());
  }

  async function loadStarters() {
    try {
      const response = await fetch("/ui/examples.json");
      const listed = (await response.json()).quick;
      QUICK = (Array.isArray(listed) ? listed : []).map((item) => (typeof item === "string" ? { text: item } : item))
        .filter((item) => item && typeof item.text === "string" && item.text.trim());
    } catch (_) { /* the first examples of the help panel are used instead */ }
    try { await loadHelp(); } catch (_) { /* shown once the app answers */ }
    renderStarters();
  }

  async function scriptModal(step, call) {
    try {
      const out = await api("script?step=" + step + "&call=" + call);
      showModal([
        h("h1", { text: "Sent to Fusion: " + out.tool }),
        h("pre", { class: "script", text: out.script || JSON.stringify(out.arguments, null, 2) }),
        h("div", { class: "row" }, h("button", { type: "button", class: "ghost", onclick: hideModal, text: "Close" })),
      ], true);
    } catch (failure) { toast(failure.message); }
  }

  // ---- the request box and the side panel --------------------------------
  function goalChanged() { return $("goal").value.trim() !== "" && $("goal").value !== S.session.goal; }
  function currentStep() { return S.session.current === null ? null : S.session.steps[S.session.current]; }

  function renderSide() {
    const ses = S.session;
    const ready = isReady();
    const hasSteps = ses.steps.length > 0;
    const goalBox = $("goal");
    const changed = goalChanged();
    const current = currentStep();
    const waiting = current && ["proposed", "empty", "dialog", "choice"].includes(current.status);
    $("btn-stop").hidden = !ses.busy;
    $("btn-run-all").hidden = ses.busy;
    $("btn-run-step").hidden = ses.busy;
    const canRun = ready && (changed || (hasSteps && !ses.finished && !(waiting && (current.status !== "proposed" || current.confirm.length || current.blocked))));
    $("btn-run-all").disabled = !canRun;
    $("btn-run-step").disabled = !(ready && (changed || (hasSteps && !ses.finished && !waiting)));
    const under = !changed && hasSteps && ses.current > 0;
    $("btn-run-all").textContent = under ? "Run the rest" : "Run all";
    $("btn-run-step").textContent = under ? "Next step" : "Run step by step";
    goalBox.placeholder = hasSteps ? "What next? For example: fillet the top edges 2 mm" : "make a box 40 by 30 by 10";
    let note = "";
    if (!S.backend.ok) note = "The app is not ready (see “Getting ready” above).";
    else if (S.model.state !== "ready") note = S.model.state === "starting" ? "Waiting for the model…" : "The model is not ready (see “Getting ready” above).";
    else if (S.fusion.state !== "connected") note = "Waiting for Fusion (see “Getting ready” above).";
    else if (changed && hasSteps && !ses.finished) note = "This text differs from the steps in progress. Running it replaces the list below; what is already built stays in Fusion.";
    else if (ses.finished && !changed) note = "All done. Type the next thing to do.";
    else if (waiting && !changed) note = "Step " + (current.index + 1) + " is waiting for you below.";
    $("goal-note").textContent = note;

    const done = ses.steps.filter((step) => ["done", "negative", "skipped"].includes(step.status)).length;
    $("steps-bar").hidden = !hasSteps;
    $("steps-title").textContent = hasSteps ? "Steps · " + done + " of " + ses.steps.length + " done" : "Steps";

    $("state-chips").replaceChildren(...(ses.fusion_state ? stateChips(ses.fusion_state)
      : [h("span", { class: "muted", text: S.fusion.state === "connected" ? "not read yet" : "Fusion is not connected" })]));
    const unhealthy = (ses.fusion_unhealthy || []).map((item) => item.name || String(item)).join(", ");
    $("state-note").textContent = ses.fusion_state_error ? ses.fusion_state_error
      : unhealthy ? "Timeline items with errors or warnings: " + unhealthy : "";
    $("btn-refresh").disabled = ses.busy || S.fusion.state !== "connected";
    $("btn-undo-step").disabled = ses.busy || !ses.can_undo_step || S.fusion.state !== "connected";
    $("btn-undo-once").disabled = ses.busy || S.fusion.state !== "connected";

    const log = ses.log;
    $("log-info").replaceChildren(
      h("span", { class: "k", text: "file" }), h("span", { class: "v", text: log.file }),
      h("span", { class: "k", text: "entries" }), h("span", { class: "v", text: log.rows + " written" + (log.pending ? ", 1 waiting until you move on" : "") }));
    $("btn-check").disabled = ses.busy || (!log.rows && !log.pending);
    const check = S.check;
    $("check-result").textContent = !check ? (S.settings.save_history ? "Entries are written when you move on from a step, so the last step can still be undone." : "The session history is off (Settings). Nothing is written.")
      : check.errors ? check.errors + " error(s) in " + check.rows + " row(s): " + check.issues.filter((i) => i.level === "error").slice(0, 3).map((i) => "line " + i.line + " " + i.code).join("; ")
        : check.rows + " entries in this session's history file.";
  }

  // ---- steps -----------------------------------------------------------
  const BADGES = {
    pending: ["waiting", ""], reading: ["reading the design", "live"], asking: ["thinking", "live"],
    proposed: ["ready to run", "warn"], empty: ["needs you", "warn"], running: ["running", "live"],
    dialog: ["dialog open in Fusion", "warn"], choice: ["pick a document", "warn"], failed: ["failed", "bad"], error: ["could not start", "bad"],
    done: ["done", "ok"], negative: ["no call", "ok"], skipped: ["skipped", ""],
  };

  // what to tell the user when a step did not run: understood, what is missing, requests that work
  function helpBlock(step) {
    const help = step.help;
    if (!help) return null;
    const pick = (text) => { rewriting = { index: step.index, text }; lastSignature = ""; render(); focusRewrite(step.index); };
    return [h("div", { class: "help-line", text: help.understood }),
      help.examples.length ? h("div", { class: "help-line" }, "Requests that work here (click one to use it):",
        h("div", { class: "help-say" }, help.examples.map((item) => item.text
          ? h("button", { type: "button", class: "example", onclick: () => pick(item.text), text: item.text })
          : h("span", { class: "chip", title: item.tool, text: item.description })))) : null];
  }

  function categoryPicker(step, button, lead) {
    const category = h("select", { "aria-label": "Why nothing should be called" }, [["nocall_missing", "a required value is missing"], ["nocall_offtopic", "not something the tools can do"],
      ["nocall_negated", "the request was negated"], ["nocall_bounds", "a value is out of bounds"]]
      .map(([value, label]) => h("option", { value, text: label + " (" + value + ")" })));
    return [h("h3", { text: lead }),
      h("div", { class: "inline" }, category, button("Agree", "", () => act("accept_empty", { index: step.index, category: category.value })))];
  }

  function isOpen(step) {
    if (open.has(step.index)) return true;
    if (closed.has(step.index)) return false;
    return step.index === S.session.current || ["failed", "error"].includes(step.status)
      || (step.results || []).some((result) => result && (result.images || []).length);
  }

  function renderCall(step, call, position, kind) {
    const result = kind === "run" ? step.results[position] : null;
    const cls = "call" + (kind === "withheld" ? " withheld" : kind === "dim" ? " dim" : result ? (result.ok ? " ok" : " bad") : "");
    const info = (toolByName(call.name) || {}).info || {};
    const stateText = kind === "withheld" ? "held back, never run"
      : result ? (result.ok ? "✓ done · " + Math.round(result.ms) + " ms" : "failed")
        : (step.status === "running" && position === step.next_call ? "running…" : "");
    const about = toolAbout(call.name);
    const node = h("div", { class: cls },
      h("div", { class: "call-head" },
        h("div", { class: "call-title" },
          h("span", { class: "call-name", title: call.name, text: toolTitle(call.name) }),
          info.read_only ? h("span", { class: "badge", title: "This only reads from the design; it changes nothing", text: "only reads" }) : null,
          info.confirm ? h("span", { class: "badge warn", title: "The app asks you before it runs this", text: "asks first" }) : null),
        h("span", { class: "call-state", text: stateText }),
        about ? h("div", { class: "call-desc", text: about }) : null,
        argsList(call.arguments)));
    if (result) {
      const out = h("div", { class: "call-out" });
      if (result.error) {
        const short = lastLine(result.error);
        add(out, h("div", { class: "mono", text: short }),
          short !== String(result.error).trim() ? details("err-" + step.index + "-" + position, "Show the full error", h("pre", { text: result.error })) : null);
      } else if (result.text) add(out, h("pre", { text: result.text }));
      for (const image of result.images || []) {
        add(out, h("img", { class: "shot", alt: "image returned by " + call.name, loading: "lazy", title: "Click to enlarge",
          src: "/api/image/" + image.id + "?k=" + encodeURIComponent(TOKEN),
          onclick: (event) => event.target.classList.toggle("big") }));
      }
      add(out, h("div", { class: "row" }, h("button", { type: "button", class: "link",
        onclick: () => scriptModal(step.index, position), text: "Show what was sent (" + result.via + ")" })));
      node.append(out);
    }
    return node;
  }

  function startEdit(step) {
    const source = step.calls.length ? step.calls : [];
    editing = { index: step.index, calls: source.map((call) => ({ name: call.name,
      values: Object.fromEntries(Object.entries(call.arguments || {}).map(([k, v]) => [k, String(v)])) })) };
    if (!editing.calls.length) editing.calls.push({ name: allowedTools(step)[0] || "", values: {} });
    lastSignature = "";
    render();
    const first = $("steps").querySelector('.step[data-index="' + step.index + '"] .edit-grid input, .step[data-index="' + step.index + '"] .edit-call select');
    if (first) first.focus();
  }

  function allowedTools(step) { return step.tools || TOOLS.map((tool) => tool.name); }

  function collectEdit(step) {
    const calls = [];
    for (const draft of editing.calls) {
      const tool = toolByName(draft.name);
      if (!tool) throw new Error("Unknown tool " + draft.name);
      const props = (tool.parameters || {}).properties || {};
      const args = {};
      for (const [key, spec] of Object.entries(props)) {
        const text = (draft.values[key] === undefined ? "" : String(draft.values[key])).trim();
        if (text === "") continue;
        if (spec.enum) { args[key] = text; continue; }
        if (spec.type === "integer" || spec.type === "number") {
          const number = Number(text.replace(",", "."));
          if (!Number.isFinite(number)) throw new Error(toolTitle(draft.name) + ", " + words(key) + ": " + text + " is not a number");
          if (spec.type === "integer" && !Number.isInteger(number)) throw new Error(toolTitle(draft.name) + ", " + words(key) + " must be a whole number");
          args[key] = number;
        } else if (spec.type === "boolean") args[key] = text === "true";
        else args[key] = text;
      }
      calls.push({ name: draft.name, arguments: args });
    }
    return calls;
  }

  function notInText(step) {
    const missing = [];
    for (const draft of editing.calls) {
      const props = ((toolByName(draft.name) || {}).parameters || {}).properties || {};
      for (const [key, raw] of Object.entries(draft.values)) {
        const text = String(raw).trim();
        if (!text || !props[key] || props[key].enum) continue;
        if (!step.text.includes(text)) missing.push(words(key) + " = " + text);
      }
    }
    return missing;
  }

  function renderEditor(step) {
    const names = allowedTools(step);
    const wrap = h("div", {});
    add(wrap, h("h3", { text: "Edit what will run" }));
    const warning = h("div", { class: "note warn", hidden: true });
    const refreshWarning = () => {
      const missing = notInText(step);
      warning.hidden = !missing.length;
      warning.replaceChildren(h("strong", { text: "Not written in your request: " + missing.join(", ") }),
        "The model copies values from the text. If the text is wrong, rewrite the feature instead.");
    };
    editing.calls.forEach((draft, position) => {
      const tool = toolByName(draft.name);
      const pick = h("select", { "aria-label": "Command " + (position + 1) }, names.map((name) => h("option", { value: name, text: toolTitle(name) })));
      pick.value = draft.name;
      pick.addEventListener("change", () => { draft.name = pick.value; draft.values = {}; lastSignature = ""; render(); });
      const box = h("div", { class: "edit-call" },
        h("div", { class: "inline" }, h("strong", { text: (position + 1) + "." }), pick,
          h("button", { type: "button", class: "small ghost", text: "Remove",
            onclick: () => { editing.calls.splice(position, 1); lastSignature = ""; render(); } })));
      if (tool) {
        add(box, h("div", { class: "hint", text: tool.description || "" }));
        const props = (tool.parameters || {}).properties || {};
        const required = (tool.parameters || {}).required || [];
        const grid = h("div", { class: "edit-grid" });
        for (const [key, spec] of Object.entries(props)) {
          let control;
          const id = "edit-" + step.index + "-" + position + "-" + key;
          if (spec.enum) {
            control = h("select", { id }, [h("option", { value: "", text: required.includes(key) ? "choose…" : "(not set)" })]
              .concat(spec.enum.map((value) => h("option", { value: String(value), text: String(value) }))));
            control.value = draft.values[key] === undefined ? "" : draft.values[key];
          } else {
            control = h("input", { id, type: "text", inputmode: "decimal", value: draft.values[key] === undefined ? "" : draft.values[key],
              placeholder: spec.default !== undefined ? "default " + spec.default : (required.includes(key) ? "required" : "not set") });
          }
          control.addEventListener("input", () => { draft.values[key] = control.value; refreshWarning(); });
          control.addEventListener("change", () => { draft.values[key] = control.value; refreshWarning(); });
          add(grid, h("label", { for: id, class: required.includes(key) ? "req" : "", title: key, text: words(key) }), control,
            h("small", { text: spec.description || "" }));
        }
        if (!Object.keys(props).length) add(grid, h("small", { text: "This command takes no values." }));
        add(box, grid);
      }
      add(wrap, box);
    });
    add(wrap, warning);
    refreshWarning();
    add(wrap, h("div", { class: "row actions" },
      h("button", { type: "button", class: "primary", disabled: !editing.calls.length, text: "Run these",
        onclick: async () => {
          let calls;
          try { calls = collectEdit(step); } catch (failure) { toast(failure.message); return; }
          const out = await act("approve", { index: step.index, calls });
          if (out) { editing = null; lastSignature = ""; render(); }
        } }),
      h("button", { type: "button", class: "small", disabled: editing.calls.length >= 4, text: "Add another command",
        onclick: () => { editing.calls.push({ name: names[0] || "", values: {} }); lastSignature = ""; render(); } }),
      h("button", { type: "button", class: "ghost", text: "Cancel", onclick: () => { editing = null; lastSignature = ""; render(); } })));
    return wrap;
  }

  function focusRewrite(index) {
    const input = $("steps").querySelector('.step[data-index="' + index + '"] .rewrite input');
    if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  }

  function rewriteBox(step, label, cancel) {
    const draft = rewriting && rewriting.index === step.index ? rewriting : null;
    const input = h("input", { type: "text", value: draft ? draft.text : step.text, spellcheck: "false", "aria-label": "The request for step " + (step.index + 1) });
    input.addEventListener("input", () => { rewriting = { index: step.index, text: input.value }; });
    const send = async () => {
      const out = await act("rewrite", { index: step.index, text: input.value });
      if (out) { rewriting = null; lastSignature = ""; render(); }
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing) send();
      if (event.key === "Escape" && cancel) { event.stopPropagation(); rewriting = null; lastSignature = ""; render(); }
    });
    return h("div", { class: "rewrite" }, input, h("button", { type: "button", class: "primary", onclick: send, text: label || "Ask again" }),
      cancel ? h("button", { type: "button", class: "ghost", onclick: () => { rewriting = null; lastSignature = ""; render(); }, text: "Cancel" }) : null);
  }

  function renderStepBody(step) {
    const ses = S.session;
    const idle = !ses.busy;
    const body = h("div", { class: "step-body" });
    const isCurrent = step.index === ses.current;
    const inEdit = !!(editing && editing.index === step.index);
    const held = step.status === "proposed" && step.blocked;      // the call guard did not let the answer through
    const actions = h("div", { class: "row actions" });
    // key: the letter that presses this button while the step is the waiting one (see the keydown handler)
    const button = (text, cls, handler, disabled, key) => {
      const node = h("button", { type: "button", class: cls || "", disabled: !!disabled || !idle, onclick: handler,
        "data-key": key || null }, text, key && isCurrent ? h("kbd", { "aria-hidden": "true", text: key.charAt(0).toUpperCase() }) : null);
      return node;
    };
    const rewriteButton = () => button(isCurrent ? "Rewrite" : "Change the text", "", () => {
      rewriting = { index: step.index, text: step.text }; lastSignature = ""; render(); focusRewrite(step.index);
    }, false, "rewrite");
    const technical = [];         // goes under "Details"
    const extra = [];             // goes under "More options"

    if (["reading", "asking", "running"].includes(step.status)) {
      add(body, h("p", { class: "lead" }, h("i", { class: "spin" }), " ",
        { reading: "Reading the design in Fusion…", asking: "Working out what to do…", running: "Running in Fusion…" }[step.status]));
    } else if (step.status === "pending") {
      add(body, h("p", { class: "lead muted", text: isCurrent ? "Not started yet. Press “Next step” above, or change the text first."
        : "Waits for the steps before it." }));
    }

    if (held && !inEdit) {
      const guard = step.guard || { issues: [], overridable: false };
      add(body, h("div", { class: "note bad" },
        h("strong", { text: "Not run: nothing was sent to Fusion" }),
        h("ul", {}, guard.issues.map((issue) => h("li", { text: issue.message }))),
        guard.overridable
          ? "The model is meant to copy every value from your text. If what it proposed below is what you meant after all, you can run it anyway."
          : "This cannot be run as it is. Change the request, or edit the values."));
    }

    if (inEdit) {
      add(body, renderEditor(step));
    } else if (step.calls.length) {
      const lead = step.corrected ? "What runs (corrected by you)"
        : held ? "What the model proposed (not run)"
          : step.status === "proposed" ? "This is what will happen" + (step.calls.length > 1 ? ", in this order" : "")
            : step.status === "done" ? "What was done" : "What runs";
      add(body, h("h3", { text: lead }),
        step.calls.map((call, position) => renderCall(step, call, position, held ? "dim" : "run")));
      if (step.corrected && step.model_calls.length) {
        technical.push(h("h3", { text: "The model answered" }),
          h("div", { class: "reasoning", text: step.model_calls.map((c) => c.name + " " + JSON.stringify(c.arguments)).join(" → ") }));
      }
    }

    if (step.status === "proposed" && !held && !inEdit) {
      if (step.confirm.length) {
        add(body, h("div", { class: "note warn" }, h("strong", { text: "Please check before this runs" }),
          h("ul", {}, step.confirm.map((reason) => h("li", { text: reason })))));
      }
      const approve = button(step.confirm.length ? "Confirm and run" : "Approve and run", "primary",
        () => act("approve", { index: step.index }), step.blocked || !isCurrent, "approve");
      approve.addEventListener("keydown", (event) => {      // a held-down Enter from the request box must not approve
        if ((event.key === "Enter" || event.key === " ") && (event.repeat || performance.now() < armAt)) event.preventDefault();
      });
      add(actions, approve, button("Edit values", "", () => startEdit(step), !isCurrent, "edit"));
    }
    if (held && !inEdit) {
      const guard = step.guard || { issues: [], overridable: false };
      add(body, h("div", { class: "fix" }, h("strong", { text: "Change the request and ask again" }), helpBlock(step),
        rewriteBox(step, isCurrent ? "Ask again" : "Change text")));
      add(actions, button("Edit values", "", () => startEdit(step), !isCurrent, "edit"),
        guard.overridable ? button("Run anyway", "danger", () => act("approve", { index: step.index, override: true }), !isCurrent) : null);
      if (isCurrent) extra.push(categoryPicker(step, button, "Or agree: the model should have called nothing"));
    }
    if (step.status === "empty") {
      add(body, h("div", { class: "note warn" },
        h("strong", { text: step.engine_error ? "The model gave up on this request" : "I could not turn this into a Fusion command" }),
        step.help ? h("ul", {}, step.help.problems.map((text) => h("li", { text })))
          : (step.engine_error ? step.engine_error + ". " : "") + "No tool applies, or a required value is missing from the text. ",
        "Nothing was run. What is built so far stays."));
      if (step.suppressed.length) {
        add(body, h("h3", { text: "Held back by the model (did you mean this?)" }),
          step.suppressed.map((call) => renderCall(step, call, 0, "withheld")));
      }
      add(body, h("div", { class: "fix" }, h("strong", { text: "Change the request and ask again" }), helpBlock(step), rewriteBox(step)));
      extra.push(categoryPicker(step, button, "Agree that nothing should be called"));
      if (!inEdit) extra.push(h("h3", { text: "Or choose the command yourself" }), h("div", { class: "row" }, button("Add calls by hand", "", () => startEdit(step), false, "edit")));
    }
    if (step.status === "dialog" && step.dialog_unknown) {
      add(body, h("div", { class: "note bad" },
        h("strong", { text: "The app could not tell whether Fusion has a command dialog open" }),
        "Before a modifying call the app asks Fusion for its active command, because a script fails while a dialog is open. "
        + "Fusion answered in a form this app does not know (" + step.dialog + "), so nothing was sent. "
        + (step.next_call ? step.next_call + " call(s) of this step are already in. " : "")
        + "The answer below is saved in " + S.diagnostics.folder + "; please export the diagnostics and send them back so the form can be added.",
        h("pre", { class: "raw", text: step.dialog_raw || "(no answer body)" }),
        "If you can see that no dialog is open in Fusion, you can send this step's calls anyway. That choice covers this step only; if a dialog is open after all, the call fails and the step is rolled back as usual."));
      add(actions, button("Check again", "primary", () => act("resume", { index: step.index })),
        button("Proceed anyway (this step)", "danger", () => act("proceed", { index: step.index })),
        button("Diagnostics…", "", diagnosticsModal));
    } else if (step.status === "dialog") {
      add(body, h("div", { class: "note warn" }, h("strong", { text: "Fusion has a command dialog open: " + step.dialog }),
        "Scripts fail while a dialog is open. Finish or cancel it in Fusion, then continue. "
        + (step.next_call ? step.next_call + " call(s) of this step are already in." : "")));
      add(actions, button("Continue", "primary", () => act("resume", { index: step.index })));
    }
    if (step.status === "choice") {
      add(body, h("div", { class: "note warn" }, h("strong", { text: "Several documents match. Which one should open?" }),
        h("div", { class: "row" }, step.choice.map((entry) => button(entry.name, "", () => act("choose", { index: step.index, id: entry.id }))))));
    }
    if (step.status === "failed" || step.status === "error") {
      const failedCall = step.calls[step.error_call];
      const shownInCall = step.results[step.error_call] && !step.results[step.error_call].ok;
      add(body, h("div", { class: "note bad" },
        h("strong", { text: step.status === "failed" ? "Fusion could not do this" + (failedCall ? ": " + toolTitle(failedCall.name)
          + (step.calls.length > 1 ? " (command " + ((step.error_call || 0) + 1) + " of " + step.calls.length + ")" : "") : "") : "This step could not start" }),
        shownInCall ? null : h("div", { class: "oneline", text: lastLine(step.error) }),
        step.status === "failed" ? (step.undone ? step.undone + " earlier call(s) of this step were undone in Fusion." : "Nothing of this step is left in the timeline.") + " The step is not logged. " : "",
        "Try again, change the request, or skip this step.",
        !shownInCall && lastLine(step.error) !== String(step.error || "").trim()
          ? details("step-err-" + step.index, "Show the full error", h("pre", { class: "raw", text: step.error })) : null));
      add(actions, button("Try again", "primary", () => act("retry", { index: step.index }), !isCurrent, "approve"));
    }
    if (["pending", "proposed", "failed", "error"].includes(step.status) && !inEdit && !held) {
      if (rewriting && rewriting.index === step.index) {
        add(body, h("div", { class: "fix" }, h("strong", { text: isCurrent ? "Change the request and ask again" : "Change the text of this step" }),
          rewriteBox(step, isCurrent ? "Ask again" : "Change text", true)));
      } else add(actions, rewriteButton());
    }
    if (["pending", "proposed", "empty", "dialog", "choice", "failed", "error"].includes(step.status)) {
      add(actions, button(step.status === "dialog" && step.next_call ? "Skip (undo its calls)" : "Skip", "ghost", () => act("skip", { index: step.index }), false, "skip"));
    }
    if (actions.childNodes.length) add(body, actions);
    if (extra.length) add(body, details("more-" + step.index, "More options", extra));

    for (const note of step.notes || []) add(body, h("div", { class: "note", text: note }));
    if (step.row_status || step.row_note) {
      const label = { pending: "History entry waits until you move on (undo still withdraws it)", logged: "Saved to the history" + (step.row_line ? " (line " + step.row_line + ")" : ""),
        not_logged: "Not saved to the history", withdrawn: "History entry withdrawn" }[step.row_status] || "";
      add(body, h("div", { class: "rowline" },
        h("b", { class: step.row_status === "logged" ? "ok" : step.row_status === "not_logged" ? "bad" : "",
          text: label + (step.row_id ? " · " + step.row_id : "") + (step.corrected ? " · corrected" : "") }),
        step.row_note ? " " + step.row_note : ""));
    }

    // the technical side, on demand
    if (step.state) technical.unshift(h("h3", { text: "State sent to the model" }), h("div", { class: "chips" }, stateChips(step.state)));
    if (step.answered || step.tools) {
      const called = new Set(step.model_calls.map((call) => call.name));
      technical.push(h("h3", { text: step.tools ? "Tools offered" : "Tools" }),
        step.tools ? h("div", { class: "chips" }, step.tools.map((name) =>
          h("span", { class: "chip tool-chip" + (called.has(name) ? " gold" : ""), text: name, title: (toolByName(name) || {}).description || "" })))
          : h("div", { class: "hint", text: "Whole catalogue sent (" + S.model.tools + " tools); Needle retrieved its own five." }));
    }
    if (step.reasoning) technical.push(h("h3", { text: "Model reasoning" }), h("div", { class: "reasoning", text: step.reasoning }));
    if (step.suppressed.length && step.status !== "empty") {
      technical.push(h("h3", { text: "Withheld by the engine (did you mean?)" }),
        step.suppressed.map((call) => renderCall(step, call, 0, "withheld")));
    }
    if (step.calls.length) technical.push(h("h3", { text: "Calls as JSON" }), h("pre", { class: "raw", text: JSON.stringify(step.calls, null, 2) }));
    if (step.state_after) technical.push(h("h3", { text: "State after" }), h("div", { class: "chips" }, stateChips(step.state_after)));
    if (step.latency_ms !== null && step.latency_ms !== undefined) {
      technical.push(h("div", { class: "hint", text: "Model answered in " + Math.round(step.latency_ms) + " ms"
        + (step.agent_cache ? " · context " + step.agent_cache : "") + (step.retrieval ? " · retrieval on" : "") }));
    }
    if (technical.length) add(body, details("tech-" + step.index, "Details", technical));
    return body;
  }

  function renderSteps() {
    const ses = S.session;
    $("empty-state").hidden = ses.steps.length > 0;
    const kept = $("steps").contains(document.activeElement) ? document.activeElement : null;
    const memo = kept && kept.closest(".step") ? [kept.closest(".step").getAttribute("data-index"), kept.getAttribute("data-key")] : null;
    const nodes = ses.steps.map((step) => {
      const held = step.status === "proposed" && step.blocked;
      const [label, tone] = held ? ["not run", "bad"] : (BADGES[step.status] || [step.status, ""]);
      const opened = isOpen(step);
      const head = h("button", { type: "button", class: "step-head", "aria-expanded": String(opened), "data-key": "head",
        onclick: () => {
          if (opened) { open.delete(step.index); closed.add(step.index); } else { closed.delete(step.index); open.add(step.index); }
          lastSignature = ""; render();
        } },
        h("span", { class: "step-num", "aria-hidden": "true", text: ["done", "negative"].includes(step.status) ? "✓" : String(step.index + 1) }),
        h("span", { class: "step-text" }, step.text,
          step.calls.length && !opened ? h("small", { text: step.calls.map((call) => toolTitle(call.name)).join(" → ") }) : null),
        h("span", { class: "badge " + tone, text: label + (step.status === "done" && step.corrected ? " · corrected" : "") }));
      return h("article", { class: "step s-" + step.status + (held ? " held" : "") + (step.index === ses.current ? " current" : ""),
        "data-index": String(step.index), "aria-label": "Step " + (step.index + 1) },
        head, opened ? renderStepBody(step) : null);
    });
    $("steps").replaceChildren(...nodes);
    if (memo && memo[1]) {
      const again = $("steps").querySelector('.step[data-index="' + memo[0] + '"] [data-key="' + memo[1] + '"]:not(:disabled)');
      if (again) again.focus({ preventScroll: true });
    }
  }

  // when a step starts waiting for the user: bring it into view, and put the keyboard on Approve
  function attend() {
    const step = currentStep();
    const key = step ? [step.index, step.status, step.blocked, S.session.busy].join(":") : "";
    if (key === waitingKey) return;
    waitingKey = key;
    if (!step || S.session.busy || !$("modal").hidden) return;
    if (!["proposed", "empty", "failed", "error", "dialog", "choice"].includes(step.status)) return;
    const node = $("steps").querySelector(".step.current");
    if (!node) return;
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({ block: "nearest", behavior: calm ? "auto" : "smooth" });
    const active = document.activeElement;
    const free = !active || active === document.body || (active === $("goal") && !goalChanged());
    const approve = node.querySelector('[data-key="approve"]:not(:disabled)');
    if (free && approve && step.status === "proposed" && !step.blocked && !step.confirm.length) {
      armAt = performance.now() + 450;
      approve.focus({ preventScroll: true });
    }
  }

  // ---- render loop -----------------------------------------------------
  function render() {
    if (!S) return;
    renderBar();
    renderSetup();
    renderSide();
    renderStarters();
    const activity = $("activity");
    activity.hidden = !S.session.busy;
    activity.lastElementChild.textContent = S.session.activity || "Working";
    const signature = JSON.stringify([S.session.steps, S.session.current, S.session.busy, TOOLS.length, editing && editing.index]);
    const typing = document.activeElement && $("steps").contains(document.activeElement)
      && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
    if (signature !== lastSignature && !(typing && lastSignature !== "")) {
      if (editing && (!S.session.steps[editing.index] || !["proposed", "empty"].includes(S.session.steps[editing.index].status))) editing = null;
      if (rewriting && !S.session.steps[rewriting.index]) rewriting = null;
      lastSignature = signature;
      renderSteps();
      attend();
    }
  }

  function sizeGoal() {
    const box = $("goal");
    box.rows = Math.min(8, Math.max(2, box.value.split("\n").length));
  }

  async function poll(force) {
    if (stopped) return;
    try {
      S = await api("state");
      lostContact = 0;
      const goalBox = $("goal");
      if (firstState) {
        firstState = false;
        if (!goalBox.value && S.session.goal && !S.session.finished) { goalBox.value = S.session.goal; sizeGoal(); }   // page reloaded mid-session
      } else if (S.session.finished && !wasFinished && goalBox.value === S.session.goal) {
        goalBox.value = "";                                // done: the box is free for the next request
        $("split-preview").replaceChildren();
        sizeGoal();
      }
      wasFinished = S.session.finished;
      const key = S.model.state + ":" + S.model.tools + ":" + S.model.url;
      if (key !== toolsKey && S.model.state === "ready") {
        TOOLS = (await api("tools")).tools;
        toolsKey = key;
        lastSignature = "";
        loadHelp().then(renderStarters, () => {});         // the model's own catalogue may differ from the project file
      }
      if (force) lastSignature = "";
      render();
      renderDiagnostics();
    } catch (failure) {
      lostContact += 1;
      if (lostContact === 3 && !stopped) {
        $("banner").hidden = false;
        $("banner").textContent = "The app has stopped (" + failure.message + "). Start it again and close this page.";
      }
    }
  }

  // ---- wiring ----------------------------------------------------------
  async function startRun(auto) {
    const goalBox = $("goal");
    const ses = S.session;
    if (goalBox.value.trim() && goalBox.value !== ses.goal) {
      const set = await act("goal", { goal: goalBox.value });
      if (!set) return;
      open = new Set(); closed = new Set(); editing = null; rewriting = null; openDetails.clear();
    }
    await act("run", { auto });
  }

  let splitTimer = 0;
  $("goal").addEventListener("input", () => {
    sizeGoal();
    if (S) renderSide();
    clearTimeout(splitTimer);
    splitTimer = setTimeout(async () => {
      try {
        const out = await api("split", { goal: $("goal").value });
        $("split-preview").replaceChildren(...(out.features.length > 1 ? [h("span", { text: out.features.length + " steps:" }),
          out.features.map((text, index) => h("span", { class: "piece" }, h("i", { text: String(index + 1) }), text))] : []).flat());
      } catch (_) { /* preview only */ }
      render();
    }, 250);
  });
  $("goal").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || !S) return;
    event.preventDefault();
    if (event.repeat) return;
    renderSide();                                        // the buttons must reflect the text as it is now
    const button = event.ctrlKey || event.metaKey ? $("btn-run-all") : $("btn-run-step");
    if (!button.hidden && !button.disabled) button.click();
    else if ($("goal-note").textContent) toast($("goal-note").textContent);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("modal").hidden) { hideModal(); return; }
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || !S || !$("modal").hidden) return;
    const target = event.target;
    if (target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable)) return;
    if (event.key === "/") { event.preventDefault(); $("goal").focus(); return; }
    if (event.key === "?") { event.preventDefault(); helpModal(); return; }
    const wanted = { a: "approve", e: "edit", r: "rewrite", s: "skip" }[event.key.toLowerCase()];
    if (!wanted || event.repeat) return;
    const button = $("steps").querySelector('.step.current [data-key="' + wanted + '"]:not(:disabled)');
    if (button) { event.preventDefault(); button.click(); }
  });
  $("btn-run-all").addEventListener("click", () => startRun(true));
  $("btn-run-step").addEventListener("click", () => startRun(false));
  $("btn-stop").addEventListener("click", () => act("stop"));
  $("btn-refresh").addEventListener("click", () => act("refresh"));
  $("btn-undo-step").addEventListener("click", () => act("undo_step"));
  $("btn-undo-once").addEventListener("click", () => act("undo_once"));
  $("btn-check").addEventListener("click", async () => { toast("Counting…"); await act("check_log"); });
  $("btn-open-log").addEventListener("click", () => act("open_folder", { which: "log" }));
  $("btn-settings").addEventListener("click", settingsModal);
  $("btn-help").addEventListener("click", helpModal);
  $("btn-help-empty").addEventListener("click", helpModal);
  $("btn-diagnostics").addEventListener("click", diagnosticsModal);
  $("btn-theme").addEventListener("click", nextTheme);
  $("pill-model").addEventListener("click", () => { if (S) modelModal(); });
  $("pill-fusion").addEventListener("click", () => { if (S) fusionModal(); });
  $("pill-custom").addEventListener("click", () => { if (S) modelModal(); });
  $("btn-quit").addEventListener("click", async () => {
    try { await api("quit", {}); } catch (failure) { toast(failure.message); return; }
    stopped = true;
    document.body.replaceChildren(h("div", { class: "stopped" }, h("img", { src: "/ui/icon.svg", alt: "", width: "56", height: "56" }),
      h("h1", { text: "Fusion Needle has stopped." }),
      h("p", { text: "The model server is shut down. You can close this window." })));
  });

  showTheme(themeNow());
  loadStarters();
  poll(true);
  setInterval(() => poll(false), 800);
})();
