/* Cache22 owns commands and durable work; this module only renders snapshots. */
(() => {
  "use strict";
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const page = $("#c22-page"), inventory = $("#c22-inventory");
  if (!page && !inventory) return;
  const prefix = "/api/";
  const connection = $("#c22-connection");
  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text ?? "—";
    return node;
  };
  async function api(endpoint, body) {
    const response = await fetch(prefix + endpoint, {
      method: body === undefined ? "GET" : "POST", credentials: "same-origin",
      headers: body === undefined ? {} : {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }
  function showError(error) {
    connection.textContent = error.message;
    connection.classList.add("c22-error");
  }
  function table(columns, rows) {
    const node = element("table"); node.className = "c22-table";
    const head = node.createTHead().insertRow();
    for (const [title] of columns) { const th = element("th", title); th.scope = "col"; head.append(th); }
    const body = node.createTBody();
    for (const row of rows) {
      const tr = body.insertRow();
      for (const [, value] of columns) {
        const cell = tr.insertCell(), result = typeof value === "function" ? value(row) : row[value];
        if (result instanceof Node) cell.append(result); else cell.textContent = result ?? "—";
      }
    }
    return node;
  }
  function results(data) {
    const target = $("#c22-results"); target.replaceChildren();
    target.append(element("p", "Submission results — queued work has not necessarily completed."));
    target.append(table([["Line", "line"], ["Repository", r => r.repo_key || r.repository_id],
      ["Status", "status"], ["Job", "job_id"], ["Detail", r => r.error || (r.duplicate_of ? `Duplicate of line ${r.duplicate_of}` : "")]], data.results));
  }
  function actions(getIds, after) {
    const target = $("#c22-actions");
    const form = element("form"), select = element("select"), label = element("label", "Action ");
    for (const [value, text] of [["check", "Check remote"], ["fetch", "Fetch"], ["convert", "Convert to bundle"], ["unqueue", "Remove pending work"], ["schedule", "Set schedule"], ["disable", "Disable schedule"]]) {
      const option = element("option", text); option.value = value; select.append(option);
    }
    label.append(select); form.append(label);
    const intervalLabel = element("label", "Interval "), interval = element("input");
    interval.placeholder = "6h"; interval.value = "6h"; intervalLabel.hidden = true;
    intervalLabel.append(interval); form.append(intervalLabel);
    select.onchange = () => { intervalLabel.hidden = select.value !== "schedule"; };
    const submit = element("button", "Apply to selection"); submit.type = "submit"; form.append(submit);
    form.onsubmit = async event => {
      event.preventDefault(); submit.disabled = true;
      try {
        const ids = getIds();
        if (!ids.length) throw new Error("Select repositories first.");
        results(await api("command", {ids, action: select.value, every: interval.value}));
        await after();
      } catch (error) { showError(error); } finally { submit.disabled = false; }
    };
    target.append(form);
  }
  function polling(refresh, period) {
    let busy = false;
    const run = async () => {
      if (document.hidden || busy) return;
      busy = true;
      try {
        await refresh();
        connection.textContent = `Updated ${new Date().toLocaleTimeString()}`;
        connection.classList.remove("c22-error");
      } catch (error) { showError(new Error(`Refresh failed; showing last received data. Retrying. ${error.message}`)); }
      finally { busy = false; }
    };
    setInterval(run, period);
    document.addEventListener("visibilitychange", run);
    window.addEventListener("online", run);
    run();
    return run;
  }

  if (inventory) {
    const maxSelection = 10000;
    const storageKey = "cache22-selection";
    let selected = new Set();
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
      if (Array.isArray(saved)) selected = new Set(saved.filter(id => Number.isSafeInteger(id) && id > 0).slice(0, maxSelection));
    } catch { /* An unavailable store still permits selection for this page. */ }
    function syncSelection() {
      try { sessionStorage.setItem(storageKey, JSON.stringify([...selected])); }
      catch { showError(new Error("Selection cannot be saved in this tab; keep this page open.")); }
      let visible = 0;
      document.querySelectorAll(".c22-select").forEach(box => {
        box.checked = selected.has(Number(box.value));
        if (box.checked) visible++;
      });
      $("#c22-selection-count").textContent = `${selected.size} selected · ${visible} on this page (captured IDs)`;
    }
    function addIds(ids) {
      const next = new Set([...selected, ...ids]);
      if (next.size > maxSelection) {
        showError(new Error("Selection exceeds 10,000 repositories; clear or narrow your selection."));
      } else selected = next;
      syncSelection();
    }
    document.addEventListener("change", event => {
      if (!event.target.matches(".c22-select")) return;
      const id = Number(event.target.value);
      if (event.target.checked) addIds([id]);
      else { selected.delete(id); syncSelection(); }
    });
    $("#c22-page-select").onclick = () => addIds([...document.querySelectorAll(".c22-select")].map(box => Number(box.value)));
    $("#c22-clear-select").onclick = () => { selected.clear(); syncSelection(); };
    $("#c22-all-select").onclick = async event => {
      event.target.disabled = true;
      try { selected = new Set((await api("selection", {query: location.search.slice(1)})).ids); syncSelection(); }
      catch (error) { showError(error); } finally { event.target.disabled = false; }
    };
    $("#c22-filters").onsubmit = event => {
      event.preventDefault();
      const form = event.currentTarget;
      const values = new URLSearchParams(new FormData(form));
      form.querySelectorAll("[data-multi-filter]").forEach(input => {
        values.delete(input.name);
        input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean).forEach(value => values.append(input.name, value));
      });
      for (const [key, value] of [...values]) if (!value) values.delete(key);
      location.href = "/?" + values.toString();
    };
    async function refresh() {
      const query = location.search;
      const response = await fetch("/inventory/fragment" + query, {cache: "no-store", signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error(`Inventory HTTP ${response.status}`);
      const html = await response.text();
      if (query !== location.search) return;
      const target = $("#c22-inventory-data");
      const focused = target.contains(document.activeElement) ? document.activeElement.id : null;
      const openFacets = [...target.querySelectorAll(".c22-facets details")].map(node => node.open);
      // This endpoint returns only Cache22's own escaped results partial.
      target.innerHTML = html;
      target.querySelectorAll(".c22-facets details").forEach((node, index) => { node.open = openFacets[index]; });
      syncSelection();
      if (focused) (document.getElementById(focused) || connection).focus({preventScroll: true});
    }
    syncSelection();
    const update = polling(refresh, 5000);
    actions(() => [...selected], update);
  }

  if (page?.dataset.page === "add") {
    const form = $("#c22-add");
    async function submit(preview) {
      const values = new FormData(form);
      const buttons = form.querySelectorAll("button"); buttons.forEach(button => { button.disabled = true; });
      try {
        results(await api(preview ? "preview" : "register", {urls: values.get("urls"),
          root: values.get("root") || null, case_sensitive: values.has("case_sensitive"), fetch: values.get("fetch") === "true"}));
        connection.textContent = preview ? "Preview only; nothing registered." : "Submission complete.";
      } catch (error) { showError(error); } finally { buttons.forEach(button => { button.disabled = false; }); }
    }
    form.onsubmit = event => { event.preventDefault(); submit(false); };
    $("#c22-preview").onclick = () => submit(true);
  }

  if (page && ["queue", "repository"].includes(page.dataset.page)) {
    const queue = page.dataset.page === "queue";
    let offset = 0, nextOffset = null, generation = 0;
    const size = queue ? 100 : 50;
    function repositoryLink(row) {
      const link = element("a", row.repo_key || row.repository_id); link.href = `/repositories/${row.repository_id}`; return link;
    }
    function progress(row) {
      const div = element("div", row.phase || "Waiting for phase");
      if (row.live) {
        const bar = element("progress"); bar.max = 100;
        bar.setAttribute("aria-label", row.phase || "Operation in progress");
        if (row.percentage != null) bar.value = row.percentage;
        div.append(bar);
      } else if (row.state === "running") div.append(element("p", "Claim expired; awaiting recovery"));
      if (row.total != null) div.append(element("p", `${row.completed}/${row.total} ${row.unit || ""} (phase only)`));
      if (row.observed_at) div.append(element("small", `Observed ${row.observed_at}`));
      if (row.detail) div.append(element("p", row.detail));
      return div;
    }
    async function refresh() {
      const current = generation;
      const section = queue ? $("#c22-section").value : null;
      const data = await api(queue ? `queue?section=${section}&offset=${offset}` : `detail?id=${page.dataset.id}&offset=${offset}`);
      if (current !== generation) return;
      nextOffset = data.next_offset;
      $("#c22-prev").disabled = offset === 0; $("#c22-next").disabled = nextOffset == null;
      $("#c22-page-number").textContent = `Page ${Math.floor(offset / size) + 1}`;
      if (queue) {
        const workers = $("#c22-workers"); workers.replaceChildren();
        if (!data.workers.some(worker => worker.available)) workers.append(element("p", "No available worker. Requests remain queued."));
        for (const worker of data.workers) workers.append(element("p", `Worker ${worker.pid}: ${worker.stopped_at ? "stopped" : !worker.available ? "stale" : worker.current_job_id ? `running job ${worker.current_job_id}` : "idle"}; heartbeat ${worker.heartbeat_age_seconds}s ago`));
        $("#c22-counts").textContent = Object.entries(data.counts).map(([name, count]) => `${name}: ${count}`).join(" · ");
        $("#c22-jobs").replaceChildren(table([["Job", "id"], ["Repository", repositoryLink], ["Operation", "kind"], ["Origin", "origin"],
          ["State", "state"], ["Attempt", "attempt_number"], ["Due / retry", "due_at"], ["Waiting for job", "blocking_job_id"], ["Elapsed (s)", "elapsed_seconds"], ["Phase", progress], ["Diagnostic", r => r.diagnostic?.error]], data.jobs));
      } else {
        const detail = $("#c22-detail"); detail.replaceChildren();
        for (const [key, value] of Object.entries(data.repository)) {
          detail.append(element("dt", key.replaceAll("_", " ")), element("dd", value));
        }
        $("#c22-attempts").replaceChildren(table([["Attempt", "id"], ["Job", "job_id"], ["Operation", "kind"], ["Origin", "origin"],
          ["Started", "started_at"], ["Finished", "finished_at"], ["Outcome", "outcome"], ["Last phase", "phase"], ["Diagnostic", "error"]], data.attempts));
      }
    }
    const update = polling(refresh, queue ? 1000 : 5000);
    $("#c22-prev").onclick = () => { offset = Math.max(0, offset - size); generation++; update(); };
    $("#c22-next").onclick = () => { if (nextOffset != null) { offset = nextOffset; generation++; update(); } };
    if (queue) $("#c22-section").onchange = () => { offset = 0; generation++; update(); };
    else actions(() => [Number(page.dataset.id)], update);
  }
})();
