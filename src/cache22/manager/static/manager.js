/* Cache22 owns commands and durable work; this module only renders snapshots. */
(() => {
  "use strict";
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const page = $("#c22-page"), inventory = $("#c22-inventory");
  if (!page && !inventory) return;
  const prefix = "/-/cache22/api/";
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
    let selected;
    try { selected = new Set(JSON.parse(sessionStorage.getItem("cache22-selection") || "[]")); }
    catch { selected = new Set(); }
    function syncSelection() {
      sessionStorage.setItem("cache22-selection", JSON.stringify([...selected]));
      $("#c22-selection-count").textContent = `${selected.size} selected (IDs captured; selection survives filters)`;
      document.querySelectorAll(".c22-select").forEach(box => { box.checked = selected.has(Number(box.value)); });
    }
    document.addEventListener("change", event => {
      if (!event.target.matches(".c22-select")) return;
      const id = Number(event.target.value);
      if (event.target.checked) selected.add(id); else selected.delete(id);
      syncSelection();
    });
    $("#c22-page-select").onclick = () => { document.querySelectorAll(".c22-select").forEach(box => selected.add(Number(box.value))); syncSelection(); };
    $("#c22-clear-select").onclick = () => { selected.clear(); syncSelection(); };
    $("#c22-all-select").onclick = async event => {
      event.target.disabled = true;
      try { selected = new Set((await api("selection", {query: location.search.slice(1)})).ids); syncSelection(); }
      catch (error) { showError(error); } finally { event.target.disabled = false; }
    };
    function search() {
      const url = new URL(location.href); url.searchParams.set("_q", $("#c22-search").value); url.searchParams.delete("_next"); location.href = url;
    }
    $("#c22-search-apply").onclick = search;
    $("#c22-search").onkeydown = event => { if (event.key === "Enter") { event.preventDefault(); search(); } };
    async function refresh() {
      const response = await fetch(location.href, {cache: "no-store", signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error(`Inventory HTTP ${response.status}`);
      const next = new DOMParser().parseFromString(await response.text(), "text/html");
      const currentBody = $(".rows-and-columns tbody"), nextBody = $(".rows-and-columns tbody", next);
      if (currentBody && nextBody) {
        if ([...nextBody.rows].some(row => !$(".c22-select", row))) {
          throw new Error("Include the id column to enable live inventory refresh and row selection.");
        }
        const oldRows = new Map([...currentBody.rows].map(row => [$(".c22-select", row)?.value, row]));
        const wanted = new Set();
        for (const nextRow of nextBody.rows) {
          const id = $(".c22-select", nextRow)?.value; wanted.add(id);
          const old = oldRows.get(id);
          if (old) {
            if (!old.contains(document.activeElement)) old.replaceChildren(...[...nextRow.cells].map(cell => document.importNode(cell, true)));
            currentBody.append(old);
          } else currentBody.append(document.importNode(nextRow, true));
        }
        for (const [id, row] of oldRows) if (!wanted.has(id)) row.remove();
      } else if (!currentBody && nextBody) {
        const oldWrapper = $(".above-table-panel");
        oldWrapper?.after(document.importNode(nextBody.closest(".table-wrapper"), true));
      } else if (currentBody && !nextBody) currentBody.replaceChildren();
      for (const selector of [".table-summary", ".facet-results", ".pagination", ".zero-results"]) {
        const current = $(selector), replacement = $(selector, next);
        if (current && replacement) current.replaceWith(document.importNode(replacement, true));
        else if (current && !replacement) current.remove();
      }
      const nextLink = doc => [...doc.querySelectorAll("a")].find(link => link.textContent.trim() === "Next page");
      const oldNext = nextLink(document), newNext = nextLink(next);
      if (oldNext && newNext) oldNext.href = newNext.getAttribute("href");
      else if (oldNext) oldNext.parentElement.remove();
      else if (newNext) $(".table-wrapper")?.after(document.importNode(newNext.parentElement, true));
      syncSelection();
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
      const link = element("a", row.repo_key || row.repository_id); link.href = `/-/cache22/repository/${row.repository_id}`; return link;
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
