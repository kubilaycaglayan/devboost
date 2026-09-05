    let currentForwards = [];
    let currentServerHost = "";
    let currentServerId = localStorage.getItem("devboost-active-server") || "";
    let cachedHistory = [];
    let allServers = [];
    let sshHostCache = [];
    let serverReachability = {};
    let currentSyncs = [];
    let cachedFolderHistory = [];
    let folderChipCache = [];
    let editingSyncId = null;
    let editingUsageId = null;
    let usageAccounts = [];
    let usageSnapshots = {};
    let usageRequestInFlight = false;
    let usageDrag = null;
    let usageOrderSaving = false;
    let usageOrderVersion = 0;
    let usageNameAuto = false;
    let usageTab = "remote";
    const usageMetrics = globalThis.DevBoostUsageMetrics;
    let browserLocalCur = "";
    let browserRemoteCur = "";
    let serversLoaded = false;
    let editingServerId = null;
    let serverDrag = null;
    let serverOrderSaving = false;
    let serverOrderVersion = 0;

    function openUsageModal(account = null) {
      editingUsageId = account ? account.id : null;
      usageNameAuto = !account;
      document.getElementById("usage-modal-title").textContent = account ? "Edit AI Usage Account" : "Add AI Usage Account";
      document.getElementById("usage-save-button").textContent = account ? "Save Changes" : "Save Account";
      document.getElementById("usage-provider").value = account ? (account.provider || "custom") : "codex";
      document.getElementById("usage-name").value = account ? (account.name || "") : usageProviderLabel();
      document.getElementById("usage-command").value = account ? usageCommandValue(account.usage_command) : "";
      document.getElementById("usage-url").value = account ? (account.balance_url || "") : "";
      document.getElementById("usage-token-env").value = account ? (account.token_env || "") : "";
      document.getElementById("usage-local-path").value = account ? (account.local_path || "") : "";
      document.getElementById("usage-codex-home").value = account ? (account.codex_home || "") : "";
      updateCodexUsageFields();
      document.getElementById("usage-organization").value = account ? (account.organization || "") : "";
      document.getElementById("usage-project").value = account ? (account.project || "") : "";
      document.getElementById("usage-api-mode").checked = Boolean(account && account.api_mode);
      document.getElementById("usage-api-days").value = account ? (account.api_days || 1) : 1;
      document.getElementById("usage-modal").style.display = "flex";
    }
    function closeUsageModal() { editingUsageId = null; document.getElementById("usage-modal").style.display = "none"; }
    function usageProviderLabel() {
      const provider = document.getElementById("usage-provider");
      return provider.options[provider.selectedIndex]?.textContent.trim() || provider.value;
    }
    function usageNameChanged() { usageNameAuto = false; }
    function updateCodexUsageFields() {
      if (usageNameAuto) document.getElementById("usage-name").value = usageProviderLabel();
      document.getElementById("usage-codex-settings").hidden = document.getElementById("usage-provider").value !== "codex";
    }
    function usageCommandValue(command) {
      if (!Array.isArray(command)) return command || "";
      return command.map(part => {
        part = String(part);
        return /[\\s"'\\\\]/.test(part) ? '"' + part.replace(/(["\\\\])/g, "\\\\$1") + '"' : part;
      }).join(" ");
    }
    function usageNumber(value) { return value == null ? "—" : String(value); }
    function formatTokenCount(value) {
      const count = Number(value);
      if (!Number.isFinite(count)) return usageNumber(value);
      const magnitude = Math.abs(count);
      const scale = magnitude >= 1e9 ? [1e9, "B"] : magnitude >= 1e6 ? [1e6, "M"] : magnitude >= 1e3 ? [1e3, "K"] : [1, ""];
      const scaled = scale[0] === 1e3 ? Math.trunc(count / scale[0]) : count / scale[0];
      const precision = scale[0] === 1 || scale[0] === 1e3 ? 0 : Math.abs(scaled) >= 100 ? 0 : 1;
      const suffix = scale[1] === "K" ? "k" : scale[1];
      return `${Number(scaled.toFixed(precision))}${suffix}`;
    }
    function usagePercent(quota) {
      return usageMetrics.usagePercent(quota);
    }
    function usageGrade(percent) {
      return usageMetrics.usageGrade(percent);
    }
    function usageResetTime(value) {
      return usageMetrics.formatResetTime(value);
    }
    function renderUsageQuota(quota) {
      const percent = usagePercent(quota);
      const name = escapeHtml(quota.name);
      const reset = usageResetTime(quota.reset_at);
      if (percent == null) {
        const unit = String(quota.unit || "");
        const tokenUsage = quota.used != null && /^tokens?$/i.test(unit);
        const detail = quota.remaining != null ? `${usageNumber(quota.remaining)} ${escapeHtml(unit || "remaining")}` :
          (quota.used != null ? `${usageNumber(quota.used)} ${escapeHtml(unit || "used")}` : "No percentage data");
        // Token totals are easiest to scan with the compact amount first.
        const content = tokenUsage
          ? `<strong>${formatTokenCount(quota.used)} tokens used</strong>`
          : `<span>${name}</span><strong>${detail}</strong>`;
        return `<div class="usage-quota-text">${content}${reset ? `<small>${escapeHtml(reset)}</small>` : ""}</div>`;
      }
      const remainingPercent = 100 - percent;
      const rounded = Math.round(remainingPercent);
      const remaining = quota.remaining != null && quota.unit !== "%" ? `${usageNumber(quota.remaining)} ${quota.unit || "remaining"} remaining` : "";
      const caption = remaining ? `<div class="usage-meter-caption">${escapeHtml(remaining)}</div>` : "";
      return `<div class="usage-quota"><div class="usage-meter-header"><span>${name}</span>${reset ? `<small class="usage-meter-reset">${escapeHtml(reset)}</small>` : ""}</div><div class="usage-meter ${usageGrade(percent)}" role="meter" aria-label="${name} remaining quota" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${rounded}"><span style="width:${remainingPercent.toFixed(2)}%"></span><strong class="usage-meter-label">${rounded}% left</strong></div>${caption}</div>`;
    }
    function usageUpdated(snapshot) {
      const timestamp = snapshot && (snapshot.last_valid_query_at || (snapshot.ok && !snapshot.stale && snapshot.updated_at));
      if (!timestamp) return "—";
      const elapsed = Math.max(0, Date.now() - new Date(timestamp).getTime());
      const seconds = Math.floor(elapsed / 1000);
      if (seconds < 60) return `${seconds}s`;
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return `${minutes}m`;
      const hours = Math.floor(minutes / 60);
      if (hours < 24) return `${hours}h`;
      const days = Math.floor(hours / 24);
      return `${days}d`;
    }
    function setHomeSummary(id, text) {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    }
    function setHomeSummaryParts(id, parts) {
      const el = document.getElementById(id);
      if (!el) return;
      el.replaceChildren(...parts.map(part => {
        const span = document.createElement("span");
        span.className = part.className || "tile-detail";
        span.textContent = part.text;
        return span;
      }));
    }
    function summarySeparator() { return {text: " · ", className: "tile-separator"}; }
    function renderHomeForwardsSummary(forwards) {
      const ports = (forwards || []).map(f => `:${f.local_port}`);
      if (!ports.length) { setHomeSummary("home-forwards-summary", "No forwarded ports"); return; }
      const parts = [{text: `${ports.length} forwarded`, className: "tile-summary-count"}];
      ports.forEach((port, index) => {
        parts.push(summarySeparator(), {text: port, className: "tile-detail tile-port"});
      });
      setHomeSummaryParts("home-forwards-summary", parts);
    }
    function renderHomeDockerSummary(containers) {
      const groups = {};
      (containers || []).forEach(container => {
        const labels = (container.labels || []).map(label => label.name).filter(Boolean);
        (labels.length ? labels : ["Untagged"]).forEach(label => { groups[label] = (groups[label] || 0) + 1; });
      });
      const tags = Object.entries(groups);
      if (!containers || !containers.length) { setHomeSummary("home-docker-summary", "No running containers"); return; }
      const parts = [{text: `${containers.length} running`, className: "tile-summary-count"}];
      tags.forEach(([label, count]) => parts.push(summarySeparator(), {text: `${count} ${label}`, className: "tile-detail tile-docker"}));
      setHomeSummaryParts("home-docker-summary", parts);
    }
    function renderHomeSyncSummary(syncs) {
      const paths = (syncs || []).map(sync => `${sync.local_path} → ${sync.remote_path}`);
      const shown = paths.slice(0, 2);
      if (paths.length > shown.length) shown.push(`+${paths.length - shown.length} more`);
      if (!shown.length) { setHomeSummary("home-syncs-summary", "No active folder syncs"); return; }
      const parts = [{text: `${paths.length} active`, className: "tile-summary-count"}];
      shown.forEach(path => parts.push(summarySeparator(), {text: path, className: "tile-detail tile-sync"}));
      setHomeSummaryParts("home-syncs-summary", parts);
    }
    function renderHomeUsageSummary(accounts, snapshots) {
      const summaries = (accounts || []).map(account => {
        const snapshot = (snapshots || {})[account.id] || {};
        const left = [];
        (snapshot.quotas || []).filter(q => q.remaining != null).forEach(q => left.push(`${q.remaining} ${q.unit || "left"}`));
        (snapshot.balances || []).filter(b => b.remaining != null).forEach(b => left.push(`${b.remaining} ${b.currency || "USD"} left`));
        return left.length ? `${account.name}: ${left.join(", ")}` : null;
      }).filter(Boolean);
      if (!summaries.length) { setHomeSummary("home-usage-summary", "No remaining usage data"); return; }
      const parts = [];
      summaries.forEach(summary => parts.push(...(parts.length ? [summarySeparator()] : []), {text: summary, className: "tile-detail tile-usage"}));
      setHomeSummaryParts("home-usage-summary", parts);
    }
    function renderUsage(data) {
      if (usageDrag) return;
      const body = document.getElementById("usage-body");
      usageAccounts = data.accounts || [];
      usageSnapshots = data.snapshots || {};
      renderHomeUsageSummary(usageAccounts, usageSnapshots);
      if (!usageAccounts.length) {
        body.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:24px;">No AI accounts configured.</td></tr>'; return;
      }
      body.innerHTML = usageAccounts.map(a => {
        const s = usageSnapshots[a.id] || {};
        // Only Codex reports a count of reset credits.  Other providers'
        // reset_at fields describe when a quota window renews, not resets the
        // user can spend.
        const resetCount = a.provider === "codex" ? (s.available_resets || 0) : 0;
        const resetSummary = resetCount ? `<span class="usage-reset-summary" role="status" aria-label="${resetCount} usage limit resets available">${resetCount}X RESETS</span>` : "";
        const warning = s.stale ? `<div class="usage-stale" role="status">Stale · ${escapeHtml(s.message || "Live quota could not be verified.")}</div>` : "";
        const quotas = warning + resetSummary + ((s.quotas || []).map(renderUsageQuota).join("") || (s.ok ? "No quota data" : escapeHtml(s.message || "Unavailable")));
        const balances = a.provider === "codex" && s.credits_unlimited ? "Unlimited credits" : ((s.balances || []).map(b => b.remaining != null ? `${usageNumber(b.remaining)} ${escapeHtml(b.currency || "USD")}` : (b.spent != null ? `spent ${usageNumber(b.spent)} ${escapeHtml(b.currency || "USD")}` : "—")).join("<br>") || "—");
        const accountId = escapeHtml(JSON.stringify(String(a.id || "")));
        const source = (a.provider === "codex" || a.provider === "agy") ? `<div class="usage-source">${escapeHtml(s.source || "Awaiting live query")}${s.plan_type ? " · " + escapeHtml(s.plan_type) : ""}</div>` : "";
        return `<tr data-usage-id="${escapeHtml(String(a.id || ""))}"><td>${escapeHtml(a.provider)}</td><td>${escapeHtml(a.name)}${source}</td><td>${quotas}</td><td>${balances}</td><td class="mono usage-age" data-last-valid-query="${escapeHtml(s.last_valid_query_at || (s.ok && !s.stale ? s.updated_at : ""))}">${usageUpdated(s)}</td><td><div class="usage-actions"><button type="button" class="usage-action-icon" onclick="editUsageAccount(${accountId})" title="Edit account" aria-label="Edit account">⚙</button><button type="button" class="usage-action-icon usage-remove-icon" onclick="removeUsageAccount(${accountId})" title="Remove account" aria-label="Remove account">✕</button><button type="button" class="usage-drag-handle" draggable="true" ondragstart="onUsageRowDragStart(event, ${accountId})" ondragend="onUsageRowDragEnd()" title="Drag to reorder" aria-label="Drag to reorder">⠿</button></div></td></tr>`;
      }).join("");
    }
    function onUsageRowDragStart(e, accountId) {
      startUsageRowDrag(e, accountId, e.currentTarget.closest("tr"));
    }
    function onUsageRowDragOver(e) {
      moveUsageRowDrag(e);
    }
    function onUsageRowDragEnd() {
      endUsageRowDrag();
    }
    function onUsageRowDrop(e) {
      return dropUsageRowDrag(e);
    }
    function startUsageRowDrag(e, accountId, source) {
      if (usageOrderSaving || usageDrag || !source) { e.preventDefault(); return; }
      usageOrderVersion++;
      const body = source.parentNode;
      const rect = source.getBoundingClientRect();
      const shadow = document.createElement("tr");
      shadow.className = "usage-row-drop-shadow";
      shadow.innerHTML = '<td colspan="6">Drop account here</td>';
      shadow.style.height = `${rect.height}px`;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", accountId);
      e.dataTransfer.setDragImage(source, e.clientX - rect.left, e.clientY - rect.top);
      source.classList.add("dragging");
      source.setAttribute("aria-grabbed", "true");
      const drag = usageDrag = {accountId: String(accountId), source, shadow, body};
      // Hiding the source during dragstart cancels native dragging in Chrome.
      drag.timer = setTimeout(() => {
        if (usageDrag !== drag) return;
        body.insertBefore(shadow, source);
        source.style.display = "none";
      }, 0);
    }
    function moveUsageRowDrag(e) {
      const drag = usageDrag;
      if (!drag || e.currentTarget !== drag.body) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!drag.shadow.isConnected || drag.shadow.contains(e.target)) return;
      const rows = Array.from(drag.body.children).filter(row => row.dataset.usageId && row !== drag.source);
      const next = rows.find(row => {
        const rect = row.getBoundingClientRect();
        return e.clientY < rect.top + rect.height / 2;
      });
      drag.body.insertBefore(drag.shadow, next);
    }
    function endUsageRowDrag() {
      if (!usageDrag) return;
      const {source, shadow, timer} = usageDrag;
      clearTimeout(timer);
      source.classList.remove("dragging");
      source.style.display = "";
      source.removeAttribute("aria-grabbed");
      shadow.remove();
      usageDrag = null;
      renderUsage({accounts: usageAccounts, snapshots: usageSnapshots});
    }
    async function dropUsageRowDrag(e) {
      const drag = usageDrag;
      if (!drag || e.currentTarget !== drag.body || !drag.shadow.isConnected) return;
      e.preventDefault();
      const order = Array.from(drag.body.children)
        .filter(row => row === drag.shadow || (row.dataset.usageId && row !== drag.source))
        .map(row => row === drag.shadow ? drag.accountId : row.dataset.usageId);
      const previous = usageAccounts.slice();
      if (order.every((id, index) => id === previous[index].id)) { endUsageRowDrag(); return; }
      usageOrderSaving = true;
      usageAccounts = order.map(id => previous.find(account => String(account.id) === id)).filter(Boolean);
      endUsageRowDrag();
      try {
        const res = await fetch("/api/usage/accounts/reorder", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({order})});
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.message || "Usage account reorder failed");
        usageAccounts = data.accounts || usageAccounts;
      } catch (err) {
        usageAccounts = previous;
        console.error(err);
        showToast("Couldn't save quota order. Please try again.");
      } finally {
        usageOrderSaving = false;
        renderUsage({accounts: usageAccounts, snapshots: usageSnapshots});
      }
    }
    function refreshUsageAges() {
      document.querySelectorAll(".usage-age").forEach(cell => {
        const timestamp = cell.dataset.lastValidQuery;
        if (timestamp) cell.textContent = usageUpdated({last_valid_query_at: timestamp});
      });
    }
    function editUsageAccount(id) {
      const account = usageAccounts.find(item => item.id === id);
      if (account) openUsageModal(account);
    }
    function usageTargetId() {
      return usageTab === "local" ? "local" : (currentServerId || "local");
    }
    function updateUsageTitle() {
      const title = document.getElementById("usage-section-title");
      if (!title) return;
      if (usageTab === "local") {
        title.innerText = "AI Usage & Balances on This Mac";
        return;
      }
      const srv = activeServer();
      title.innerText = `AI Usage & Balances on ${(srv && (srv.name || srv.ssh_host)) || "Remote Server"}`;
    }
    function switchUsageTab(which) {
      usageTab = which;
      document.getElementById("usage-tab-remote").classList.toggle("active", which === "remote");
      document.getElementById("usage-tab-local").classList.toggle("active", which === "local");
      updateUsageTitle();
      fetchUsage();
    }
    async function fetchUsage(refresh = false) {
      if (usageRequestInFlight || usageDrag || usageOrderSaving) return;
      usageRequestInFlight = true;
      const requestedTargetId = usageTargetId();
      const orderVersion = usageOrderVersion;
      try {
        const params = new URLSearchParams();
        if (refresh) params.set("refresh", "1");
        if (usageTab === "remote" && currentServerId) params.set("server", currentServerId);
        const query = params.toString() ? "?" + params.toString() : "";
        const response = await fetch("/api/usage" + query, {cache: "no-store"});
        const data = await response.json();
        if (requestedTargetId !== usageTargetId() || data.server_id !== requestedTargetId || usageDrag || usageOrderSaving || orderVersion !== usageOrderVersion) return;
        renderUsage(data);
      }
      catch (err) { document.getElementById("usage-body").innerHTML = '<tr><td colspan="6" class="muted">Usage monitor unavailable</td></tr>'; }
      finally {
        usageRequestInFlight = false;
        // A target change while the request was in flight invalidates its
        // response. Start the request for the newly selected machine now.
        if (requestedTargetId !== usageTargetId()) fetchUsage(refresh);
      }
    }
    async function saveUsageFromModal() {
      const payload = {id: editingUsageId, provider: document.getElementById("usage-provider").value, name: document.getElementById("usage-name").value.trim(), usage_command: document.getElementById("usage-command").value.trim(), balance_url: document.getElementById("usage-url").value.trim(), token_env: document.getElementById("usage-token-env").value.trim(), local_path: document.getElementById("usage-local-path").value.trim(), organization: document.getElementById("usage-organization").value.trim(), project: document.getElementById("usage-project").value.trim(), api_mode: document.getElementById("usage-api-mode").checked, api_days: parseInt(document.getElementById("usage-api-days").value, 10) || 1};
      const autoSource = ["codex", "agy", "claude", "opencode"].includes(payload.provider);
      if (payload.provider === "codex") payload.codex_home = document.getElementById("usage-codex-home").value.trim();
      if (!payload.name || (!payload.usage_command && !payload.balance_url && !autoSource)) { alert("Enter an account name and a command or HTTPS URL."); return; }
      const response = await fetch("/api/usage/accounts", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      const data = await response.json(); if (!data.ok) { alert(data.message || "Could not save account"); return; }
      closeUsageModal(); fetchUsage(true);
    }
    async function removeUsageAccount(id) {
      if (!confirm("Remove this usage account?")) return;
      await fetch("/api/usage/accounts/remove", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:id})}); fetchUsage();
    }

    function escapeHtml(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    }

    function showToast(msg) {
      const t = document.getElementById("toast");
      t.innerText = msg;
      t.style.display = "block";
      setTimeout(() => { t.style.display = "none"; }, 3500);
    }

    function syncRemotePort() {
      const lp = document.getElementById("modal-local-port").value;
      const rp = document.getElementById("modal-remote-port");
      if (!rp.value || rp.dataset.autoSynced === "true") {
        rp.value = lp;
        rp.dataset.autoSynced = "true";
      }
    }

    function openAddModal(local = "", remote = "", label = "") {
      document.getElementById("modal-local-port").value = local;
      document.getElementById("modal-remote-port").value = remote || local;
      document.getElementById("modal-remote-port").dataset.autoSynced = "";
      const labelEl = document.getElementById("modal-label");
      labelEl.value = label;
      // If a label was explicitly passed (e.g. from Scan), treat as manual;
      // otherwise allow auto-fill from stored SERVICE/LABEL mapping.
      labelEl.dataset.autoFilled = label ? "false" : "true";
      if (!label && local) maybeAutofillLabel();
      document.getElementById("modal-always").checked = true;
      document.getElementById("modal-remote-label").innerText = "Remote Port (on " + (currentServerHost || 'server') + ")";
      renderRecentChips(local);
      document.getElementById("add-modal").style.display = "flex";
      document.getElementById("modal-local-port").focus();
      // Refresh history in background so quick-select is always fresh (per active tab)
      fetch("/api/history" + serverQuery()).then(r => r.json()).then(d => {
        if (d.history) {
          const forwarded = new Set((currentForwards || []).map(f => f.local_port));
          const cur = document.getElementById("modal-local-port").value;
          cachedHistory = d.history.filter(h => !forwarded.has(h.local_port) && String(h.local_port) !== String(cur));
          renderRecentChips(cur);
        }
      }).catch(() => {});
    }

    function renderRecentChips(preselectLocal = "") {
      const section = document.getElementById("recent-ports-section");
      const container = document.getElementById("recent-ports-chips");
      const title = document.getElementById("recent-ports-title");
      let items = (cachedHistory || []).filter(h => String(h.local_port) !== String(preselectLocal));
      // Fallback to common ports when no history yet
      if (!cachedHistory || cachedHistory.length === 0) {
        const forwarded = new Set((currentForwards || []).map(f => f.local_port));
        const common = [
          {local_port: 3000, remote_port: 3000, label: "Docker Web / App"},
          {local_port: 3030, remote_port: 3030, label: "Grafana Dashboard"},
          {local_port: 8080, remote_port: 8080, label: "Docker Proxy / Web App"},
          {local_port: 9090, remote_port: 9090, label: "Prometheus Metrics"},
          {local_port: 11434, remote_port: 11434, label: "Ollama LLM API"},
        ].filter(c => !forwarded.has(c.local_port) && String(c.local_port) !== String(preselectLocal));
        if (common.length === 0) { section.style.display = "none"; return; }
        title.innerText = "Common ports — click to quick select";
        items = common;
      } else {
        title.innerText = "Previously used — click to quick select";
      }
      if (items.length === 0) { section.style.display = "none"; return; }
      section.style.display = "block";
      quickSelectCache = items;
      container.innerHTML = items.map((h, i) => {
        const remoteSuffix = (h.remote_port && h.remote_port !== h.local_port) ? ` → :${h.remote_port}` : "";
        return `<button class="recent-chip" onclick="quickSelectRecent(${i})" title="${escapeHtml(h.label || '')}">:${h.local_port}${remoteSuffix}<small>${escapeHtml((h.label || '').slice(0, 22))}</small></button>`;
      }).join("");
    }

    let quickSelectCache = [];

    function lookupStoredLabel(port) {
      const key = String(port == null ? "" : port).trim();
      if (!key) return "";
      const pools = [cachedHistory || [], quickSelectCache || [], currentForwards || []];
      for (const pool of pools) {
        const hit = pool.find(x => String(x.local_port) === key);
        if (hit && hit.label) return hit.label;
      }
      return "";
    }

    function maybeAutofillLabel() {
      const lp = document.getElementById("modal-local-port").value;
      const labelEl = document.getElementById("modal-label");
      if (!lp) return;
      // Don't clobber text the user typed manually
      if (labelEl.value && labelEl.dataset.autoFilled !== "true") return;
      const found = lookupStoredLabel(lp);
      if (found) {
        labelEl.value = found;
        labelEl.dataset.autoFilled = "true";
      }
    }

    function quickSelectRecent(i) {
      const h = quickSelectCache[i];
      if (!h) return;
      document.getElementById("modal-local-port").value = h.local_port;
      document.getElementById("modal-remote-port").value = h.remote_port || h.local_port;
      document.getElementById("modal-remote-port").dataset.autoSynced = "";
      const labelEl = document.getElementById("modal-label");
      labelEl.value = h.label || "";
      labelEl.dataset.autoFilled = "false";
      renderRecentChips(h.local_port);
      document.getElementById("modal-local-port").focus();
    }

    function closeAddModal() {
      document.getElementById("add-modal").style.display = "none";
    }

    function activeServer() {
      return (allServers || []).find(s => s.id === currentServerId) || allServers[0] || null;
    }

    function serverQuery() {
      return currentServerId ? `?server=${encodeURIComponent(currentServerId)}` : "";
    }

    // A response is safe to render only while the selection that produced it
    // is still active. Some endpoints may not echo a server id, so an absent
    // response id is accepted when the request itself was server-scoped.
    function responseBelongsToServer(requestedServerId, activeServerId, responseServerId = "") {
      return requestedServerId === activeServerId &&
        (!requestedServerId || !responseServerId || responseServerId === requestedServerId);
    }

    async function fetchServers() {
      if (serverDrag || serverOrderSaving) return;
      const orderVersion = serverOrderVersion;
      try {
        const res = await fetch("/api/servers");
        const data = await res.json();
        if (serverDrag || serverOrderSaving || orderVersion !== serverOrderVersion) return;
        allServers = data.servers || [];
        // A background refresh must not undo an explicit tab selection. Only
        // choose a default during startup (or after the selected tab was
        // deliberately cleared, e.g. when it was removed).
        if ((!currentServerId || (!serversLoaded && !allServers.some(s => s.id === currentServerId))) && allServers.length) {
          currentServerId = (allServers[0] || {}).id || "";
          localStorage.setItem("devboost-active-server", currentServerId);
        }
        serversLoaded = true;
        renderTabs();
        renderServerManagement();
        updateUsageTitle();
      } catch (err) {
        console.error(err);
      }
    }

    function renderTabs() {
      if (serverDrag) return;
      const bar = document.getElementById("tabs-bar");
      if (!allServers.length) {
        bar.innerHTML = '<span style="font-size:12px; color:var(--text-muted);">No servers yet.</span>' +
          '<button class="btn btn-sm server-manage-button" onclick="navigatePage(\'servers\')">Manage servers</button>';
        return;
      }
      bar.innerHTML = allServers.map(s => {
        const isActive = s.id === currentServerId;
        const dotCls = serverReachability[s.id] === true ? "tab-dot online" : (serverReachability[s.id] === false ? "tab-dot" : "tab-dot");
        return `<div class="tab ${isActive ? 'active' : ''}" data-server-id="${s.id}" draggable="true"
            onclick="selectServer('${s.id}')" ondragstart="onServerTabDragStart(event, '${s.id}')"
            ondragend="onServerTabDragEnd()"
            title="${escapeHtml(s.ssh_host)}${s.ip ? ' (' + escapeHtml(s.ip) + ')' : ''}">
          <span class="${dotCls}"></span>
          <span class="tab-name">${escapeHtml(s.name || s.ssh_host)}</span>
        </div>`;
      }).join("") + `<button class="btn btn-sm server-manage-button" onclick="navigatePage('servers')">Manage servers</button>`;
    }

    function onServerTabDragStart(e, sid) {
      startServerDrag(e, sid, "tab", e.currentTarget);
    }
    function onServerTabDragOver(e) {
      moveServerDrag(e);
    }
    function onServerTabDragEnd() {
      endServerDrag();
    }
    function onServerTabDrop(e) {
      return dropServerDrag(e);
    }

    function renderServerManagement() {
      if (serverDrag) return;
      const list = document.getElementById("server-management-list");
      if (!list) return;
      if (!allServers.length) {
        list.innerHTML = '<div class="empty-state">No servers configured yet. Add an SSH connection to get started.</div>';
        return;
      }
      list.innerHTML = allServers.map(s => {
        const isActive = s.id === currentServerId;
        const reachability = serverReachability[s.id];
        const dotCls = reachability === true ? "tab-dot online" : "tab-dot";
        const status = reachability === true ? "Online" : (reachability === false ? "Offline" : "Not checked");
        return `<article class="server-management-item" data-server-id="${s.id}">
          <div class="server-management-details">
            <div class="server-management-name"><span class="${dotCls}"></span>${escapeHtml(s.name || s.ssh_host)}${isActive ? '<span class="badge badge-active">Active</span>' : ''}</div>
            <div class="server-management-host">${escapeHtml(s.ssh_host)}${s.ip ? ` · ${escapeHtml(s.ip)}` : ''}</div>
            <div class="server-management-status">${status}</div>
          </div>
          <div class="server-management-actions">
            <button class="server-drag-handle" draggable="true" ondragstart="onServerCardDragStart(event, '${s.id}')" ondragend="onServerCardDragEnd(event)" title="Drag to reorder" aria-label="Drag to reorder">⠿</button>
            <button class="btn btn-sm" onclick="selectServer('${s.id}'); navigatePage('forwards')">Use server</button>
            <button class="btn btn-sm" onclick="editServer('${s.id}')">Edit</button>
            <button class="btn btn-sm btn-danger" onclick="removeServerTab('${s.id}')">Remove</button>
          </div>
        </article>`;
      }).join("");
    }

    function onServerCardDragStart(e, sid) {
      startServerDrag(e, sid, "card", e.currentTarget.closest(".server-management-item"));
    }
    function onServerCardDragOver(e) {
      moveServerDrag(e);
    }
    function onServerCardDragEnd() {
      endServerDrag();
    }
    function onServerCardDrop(e) {
      return dropServerDrag(e);
    }

    function startServerDrag(e, sid, kind, source) {
      if (serverOrderSaving || serverDrag) { e.preventDefault(); return; }
      // Invalidate responses requested before this drag, even if they arrive
      // after its save completes, so polling cannot restore an old order.
      serverOrderVersion++;
      const rect = source.getBoundingClientRect();
      const shadow = document.createElement("div");
      shadow.className = kind === "tab" ? "tab server-tab-drop-shadow" : "server-management-item server-card-drop-shadow";
      if (kind === "tab") shadow.style.width = `${rect.width}px`;
      shadow.style.height = `${rect.height}px`;
      shadow.setAttribute("aria-label", "Drop server here");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", sid);
      e.dataTransfer.setDragImage(source, e.clientX - rect.left, e.clientY - rect.top);
      source.classList.add("dragging");
      source.setAttribute("aria-grabbed", "true");
      const drag = serverDrag = { sid, kind, source, shadow, container: source.parentNode };
      // Hiding the source inside dragstart aborts the browser's native drag.
      // Wait until it has captured the drag image and started the session.
      drag.timer = setTimeout(() => {
        if (serverDrag !== drag) return;
        drag.container.insertBefore(shadow, source);
        source.style.display = "none";
      }, 0);
    }

    function moveServerDrag(e) {
      const drag = serverDrag;
      if (!drag || e.currentTarget !== drag.container) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      // The container accepts drops on the placeholder and in the gaps too.
      // Keep the slot still while the pointer is inside it.
      if (!drag.shadow.isConnected || drag.shadow.contains(e.target)) return;
      const items = Array.from(drag.container.children).filter(child => child.dataset.serverId && child !== drag.source);
      const horizontal = drag.kind === "tab" || getComputedStyle(drag.container).gridTemplateColumns.split(" ").length > 1;
      const next = items.find(item => {
        const rect = item.getBoundingClientRect();
        return horizontal
          ? e.clientY < rect.top || (e.clientY <= rect.bottom && e.clientX < rect.left + rect.width / 2)
          : e.clientY < rect.top + rect.height / 2;
      });
      const end = drag.kind === "tab" ? drag.container.querySelector(".server-manage-button") : null;
      drag.container.insertBefore(drag.shadow, next || end);
    }

    function endServerDrag() {
      if (!serverDrag) return;
      const { source, shadow, timer } = serverDrag;
      clearTimeout(timer);
      source.classList.remove("dragging");
      source.style.display = "";
      source.removeAttribute("aria-grabbed");
      shadow.remove();
      serverDrag = null;
      renderTabs();
      renderServerManagement();
    }

    async function dropServerDrag(e) {
      const drag = serverDrag;
      if (!drag || e.currentTarget !== drag.container || !drag.shadow.isConnected) return;
      e.preventDefault();
      const ids = Array.from(drag.container.children)
        .filter(child => child === drag.shadow || (child.dataset.serverId && child !== drag.source))
        .map(child => child === drag.shadow ? drag.sid : child.dataset.serverId);
      const previous = allServers.slice();
      if (ids.every((id, index) => id === previous[index].id)) { endServerDrag(); return; }
      serverOrderSaving = true;
      allServers.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
      endServerDrag();
      try {
        const res = await fetch("/api/servers/reorder", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ order: ids }) });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.message || "Server reorder failed");
        allServers = data.servers;
      } catch (err) {
        allServers = previous;
        console.error(err);
        showToast("Couldn't save server order. Please try again.");
      } finally {
        serverOrderSaving = false;
        renderTabs();
        renderServerManagement();
      }
    }

    function selectServer(sid) {
      if (currentServerId === sid) return;
      currentServerId = sid;
      localStorage.setItem("devboost-active-server", sid);
      document.getElementById("remote-services-body").innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">Click "Scan Ports" to detect running services on the remote server.</td></tr>';
      document.getElementById("stat-remote").innerText = "-";
      document.getElementById("docker-body").innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:30px;">Checking Docker on the active server...</td></tr>';
      document.getElementById("docker-subtitle").innerText = "";
      document.getElementById("syncs-body").innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:30px;">Loading folder syncs...</td></tr>';
      renderTabs();
      updateUsageTitle();
      fetchStatus();
      fetchDocker();
      fetchSyncs();
      if (usageTab === "remote") fetchUsage();
    }

    async function removeServerTab(sid) {
      const srv = allServers.find(s => s.id === sid);
      if (!srv) return;
      if (allServers.length <= 1) { alert("Cannot remove the last server tab."); return; }
      if (!confirm(`Remove tab "${srv.name || srv.ssh_host}"? Its tunnels and persistent agents will be stopped.`)) return;
      try {
        const res = await fetch("/api/servers/remove", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid }) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Failed to remove server"); return; }
        if (currentServerId === sid) { currentServerId = ""; localStorage.removeItem("devboost-active-server"); }
        showToast(d.message || "Server removed");
        await fetchServers();
        fetchStatus();
        fetchSyncs();
      } catch (err) { alert("Failed to remove server: " + err); }
    }

    function openServerModal() {
      editingServerId = null;
      document.getElementById("server-modal-title").textContent = "Add SSH Connection Tab";
      document.getElementById("server-submit-btn").textContent = "Add Manually";
      document.getElementById("server-search").value = "";
      document.getElementById("manual-ssh-host").value = "";
      document.getElementById("manual-ssh-host").readOnly = false;
      document.getElementById("manual-server-name").value = "";
      document.getElementById("manual-server-ip").value = "";
      document.getElementById("manual-server-ip").readOnly = false;
      document.getElementById("server-modal").style.display = "flex";
      loadSshHosts();
    }
    function closeServerModal() { editingServerId = null; document.getElementById("server-modal").style.display = "none"; }

    function editServer(sid) {
      const server = allServers.find(item => item.id === sid);
      if (!server) return;
      editingServerId = sid;
      document.getElementById("server-modal-title").textContent = "Edit SSH Connection";
      document.getElementById("server-submit-btn").textContent = "Save Changes";
      document.getElementById("server-search").value = "";
      document.getElementById("manual-ssh-host").value = server.ssh_host || "";
      document.getElementById("manual-ssh-host").readOnly = true;
      document.getElementById("manual-server-name").value = server.name || "";
      document.getElementById("manual-server-ip").value = server.ip || "";
      document.getElementById("manual-server-ip").readOnly = false;
      document.getElementById("ssh-host-list").innerHTML = "";
      document.getElementById("server-modal").style.display = "flex";
    }

    async function loadSshHosts() {
      const list = document.getElementById("ssh-host-list");
      list.innerHTML = '<div style="color:var(--text-muted); font-size:13px;">Loading SSH hosts from ~/.ssh/config...</div>';
      try {
        const res = await fetch("/api/ssh-hosts");
        const data = await res.json();
        sshHostCache = data.hosts || [];
        renderSshHostList();
      } catch (err) {
        list.innerHTML = `<div style="color:var(--danger); font-size:13px;">Failed to load ~/.ssh/config: ${err}</div>`;
      }
    }

    function renderSshHostList() {
      const list = document.getElementById("ssh-host-list");
      const q = (document.getElementById("server-search").value || "").toLowerCase();
      const items = (sshHostCache || []).filter(h => !q || h.ssh_host.toLowerCase().includes(q) || (h.hostname || "").toLowerCase().includes(q));
      if (!items.length) {
        list.innerHTML = '<div style="color:var(--text-muted); font-size:13px;">No matching SSH hosts. Add manually below.</div>';
        return;
      }
      filteredSshHosts = items;
      list.innerHTML = items.map((h, i) => {
        const sub = [h.user ? h.user + "@" : "", h.hostname || "", h.port && h.port !== 22 ? ":" + h.port : ""].join("");
        return `<div class="server-row">
          <div class="srv-main">
            <div class="srv-host">${escapeHtml(h.ssh_host)}</div>
            <div class="srv-sub">${escapeHtml(sub || "ssh-config entry")}</div>
          </div>
          ${h.added
            ? `<span class="badge badge-active">Added</span>`
            : `<button class="btn btn-sm btn-primary" onclick="addServerFromSshByIndex(${i})">+ Add Tab</button>`}
        </div>`;
      }).join("");
    }

    let filteredSshHosts = [];

    async function addServerFromSshByIndex(i) {
      const h = (filteredSshHosts || [])[i] || {};
      const sshHost = h.ssh_host;
      if (!sshHost) return;
      showToast(`Adding ${sshHost}...`);
      try {
        const res = await fetch("/api/servers", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ ssh_host: sshHost, name: sshHost, ip: h.hostname || "" }) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Failed to add server"); return; }
        closeServerModal();
        showToast(`Tab "${sshHost}" added`);
        await fetchServers();
        selectServer(d.server.id);
      } catch (err) { alert("Failed to add server: " + err); }
    }

    async function submitManualServer() {
      const sshHost = document.getElementById("manual-ssh-host").value.trim();
      const name = document.getElementById("manual-server-name").value.trim();
      const ip = document.getElementById("manual-server-ip").value.trim();
      if (!sshHost) { alert("Enter an SSH Host alias."); return; }
      try {
        const editing = !!editingServerId;
        const url = editing ? "/api/servers/update" : "/api/servers";
        const payload = editing ? { id: editingServerId, name: name, ip: ip } : { ssh_host: sshHost, name: name || sshHost, ip: ip };
        const res = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || (editing ? "Failed to update server" : "Failed to add server")); return; }
        closeServerModal();
        await fetchServers();
        if (!editing) selectServer(d.server.id);
        else showToast("Server settings saved");
      } catch (err) { alert("Failed to add server: " + err); }
    }

    async function fetchStatus() {
      if (serverDrag || serverOrderSaving) return;
      const requestedServerId = currentServerId;
      const orderVersion = serverOrderVersion;
      try {
        const res = await fetch("/api/status" + serverQuery());
        const data = await res.json();
        // Responses can arrive after the user has selected another tab.
        // Never let an old response repaint state for the newly selected tab.
        if (!responseBelongsToServer(requestedServerId, currentServerId, data.server_id)) return;
        if (data.servers && !serverDrag && !serverOrderSaving && orderVersion === serverOrderVersion) {
          allServers = data.servers;
          if (!currentServerId && allServers[0]) {
            currentServerId = allServers[0].id;
            localStorage.setItem("devboost-active-server", currentServerId);
          }
          renderTabs();
        }
        if (data.server_id) { serverReachability[data.server_id] = !!data.server_reachable; }
        renderStatus(data);
      } catch (err) {
        console.error(err);
      }
    }

    function renderStatus(data) {
      const dot = document.getElementById("server-status-dot");
      const txt = document.getElementById("server-status-text");
      if (data.server_reachable) {
        dot.className = "status-dot online";
        txt.innerText = "Connected";
      } else {
        dot.className = "status-dot offline";
        txt.innerText = "Unreachable / Offline";
      }

      if (data.server_name) {
        document.getElementById("header-server-name").innerText = data.server_name;
        if (servicesTab === "local") {
          document.getElementById("services-section-title").innerText = "Listening Ports on This Mac";
        } else {
          document.getElementById("services-section-title").innerText = `Discovered Services on ${data.server_name}`;
        }
        document.title = `DevBoost • ${data.server_name} • Port Forward Manager`;
      }
      if (data.server_host) {
        currentServerHost = data.server_host;
        const ipPart = data.server_ip ? ` (${data.server_ip})` : "";
        document.getElementById("header-server-host").innerText = `Host: ${data.server_host}${ipPart}`;
      }

      currentForwards = data.forwards;
      renderHomeForwardsSummary(currentForwards);
      cachedHistory = data.history || [];
      const activeCount = data.forwards.filter(f => f.active).length;
      const alwaysCount = data.forwards.filter(f => f.always).length;
      document.getElementById("stat-active").innerText = activeCount;
      document.getElementById("stat-always").innerText = alwaysCount;

      const orphanBtn = document.getElementById("clean-orphans-btn");
      const orphanBadge = document.getElementById("orphans-badge");
      if (data.orphaned_count > 0) {
        orphanBadge.innerText = `(${data.orphaned_count} found)`;
        orphanBtn.style.borderColor = "var(--warning)";
      } else {
        orphanBadge.innerText = "";
        orphanBtn.style.borderColor = "var(--border)";
      }

      const tbody = document.getElementById("forwards-body");
      if (data.forwards.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:30px;">No port forwards configured. Click "+ Add Port Forward" above.</td></tr>';
        return;
      }

      tbody.innerHTML = data.forwards.map(f => {
        const url = `http://localhost:${f.local_port}`;
        const conflictOwner = (f.conflict_with && (f.conflict_with.name || f.conflict_with.ssh_host)) || "";
        const conflictBadge = f.conflict ? `<span class="badge badge-inactive" title="${conflictOwner ? `Local port ${f.local_port} is already held by ${conflictOwner}` : "Another server tab is already listening on this local port"}">⚠️ ${conflictOwner ? `Port in use by ${escapeHtml(conflictOwner)}` : "Port in use by another tab"}</span>` : "";
        const activeBadge = f.active
          ? `<span class="badge badge-active">🟢 Active ${f.pid ? '(PID ' + f.pid + ')' : ''}</span>`
          : `<span class="badge badge-inactive">🔴 Stopped</span>`;
        const modeBadge = f.always
          ? `<span class="badge badge-always" title="Managed by LaunchAgent">📌 Always</span>`
          : `<span class="badge badge-session" title="Temporary SSH tunnel">⚡ Session</span>`;

        return `
          <tr>
            <td>
              <a href="${url}" target="_blank" class="port-link">
                :${f.local_port} ↗
              </a>
            </td>
            <td style="font-family:var(--font-mono); color:var(--text-muted);">
              :${f.remote_port}
            </td>
            <td>
              <strong>${escapeHtml(f.label)}</strong><br/>${conflictBadge}
            </td>
            <td>${modeBadge}</td>
            <td>${activeBadge}</td>
            <td>
              <div class="actions-cell">
                <button class="btn btn-sm" onclick="toggleAlways(${f.local_port}, ${!f.always})" title="${f.always ? 'Change to temporary session' : 'Make persistent (Always Forward)'}">
                  ${f.always ? 'Make Session' : 'Make Always'}
                </button>
                <button class="btn btn-sm btn-danger" onclick="deleteForward(${f.local_port})" title="Remove forward">
                  Delete
                </button>
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function submitAddForward() {
      const localPort = parseInt(document.getElementById("modal-local-port").value, 10);
      let remotePort = parseInt(document.getElementById("modal-remote-port").value, 10);
      if (isNaN(remotePort)) remotePort = localPort;
      const label = document.getElementById("modal-label").value.trim();
      const always = document.getElementById("modal-always").checked;

      if (isNaN(localPort) || localPort <= 0) {
        alert("Please specify a valid port number.");
        return;
      }

      closeAddModal();
      showToast(`Setting up forward for port ${localPort}...`);
      try {
        const res = await fetch("/api/forward", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort, remote_port: remotePort, label: label, always: always })
        });
        const d = await res.json();
        showToast(d.message || "Forward created!");
        fetchStatus();
      } catch (err) {
        alert("Failed to add forward: " + err);
      }
    }

    async function toggleAlways(localPort, makeAlways) {
      showToast(`Updating persistence rule for port ${localPort}...`);
      try {
        await fetch("/api/toggle", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort, always: makeAlways })
        });
        fetchStatus();
      } catch (err) {
        alert("Error toggling persistence: " + err);
      }
    }

    async function deleteForward(localPort) {
      if (!confirm(`Are you sure you want to remove forward for port ${localPort}?`)) return;
      showToast(`Removing forward for port ${localPort}...`);
      try {
        await fetch("/api/remove", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort })
        });
        fetchStatus();
      } catch (err) {
        alert("Error removing forward: " + err);
      }
    }

    async function cleanOrphans() {
      showToast("Cleaning up duplicate/hung SSH processes...");
      try {
        const res = await fetch("/api/clean", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ server_id: currentServerId }) });
        const d = await res.json();
        showToast(`Cleaned up ${d.killed} orphaned process(es).`);
        fetchStatus();
      } catch (err) {
        alert("Error cleaning orphans: " + err);
      }
    }

    async function scanRemoteServices() {
      const requestedServerId = currentServerId;
      const tbody = document.getElementById("remote-services-body");
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">Scanning remote ports on ${currentServerHost || 'remote server'}...</td></tr>`;
      try {
        const res = await fetch("/api/scan" + serverQuery());
        const data = await res.json();
        if (!responseBelongsToServer(requestedServerId, currentServerId)) return;
        document.getElementById("stat-remote").innerText = data.services.length;
        if (data.services.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">No listening ports detected or server unreachable.</td></tr>';
          return;
        }

        tbody.innerHTML = data.services.map(s => {
          const isForwarded = currentForwards.some(f => f.remote_port === s.port);
          return `
            <tr>
              <td style="font-family:var(--font-mono); font-weight:600; color:#fff;">:${s.port}</td>
              <td style="font-family:var(--font-mono); color:var(--text-muted);">${s.process}</td>
              <td><strong>${s.label}</strong></td>
              <td>
                ${isForwarded 
                  ? '<span class="badge badge-active">Forwarded</span>' 
                  : '<span class="badge" style="background:rgba(255,255,255,0.05); color:var(--text-muted);">Not forwarded</span>'}
              </td>
              <td style="text-align:right;">
                ${isForwarded
                  ? `<a href="http://localhost:${s.port}" target="_blank" class="btn btn-sm">Open ↗</a>`
                  : `<button class="btn btn-sm btn-primary" onclick='openAddModal(${s.port}, ${s.port}, ${JSON.stringify(s.label || "")})'>+ Forward</button>`}
              </td>
            </tr>
          `;
        }).join("");
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:var(--danger); padding:20px;">Scan failed: ${err}</td></tr>`;
      }
    }

    let dockerRows = [];
    let dockerSorts = [{key: "name", dir: 1}];
    let dockerLogTimer = null;
    let dockerLogContainer = "";
    let dockerLogProfileKey = "";
    let dockerLogResizeObserver = null;
    const DOCKER_LOG_DEFAULTS = {fontSize: 12, width: 1000, height: 720, search: ""};

    function dockerLogStorageKey() {
      return `devboost-docker-log:${currentServerId || currentServerHost || "default"}:${dockerLogContainer}`;
    }
    function getDockerLogProfile() {
      try { return {...DOCKER_LOG_DEFAULTS, ...(JSON.parse(localStorage.getItem(dockerLogProfileKey) || "{}"))}; }
      catch (_) { return {...DOCKER_LOG_DEFAULTS}; }
    }
    function saveDockerLogProfile(changes) {
      try { localStorage.setItem(dockerLogProfileKey, JSON.stringify({...getDockerLogProfile(), ...changes})); } catch (_) {}
    }
    function applyDockerLogProfile() {
      const profile = getDockerLogProfile();
      const windowEl = document.getElementById("docker-log-window");
      const search = document.getElementById("docker-log-search");
      windowEl.style.width = `${profile.width}px`; windowEl.style.height = `${profile.height}px`;
      document.getElementById("docker-log-output").style.fontSize = `${profile.fontSize}px`;
      search.value = profile.search;
    }
    function renderDockerLog() {
      const output = document.getElementById("docker-log-output");
      const raw = output.dataset.content || "";
      const query = document.getElementById("docker-log-search").value;
      saveDockerLogProfile({search: query});
      const lines = raw ? raw.split("\\n") : [];
      const matching = query ? lines.filter(line => line.toLowerCase().includes(query.toLowerCase())) : lines;
      const needle = query ? new RegExp(query.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&"), "ig") : null;
      const rendered = matching.map(line => escapeHtml(line).replace(needle, match => `<mark>${match}</mark>`)).join("\\n");
      output.innerHTML = rendered || (query ? "(no matching log lines)" : "(no logs yet — waiting for the container)");
      document.getElementById("docker-log-count").innerText = query ? `${matching.length}/${lines.length} lines` : `${lines.length} lines`;
    }
    function changeDockerLogFont(delta) {
      const profile = getDockerLogProfile();
      const fontSize = Math.max(9, Math.min(24, profile.fontSize + delta));
      saveDockerLogProfile({fontSize});
      document.getElementById("docker-log-output").style.fontSize = `${fontSize}px`;
    }
    function resetDockerLogView() {
      saveDockerLogProfile(DOCKER_LOG_DEFAULTS);
      applyDockerLogProfile(); renderDockerLog();
    }

    function numericDockerValue(value) {
      const m = String(value || "").replace(/,/g, "").match(/-?[0-9]+(?:\\.[0-9]+)?/);
      return m ? Number(m[0]) : -Infinity;
    }
    function dockerSortValue(row, key) {
      const s = row.stats || {};
      if (key === "name") return String(row.name || row.id || "").toLowerCase();
      if (key === "labels") return (row.labels || []).length;
      if (key === "status") return String(row.status || "").toLowerCase();
      if (key === "cpu") return numericDockerValue(s.cpu_percent);
      if (key === "memory") return numericDockerValue(s.memory_usage);
      if (key === "memory_percent") return numericDockerValue(s.memory_percent);
      if (key === "network") return numericDockerValue(s.network_io);
      return "";
    }
    function sortDocker(key, event) {
      const multi = event && event.shiftKey;
      const current = dockerSorts.find(s => s.key === key);
      if (!multi) dockerSorts = [{key, dir: current ? -current.dir : (key === "name" ? 1 : -1)}];
      else if (current) current.dir *= -1;
      else dockerSorts.push({key, dir: key === "name" ? 1 : -1});
      renderDockerRows();
    }
    function renderDockerSortIndicators() {
      ["name", "labels", "status", "cpu", "memory", "memory_percent", "network"].forEach(key => {
        const el = document.getElementById("sort-" + key);
        if (!el) return;
        const i = dockerSorts.findIndex(s => s.key === key);
        el.innerText = i < 0 ? "" : (dockerSorts[i].dir > 0 ? "↑" : "↓") + (dockerSorts.length > 1 ? (i + 1) : "");
      });
    }
    function renderDockerRows() {
      const tbody = document.getElementById("docker-body");
      const rows = dockerRows.slice().sort((a, b) => {
        for (const sort of dockerSorts) {
          const av = dockerSortValue(a, sort.key), bv = dockerSortValue(b, sort.key);
          if (av < bv) return -1 * sort.dir;
          if (av > bv) return 1 * sort.dir;
        }
        return dockerSortValue(a, "name").localeCompare(dockerSortValue(b, "name"));
      });
      tbody.innerHTML = rows.map(c => {
        const s = c.stats || {};
        const labels = (c.labels || []).map(l => `<span class="docker-label" style="background:${escapeHtml(l.color || '#8b949e')}" title="matches: ${escapeHtml(l.name || '')}">${escapeHtml(l.name || '')}</span>`).join("") || '<span class="muted">—</span>';
        return `<tr>
          <td><button class="btn btn-sm docker-logs-button" onclick='openDockerLog(${JSON.stringify(c.name || c.id)})' title="Watch logs">LOGS</button><strong>${escapeHtml(c.name || c.id)}</strong><div class="sync-sub">${escapeHtml(c.id || "")}</div></td>
          <td>${labels}</td><td>${escapeHtml(c.status || "")}</td>
          <td class="mono">${escapeHtml(s.cpu_percent || "-")}</td><td class="mono">${escapeHtml(s.memory_usage || "-")}</td>
          <td class="mono">${escapeHtml(s.memory_percent || "-")}</td><td class="mono">${escapeHtml(s.network_io || "-")}</td>
          <td class="mono" style="max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(c.image || "")}">${escapeHtml(c.image || "")}</td>
        </tr>`;
      }).join("");
      renderDockerSortIndicators();
    }
    async function fetchDocker() {
      const requestedServerId = currentServerId;
      const tbody = document.getElementById("docker-body");
      const subtitle = document.getElementById("docker-subtitle");
      try {
        const res = await fetch("/api/docker" + serverQuery());
        const data = await res.json();
        if (!responseBelongsToServer(requestedServerId, currentServerId)) return;
        if (data.available === null) {
          setHomeSummary("home-docker-summary", "Checking Docker…");
          subtitle.innerText = "checking...";
          return;
        }
        if (!data.available) {
          setHomeSummary("home-docker-summary", "Docker unavailable");
          subtitle.innerText = "unavailable";
          tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--warning); padding:30px;">${escapeHtml(data.message || "Docker is unavailable on this server.")}</td></tr>`;
          return;
        }
        const age = data.age_seconds == null ? "just now" : `${data.age_seconds}s ago`;
        subtitle.innerText = `${data.containers.length} running • updated ${age}`;
        if (!data.containers.length) {
          tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:30px;">Docker is available, but no containers are running.</td></tr>';
          return;
        }
        dockerRows = data.containers || [];
        renderHomeDockerSummary(dockerRows);
        renderDockerRows();
      } catch (err) {
        setHomeSummary("home-docker-summary", "Docker unavailable");
        subtitle.innerText = "error";
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--danger); padding:30px;">Docker query failed: ${escapeHtml(String(err))}</td></tr>`;
      }
    }

    function dockerLabelRow(label = {}) {
      const id = escapeHtml(label.id || "");
      return `<div class="docker-label-edit" data-id="${id}" style="display:grid; grid-template-columns:1.1fr 1.3fr 72px 28px 28px; gap:8px; align-items:center; margin-bottom:8px;">
        <input type="text" value="${escapeHtml(label.name || "")}" placeholder="Label name" data-field="name" />
        <input type="text" value="${escapeHtml(label.match || "")}" placeholder="name contains..." data-field="match" />
        <input type="color" value="${/^#[0-9a-fA-F]{6}$/.test(label.color || '') ? label.color : '#8b949e'}" data-field="color" title="Label color" />
        <input type="checkbox" ${label.enabled === false ? "" : "checked"} data-field="enabled" title="Enabled" />
        <button class="btn btn-sm btn-danger" onclick="this.parentElement.remove()" title="Remove">✕</button></div>`;
    }
    function addDockerLabelRow() { document.getElementById("docker-label-list").insertAdjacentHTML("beforeend", dockerLabelRow()); }
    async function openDockerLabelsModal() {
      const res = await fetch("/api/docker/labels"); const data = await res.json();
      document.getElementById("docker-label-list").innerHTML = (data.labels || []).map(dockerLabelRow).join("");
      document.getElementById("docker-labels-modal").style.display = "flex";
    }
    function closeDockerLabelsModal() { document.getElementById("docker-labels-modal").style.display = "none"; }
    async function saveDockerLabels() {
      const labels = [...document.querySelectorAll(".docker-label-edit")].map(row => {
        const get = field => row.querySelector(`[data-field="${field}"]`);
        return {id: row.dataset.id, name: get("name").value, match: get("match").value, color: get("color").value, enabled: get("enabled").checked};
      });
      const res = await fetch("/api/docker/labels", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({labels})});
      const data = await res.json(); if (!data.ok) { alert(data.message || "Could not save labels"); return; }
      closeDockerLabelsModal(); fetchDocker(); showToast("Docker labels saved");
    }
    async function openDockerLog(container) {
      closeDockerLog(); dockerLogContainer = container; dockerLogProfileKey = dockerLogStorageKey();
      document.getElementById("docker-log-title").innerText = `Logs • ${container}`;
      const output = document.getElementById("docker-log-output"); output.dataset.content = ""; output.innerText = "Connecting...";
      applyDockerLogProfile(); document.getElementById("docker-log-modal").style.display = "flex";
      if (dockerLogResizeObserver) dockerLogResizeObserver.disconnect();
      dockerLogResizeObserver = new ResizeObserver(() => {
        const windowEl = document.getElementById("docker-log-window");
        saveDockerLogProfile({width: Math.round(windowEl.getBoundingClientRect().width), height: Math.round(windowEl.getBoundingClientRect().height)});
      });
      dockerLogResizeObserver.observe(document.getElementById("docker-log-window"));
      await refreshDockerLog(); dockerLogTimer = setInterval(refreshDockerLog, 2000);
    }
    function closeDockerLog() { if (dockerLogTimer) clearInterval(dockerLogTimer); dockerLogTimer = null; if (dockerLogResizeObserver) dockerLogResizeObserver.disconnect(); document.getElementById("docker-log-modal").style.display = "none"; }
    async function refreshDockerLog() {
      if (!dockerLogContainer) return;
      try {
        const query = serverQuery();
        const res = await fetch("/api/docker/logs" + (query ? query + "&" : "?") + "container=" + encodeURIComponent(dockerLogContainer) + "&tail=300");
        const data = await res.json(); const output = document.getElementById("docker-log-output");
        if (!data.ok) { output.innerText = data.message || "Logs unavailable"; return; }
        const previous = output.dataset.content || ""; const next = data.logs || "";
        output.dataset.content = previous && next && next.length < previous.length && !next.includes(previous) ? `[container restarted or rebuilt]\n${next}` : next;
        renderDockerLog();
        if (!document.getElementById("docker-log-search").value) output.scrollTop = output.scrollHeight;
      } catch (err) { document.getElementById("docker-log-output").innerText = String(err); }
    }

    let servicesTab = "remote";
    let currentLocalPorts = [];

    function switchServicesTab(which) {
      servicesTab = which;
      document.getElementById("tab-remote").classList.toggle("active", which === "remote");
      document.getElementById("tab-local").classList.toggle("active", which === "local");
      document.getElementById("remote-services-wrap").style.display = which === "remote" ? "block" : "none";
      document.getElementById("local-services-wrap").style.display = which === "local" ? "block" : "none";
      document.getElementById("scan-remote-btn").style.display = which === "remote" ? "" : "none";
      document.getElementById("refresh-local-btn").style.display = which === "local" ? "" : "none";
      const title = document.getElementById("services-section-title");
      if (which === "local") {
        title.innerText = "Listening Ports on This Mac";
        fetchLocalPorts();
      } else {
        const srv = activeServer();
        title.innerText = `Discovered Services on ${(srv && (srv.name || srv.ssh_host)) || "Remote Server"}`;
      }
    }

    async function fetchLocalPorts() {
      if (servicesTab !== "local") return;
      const tbody = document.getElementById("local-services-body");
      try {
        const res = await fetch("/api/local-ports");
        const data = await res.json();
        currentLocalPorts = data.ports || [];
        renderLocalPorts();
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--danger); padding:20px;">Failed to list local ports: ${escapeHtml(String(err))}</td></tr>`;
      }
    }

    function renderLocalPorts() {
      const tbody = document.getElementById("local-services-body");
      if (!currentLocalPorts.length) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-muted); padding:20px;">No listening ports on this Mac.</td></tr>';
        return;
      }
      tbody.innerHTML = currentLocalPorts.map(p => {
        const url = `http://localhost:${p.port}`;
        const srcBadge = p.devboost
          ? `<span class="badge badge-always" title="Held by one of your DevBoost SSH tunnels">🔗 DevBoost tunnel</span>`
          : `<span class="badge" style="background:rgba(255,255,255,0.05); color:var(--text-muted);">Other app</span>`;
        return `
          <tr>
            <td><a href="${url}" target="_blank" class="port-link">:${p.port} ↗</a></td>
            <td><strong class="mono">${escapeHtml(p.cmd || "?")}</strong><br/><span class="muted" style="font-size:11px;">PID ${p.pid}</span></td>
            <td>${srcBadge}</td>
            <td>
              <div class="actions-cell">
                <button class="btn btn-sm btn-danger" onclick="killLocalPort(${p.pid})" title="Stop the process holding this port">
                  Kill
                </button>
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function killLocalPort(pid) {
      const entry = (currentLocalPorts || []).find(p => p.pid === pid) || {};
      const port = entry.port != null ? entry.port : "?";
      const cmd = entry.cmd || "process";
      const extra = entry.devboost ? "\\n\\nNote: this is a DevBoost tunnel — an Always-managed one will restart automatically." : "";
      if (!confirm(`Kill ${cmd} (PID ${pid}) listening on :${port}?\\nThe port will be freed immediately.${extra}`)) return;
      showToast(`Stopping PID ${pid}...`);
      try {
        const res = await fetch("/api/local-ports/kill", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ pid: pid })
        });
        const d = await res.json();
        showToast(d.message || (d.ok ? "Process stopped" : "Could not stop process"));
        fetchLocalPorts();
        fetchStatus();
      } catch (err) {
        alert("Failed to kill process: " + err);
      }
    }

    async function fetchSyncs() {
      const requestedServerId = currentServerId;
      try {
        const res = await fetch("/api/syncs" + serverQuery());
        const data = await res.json();
        if (!responseBelongsToServer(requestedServerId, currentServerId, data.server_id)) return;
        cachedFolderHistory = data.folder_history || [];
        renderSyncs(data);
      } catch (err) {
        console.error(err);
      }
    }

    function formatSyncAge(ts) {
      if (!ts) return "never";
      const delta = (Date.now() / 1000) - ts;
      if (delta < 60) return Math.max(0, Math.floor(delta)) + "s ago";
      if (delta < 3600) return Math.floor(delta / 60) + "m ago";
      if (delta < 86400) return Math.floor(delta / 3600) + "h ago";
      return new Date(ts * 1000).toLocaleString();
    }

    function directionBadge(direction, mirror) {
      const icons = { "two-way": "⇄ Two-way", "push": "⬆ Push", "pull": "⬇ Pull" };
      const label = icons[direction] || escapeHtml(direction || "two-way");
      const mirrorTag = (mirror && direction !== "two-way") ? " +mirror" : "";
      return `<span class="badge badge-session" title="${direction === 'two-way' ? 'Bidirectional merge, newer wins, deletions never propagate' : (mirror ? 'Exact copy — deletions propagate (rsync --delete)' : 'One-way, extra files kept')}">${label}${mirrorTag}</span>`;
    }

    function modeBadge(s) {
      return s.always
        ? `<span class="badge badge-always" title="Persistent LaunchAgent: instant local triggers + polling every ${s.interval || 15}s">📌 Auto</span>`
        : `<span class="badge badge-session" title="One-time sync: runs now + on-demand">⚡ Once</span>`;
    }

    function formatSyncTime(ts) {
      if (!ts) return "—";
      try {
        return new Date(ts * 1000).toLocaleString();
      } catch (err) {
        return "—";
      }
    }

    function syncStatusBadge(s) {
      if (s.last_status === "ok") return `<span class="badge badge-active" title="Last synced: ${escapeHtml(formatSyncTime(s.last_sync))}">✓ ${escapeHtml(formatSyncAge(s.last_sync))}</span>`;
      if (s.last_status === "error") return `<span class="badge badge-inactive" title="${escapeHtml(s.last_message || 'Sync failed')}">✕ failed</span>`;
      return `<span class="badge" style="background:rgba(255,255,255,0.05); color:var(--text-muted);">never synced</span>`;
    }

    function confirmMirrorSync(direction, localPath, remotePath) {
      if (!((direction === "push" || direction === "pull") && arguments.length >= 3)) return true;
      const source = direction === "push" ? localPath : remotePath;
      const destination = direction === "push" ? remotePath : localPath;
      return confirm(`Mirror sync will delete files in the destination folder that are not in the source.\n\nSource: ${source}\nDestination: ${destination}\n\nContinue with rsync --delete?`);
    }

    function renderSyncs(data) {
      currentSyncs = data.syncs || [];
      renderHomeSyncSummary(currentSyncs);
      const autoCount = currentSyncs.filter(s => s.always).length;
      document.getElementById("stat-syncs").innerText = currentSyncs.length ? `${currentSyncs.length} (${autoCount} auto)` : "0";
      const sub = document.getElementById("syncs-subtitle");
      if (sub) sub.innerText = data.server_name ? `• ${data.server_name}` : "";
      const tbody = document.getElementById("syncs-body");
      if (!currentSyncs.length) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:30px;">No folder syncs yet. Click "+ Add Folder Sync" to mirror a folder with this server.</td></tr>';
        return;
      }
      tbody.innerHTML = currentSyncs.map(s => {
        const msg = s.last_message ? `<br/><span class="muted" style="font-size:11px;">${escapeHtml((s.last_message || '').slice(0, 120))}</span>` : "";
        const protectedWarn = s.local_protected && s.last_status !== "ok"
          ? `<br/><span style="font-size:11px; color:var(--warning);">⚠️ ${data.packaged_app ? 'Allow DevBoost to access this protected folder in macOS Privacy & Security before background sync can write here.' : 'Background runs from a source checkout cannot access this folder — run the packaged DevBoost app.'}</span>`
          : "";
        const lastSyncLine = (s.last_status === "ok" && s.last_sync)
          ? `<br/><span class="muted mono" style="font-size:11px;" title="Exact time of the last successful sync">Last synced: ${escapeHtml(formatSyncTime(s.last_sync))}</span>`
          : "";
        return `
          <tr>
            <td><span class="sync-path">${escapeHtml(s.local_path)}</span><br/><span class="muted" style="font-size:11px;">this Mac</span></td>
            <td><span class="sync-path">${escapeHtml(s.remote_path)}</span><br/><span class="muted" style="font-size:11px;">${escapeHtml(data.server_host || currentServerHost || 'server')}</span></td>
            <td>${directionBadge(s.direction, s.mirror)}<br/><span style="display:inline-block; margin-top:4px;">${modeBadge(s)}</span></td>
            <td>${syncStatusBadge(s)}${lastSyncLine}${protectedWarn}${msg}</td>
            <td>
              <div class="actions-cell">
                <button class="btn btn-sm" onclick="openSyncModal('${s.id}')" title="Edit paths and options">Edit</button>
                <button class="btn btn-sm" onclick="runSyncNow('${s.id}')" title="Run this sync immediately">Sync Now</button>
                <button class="btn btn-sm" onclick="toggleSyncAlways('${s.id}', ${!s.always})" title="${s.always ? 'Switch to one-time (remove background agent)' : 'Keep in sync continuously (persistent agent)'}">
                  ${s.always ? 'Make Once' : 'Make Auto'}
                </button>
                <button class="btn btn-sm btn-danger" onclick="deleteSync('${s.id}')" title="Remove sync">Delete</button>
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function runSyncNow(sid) {
      const sync = (currentSyncs || []).find(x => x.id === sid);
      if (sync && sync.mirror && !confirmMirrorSync(sync.direction, sync.local_path, sync.remote_path)) return;
      showToast("Syncing folders...");
      try {
        const res = await fetch("/api/syncs/run", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid }) });
        const d = await res.json();
        showToast(d.message || (d.ok ? "Sync complete" : "Sync failed"));
        fetchSyncs();
      } catch (err) { alert("Sync failed: " + err); }
    }

    async function toggleSyncAlways(sid, makeAlways) {
      showToast(makeAlways ? "Enabling Auto sync..." : "Switching to one-time...");
      try {
        await fetch("/api/syncs/toggle", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid, always: makeAlways }) });
        fetchSyncs();
        fetchServers();
      } catch (err) { alert("Error toggling sync mode: " + err); }
    }

    async function deleteSync(sid) {
      const s = (currentSyncs || []).find(x => x.id === sid);
      const label = s ? `${s.local_path} <-> ${s.remote_path}` : sid;
      if (!confirm(`Remove folder sync "${label}"? Files are kept on both sides; only the tracking + background agent are removed.`)) return;
      showToast("Removing folder sync...");
      try {
        await fetch("/api/syncs/remove", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid }) });
        fetchSyncs();
        fetchServers();
      } catch (err) { alert("Error removing sync: " + err); }
    }

    function onSyncDirectionChange() {
      const dir = document.getElementById("sync-direction").value;
      const mirrorEl = document.getElementById("sync-mirror");
      const hint = document.getElementById("sync-direction-hint");
      if (dir === "two-way") {
        mirrorEl.checked = false;
        mirrorEl.disabled = true;
        document.getElementById("sync-mirror-group").style.opacity = "0.55";
        hint.innerText = "Two-way keeps both sides merged. Newer file wins. Deletions never propagate.";
      } else {
        mirrorEl.disabled = false;
        document.getElementById("sync-mirror-group").style.opacity = "1";
        hint.innerText = dir === "push"
          ? "Push: local is the source of truth, copied to the server."
          : "Pull: server is the source of truth, copied to this Mac.";
      }
    }

    function openSyncModal(syncId) {
      const existing = syncId ? (currentSyncs || []).find(x => x.id === syncId) : null;
      editingSyncId = existing ? existing.id : null;
      document.getElementById("sync-modal-title").innerText = existing ? "Edit Folder Sync" : "Add Folder Sync";
      document.getElementById("sync-submit-btn").innerText = existing ? "Save & Sync" : "Start Sync";
      document.getElementById("sync-local-path").value = existing ? (existing.local_path || "") : "";
      document.getElementById("sync-remote-path").value = existing ? (existing.remote_path || "") : "";
      document.getElementById("sync-direction").value = existing ? (existing.direction || "two-way") : "two-way";
      document.getElementById("sync-mirror").checked = existing ? !!existing.mirror : false;
      document.getElementById("sync-always").checked = existing ? !!existing.always : true;
      document.getElementById("sync-interval").value = existing ? (existing.interval || 15) : 15;
      document.getElementById("sync-interval-group").style.display = document.getElementById("sync-always").checked ? "block" : "none";
      document.getElementById("sync-remote-label").innerText = "Remote folder (on " + (currentServerHost || "server") + ")";
      onSyncDirectionChange();
      document.getElementById("browser-local").classList.remove("open");
      document.getElementById("browser-remote").classList.remove("open");
      hideSuggest("local");
      hideSuggest("remote");
      hideBrowserSuggest("local");
      hideBrowserSuggest("remote");
      renderFolderChips();
      document.getElementById("sync-modal").style.display = "flex";
      // Refresh recent pairs in background (per active tab)
      fetch("/api/folder-history" + serverQuery()).then(r => r.json()).then(d => {
        if (d.history) { cachedFolderHistory = d.history; renderFolderChips(); }
      }).catch(() => {});
      browseLocalGo(existing ? (existing.local_path || "~") : "~");
      browseRemoteGo(existing ? (existing.remote_path || "~") : "~");
    }

    function closeSyncModal() {
      hideBrowserSuggest("local");
      hideBrowserSuggest("remote");
      document.getElementById("sync-modal").style.display = "none";
      editingSyncId = null;
    }

    function renderFolderChips() {
      const section = document.getElementById("recent-folders-section");
      const container = document.getElementById("recent-folders-chips");
      const items = cachedFolderHistory || [];
      if (!items.length) { section.style.display = "none"; return; }
      section.style.display = "block";
      folderChipCache = items;
      container.innerHTML = items.map((h, i) => {
        const localShort = (h.local_path || "").split("/").slice(-2).join("/") || h.local_path;
        const remoteShort = (h.remote_path || "").split("/").slice(-2).join("/") || h.remote_path;
        return `<button class="recent-chip" onclick="quickSelectFolder(${i})" title="local: ${escapeHtml(h.local_path || '')}&#10;remote: ${escapeHtml(h.remote_path || '')}">${escapeHtml(localShort)} ⇄ ${escapeHtml(remoteShort)}</button>`;
      }).join("");
    }

    function quickSelectFolder(i) {
      const h = (folderChipCache || [])[i];
      if (!h) return;
      if (h.local_path) document.getElementById("sync-local-path").value = h.local_path;
      if (h.remote_path) document.getElementById("sync-remote-path").value = h.remote_path;
    }

    function toggleBrowser(which) {
      const el = document.getElementById(which === "local" ? "browser-local" : "browser-remote");
      el.classList.toggle("open");
      if (!el.classList.contains("open")) hideBrowserSuggest(which);
    }

    function pickBrowserPath(which) {
      if (which === "local" && browserLocalCur) {
        document.getElementById("sync-local-path").value = browserLocalCur;
        document.getElementById("browser-local").classList.remove("open");
      } else if (which === "remote" && browserRemoteCur) {
        document.getElementById("sync-remote-path").value = browserRemoteCur;
        document.getElementById("browser-remote").classList.remove("open");
      }
    }

    function browserPathEl(which) {
      return document.getElementById(which === "local" ? "browser-local-path" : "browser-remote-path");
    }

    function browserGo(which) {
      hideBrowserSuggest(which);
      const typed = (browserPathEl(which).value || "").trim();
      if (which === "local") browseLocalGo(typed || "~");
      else browseRemoteGo(typed || "~");
    }

    async function mkdirBrowser(which) {
      const cur = (which === "local" ? browserLocalCur : browserRemoteCur)
        || (browserPathEl(which).value || "").trim() || "~";
      const name = prompt(`New folder name (inside ${cur}):`, "");
      if (name === null) return;
      if (!name.trim()) return;
      showToast("Creating folder...");
      try {
        const body = which === "local"
          ? { which: "local", path: cur, name: name }
          : { which: "remote", server_id: currentServerId, path: cur, name: name };
        const res = await fetch("/api/browse/mkdir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Could not create folder"); return; }
        showToast(d.message || "Folder created");
        if (which === "local") {
          document.getElementById("sync-local-path").value = d.path;
          browseLocalGo(d.path);
        } else {
          document.getElementById("sync-remote-path").value = d.path;
          browseRemoteGo(d.path);
        }
      } catch (err) {
        alert("Could not create folder: " + err);
      }
    }

    function renderBrowserList(which, data) {
      const listEl = document.getElementById(which === "local" ? "browser-local-list" : "browser-remote-list");
      const pathEl = browserPathEl(which);
      if (!data || data.ok === false) {
        if (data && data.path) pathEl.value = data.path;
        if (data && data.denied) {
          listEl.innerHTML = `<div style="padding:12px; font-size:12px; line-height:1.6;">
            <div style="color:var(--warning); font-weight:600; margin-bottom:6px;">🔒 macOS blocked access to this folder</div>
            <div style="color:var(--text-muted);">The dashboard process isn't allowed to read<br/><span class="mono">${escapeHtml(data.path || '')}</span></div>
            <div style="margin-top:8px; color:var(--text);">Fix: System Settings → Privacy &amp; Security → <b>Full Disk Access</b> → add your terminal app (Terminal, iTerm, VS Code…), then restart the dashboard. If the dashboard runs in the background, add the Python that runs it instead.</div>
            <div style="margin-top:6px; color:var(--text-muted);">Tip: you can still type the full path above, but syncing the folder needs the same permission.</div>
          </div>`;
          return;
        }
        listEl.innerHTML = `<div style="padding:10px; font-size:12px; color:var(--danger);">${escapeHtml((data && data.message) || 'Cannot list folders')}</div>`;
        return;
      }
      pathEl.value = data.path || "";
      const entries = data.entries || [];
      browserCache[which] = entries;
      if (!entries.length) {
        listEl.innerHTML = `<div style="padding:10px; font-size:12px; color:var(--text-muted);">No subfolders here. <button class="btn btn-sm" onclick="pickBrowserPath('${which}')">Select this folder</button></div>`;
        return;
      }
      listEl.innerHTML = entries.map((e, i) =>
        `<button class="browser-item${e.hidden ? ' is-hidden' : ''}" onclick="browseCacheGo('${which}', ${i})" title="${escapeHtml(e.path)}"><span>📁</span><span style="flex:1; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(e.name)}</span><span style="color:var(--text-muted);">→</span></button>`
      ).join("");
    }

    let browserCache = { local: [], remote: [] };

    function browseCacheGo(which, i) {
      const e = (browserCache[which] || [])[i];
      if (!e) return;
      if (which === "local") browseLocalGo(e.path);
      else browseRemoteGo(e.path);
    }

    async function browseLocalGo(path) {
      let target = path;
      if (path === "..") target = browserLocalCur ? browserLocalCur + "/.." : "~";
      if (path === "~") target = "";
      const listEl = document.getElementById("browser-local-list");
      listEl.innerHTML = '<div style="padding:10px; font-size:12px; color:var(--text-muted);">Loading...</div>';
      try {
        const res = await fetch("/api/browse/local?path=" + encodeURIComponent(target || ""));
        const data = await res.json();
        if (data.ok) browserLocalCur = data.path;
        renderBrowserList("local", data);
      } catch (err) {
        listEl.innerHTML = `<div style="padding:10px; font-size:12px; color:var(--danger);">Browse failed: ${escapeHtml(String(err))}</div>`;
      }
    }

    async function browseRemoteGo(path) {
      let target = path;
      if (path === "..") {
        // Go up from current remote dir (posix-style, remote is ~-aware)
        const cur = browserRemoteCur || "~";
        target = cur === "/" ? "/" : cur.replace(/[/]+$/, "").split("/").slice(0, -1).join("/") || "/";
        if (cur === "~") target = "~";
      }
      if (path === "~") target = "~";
      const listEl = document.getElementById("browser-remote-list");
      listEl.innerHTML = '<div style="padding:10px; font-size:12px; color:var(--text-muted);">Loading remote folders...</div>';
      try {
        const res = await fetch("/api/browse/remote" + serverQuery() + (serverQuery() ? "&" : "?") + "path=" + encodeURIComponent(target || "~"));
        const data = await res.json();
        if (data.ok) browserRemoteCur = data.path;
        renderBrowserList("remote", data);
      } catch (err) {
        listEl.innerHTML = `<div style="padding:10px; font-size:12px; color:var(--danger);">Remote browse failed: ${escapeHtml(String(err))}</div>`;
      }
    }

    let browserSuggestState = { local: { active: -1, req: 0, timer: null }, remote: { active: -1, req: 0, timer: null } };
    let browserSuggestCache = { local: [], remote: [] };

    function browserSuggestListEl(which) {
      return document.getElementById(which === "local" ? "browser-suggest-local" : "browser-suggest-remote");
    }

    function onBrowserPathInput(which) {
      const st = browserSuggestState[which];
      clearTimeout(st.timer);
      hideBrowserSuggest(which);
      st.timer = setTimeout(() => fetchBrowserSuggest(which), 250);
    }

    function hideBrowserSuggest(which) {
      const st = browserSuggestState[which];
      clearTimeout(st.timer);
      st.timer = null;
      st.req++;
      st.active = -1;
      browserSuggestCache[which] = [];
      browserSuggestListEl(which).classList.remove("open");
    }

    async function fetchBrowserSuggest(which) {
      const input = browserPathEl(which);
      const listEl = browserSuggestListEl(which);
      const val = (input.value || "").trim();
      if (!val) { hideBrowserSuggest(which); return; }
      let dir, prefix;
      if (val.endsWith("/")) { dir = val; prefix = ""; }
      else {
        const idx = val.lastIndexOf("/");
        if (idx < 0) { dir = "~"; prefix = val; }
        else if (idx === 0) { dir = "/"; prefix = val.slice(1); }
        else { dir = val.slice(0, idx) || "/"; prefix = val.slice(idx + 1); }
      }
      const myReq = ++browserSuggestState[which].req;
      const url = which === "local"
        ? "/api/browse/local?path=" + encodeURIComponent(dir)
        : "/api/browse/remote" + serverQuery() + (serverQuery() ? "&" : "?") + "path=" + encodeURIComponent(dir);
      try {
        const res = await fetch(url);
        const data = await res.json();
        if (myReq !== browserSuggestState[which].req) return;
        if (!data || data.ok === false) { hideBrowserSuggest(which); return; }
        const pl = prefix.toLowerCase();
        const items = (data.entries || [])
          .filter(e => !prefix || e.name.toLowerCase().startsWith(pl))
          .slice(0, 8);
        if (!items.length) { hideBrowserSuggest(which); return; }
        browserSuggestCache[which] = items;
        browserSuggestState[which].active = -1;
        listEl.innerHTML = items.map((e, i) =>
          `<button class="suggest-item${e.hidden ? ' is-hidden' : ''}" onmousedown="event.preventDefault(); pickBrowserSuggest('${which}', ${i})" title="${escapeHtml(e.path)}"><span>📁</span><span style="flex:1; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(e.path)}</span></button>`
        ).join("");
        listEl.classList.add("open");
      } catch (err) {
        hideBrowserSuggest(which);
      }
    }

    function setActiveBrowserSuggest(which, i) {
      browserSuggestState[which].active = i;
      [...browserSuggestListEl(which).children].forEach((child, index) => {
        child.classList.toggle("active", index === i);
      });
    }

    function pickBrowserSuggest(which, i, keepSuggestions = false) {
      const e = (browserSuggestCache[which] || [])[i];
      if (!e) return;
      browserPathEl(which).value = e.path;
      setActiveBrowserSuggest(which, i);
      if (!keepSuggestions) browserGo(which);
    }

    function onBrowserSuggestKey(ev, which) {
      const listEl = browserSuggestListEl(which);
      const items = browserSuggestCache[which] || [];
      if (!listEl.classList.contains("open")) {
        if (ev.key === "Enter") { ev.preventDefault(); browserGo(which); }
        return;
      }
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!items.length) return;
        let a = browserSuggestState[which].active + (ev.key === "ArrowDown" ? 1 : -1);
        if (browserSuggestState[which].active < 0) a = ev.key === "ArrowDown" ? 0 : items.length - 1;
        a = (a + items.length) % items.length;
        setActiveBrowserSuggest(which, a);
      } else if (ev.key === "Tab") {
        if (!items.length) return;
        ev.preventDefault();
        const current = browserSuggestState[which].active;
        const step = ev.shiftKey ? -1 : 1;
        const a = current < 0
          ? (step > 0 ? 0 : items.length - 1)
          : (current + step + items.length) % items.length;
        pickBrowserSuggest(which, a, true);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        pickBrowserSuggest(which, browserSuggestState[which].active < 0 ? 0 : browserSuggestState[which].active);
      } else if (ev.key === "Escape") {
        hideBrowserSuggest(which);
      }
    }

    let suggestState = { local: { active: -1, req: 0, timer: null }, remote: { active: -1, req: 0, timer: null } };
    let suggestCache = { local: [], remote: [] };

    function suggestInputEl(which) {
      return document.getElementById(which === "local" ? "sync-local-path" : "sync-remote-path");
    }

    function suggestListEl(which) {
      return document.getElementById(which === "local" ? "suggest-local" : "suggest-remote");
    }

    function onSyncPathInput(which) {
      const st = suggestState[which];
      clearTimeout(st.timer);
      // Never leave results for a previous query visible while the new query
      // is being debounced. This also ensures Tab only cycles current matches.
      hideSuggest(which);
      st.timer = setTimeout(() => fetchSuggest(which), 250);
    }

    function hideSuggest(which) {
      const st = suggestState[which];
      clearTimeout(st.timer);
      st.timer = null;
      st.req++; // invalidate in-flight requests
      st.active = -1;
      suggestCache[which] = [];
      suggestListEl(which).classList.remove("open");
    }

    async function fetchSuggest(which) {
      const input = suggestInputEl(which);
      const listEl = suggestListEl(which);
      const val = (input.value || "").trim();
      if (!val) { hideSuggest(which); return; }
      // Split typed text into dir-to-list + name prefix to match.
      let dir, prefix;
      if (val.endsWith("/")) { dir = val; prefix = ""; }
      else {
        const idx = val.lastIndexOf("/");
        if (idx < 0) { dir = "~"; prefix = val; }
        else if (idx === 0) { dir = "/"; prefix = val.slice(1); }
        else { dir = val.slice(0, idx) || "/"; prefix = val.slice(idx + 1); }
      }
      const myReq = ++suggestState[which].req;
      const url = which === "local"
        ? "/api/browse/local?path=" + encodeURIComponent(dir)
        : "/api/browse/remote" + serverQuery() + (serverQuery() ? "&" : "?") + "path=" + encodeURIComponent(dir);
      try {
        const res = await fetch(url);
        const data = await res.json();
        if (myReq !== suggestState[which].req) return; // stale response
        if (!data || data.ok === false) { hideSuggest(which); return; }
        const pl = prefix.toLowerCase();
        const items = (data.entries || [])
          .filter(e => !prefix || e.name.toLowerCase().startsWith(pl))
          .slice(0, 8);
        if (!items.length) { hideSuggest(which); return; }
        suggestCache[which] = items;
        suggestState[which].active = -1;
        listEl.innerHTML = items.map((e, i) =>
          `<button class="suggest-item${e.hidden ? ' is-hidden' : ''}" onmousedown="event.preventDefault(); pickSuggest('${which}', ${i})" title="${escapeHtml(e.path)}"><span>📁</span><span style="flex:1; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(e.path)}</span></button>`
        ).join("");
        listEl.classList.add("open");
      } catch (err) {
        hideSuggest(which);
      }
    }

    function setActiveSuggest(which, i) {
      suggestState[which].active = i;
      [...suggestListEl(which).children].forEach((child, index) => {
        child.classList.toggle("active", index === i);
      });
    }

    function pickSuggest(which, i, keepSuggestions = false) {
      const e = (suggestCache[which] || [])[i];
      if (!e) return;
      suggestInputEl(which).value = e.path;
      setActiveSuggest(which, i);
      if (!keepSuggestions) {
        hideSuggest(which);
        if (which === "local") browseLocalGo(e.path);
        else browseRemoteGo(e.path);
      }
    }

    function onSuggestKey(ev, which) {
      const listEl = suggestListEl(which);
      if (!listEl.classList.contains("open")) return;
      const items = suggestCache[which] || [];
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!items.length) return;
        let a = suggestState[which].active + (ev.key === "ArrowDown" ? 1 : -1);
        if (suggestState[which].active < 0) a = ev.key === "ArrowDown" ? 0 : items.length - 1;
        a = (a + items.length) % items.length;
        setActiveSuggest(which, a);
      } else if (ev.key === "Tab") {
        if (!items.length) return;
        ev.preventDefault();
        const current = suggestState[which].active;
        const step = ev.shiftKey ? -1 : 1;
        const a = current < 0
          ? (step > 0 ? 0 : items.length - 1)
          : (current + step + items.length) % items.length;
        pickSuggest(which, a, true);
      } else if (ev.key === "Enter") {
        if (items.length) {
          ev.preventDefault();
          pickSuggest(which, suggestState[which].active < 0 ? 0 : suggestState[which].active);
        }
      } else if (ev.key === "Escape") {
        hideSuggest(which);
      }
    }

    async function submitAddSync() {
      const localPath = document.getElementById("sync-local-path").value.trim();
      const remotePath = document.getElementById("sync-remote-path").value.trim();
      const direction = document.getElementById("sync-direction").value;
      const mirror = document.getElementById("sync-mirror").checked;
      const always = document.getElementById("sync-always").checked;
      let interval = parseInt(document.getElementById("sync-interval").value, 10);
      if (isNaN(interval)) interval = 15;
      if (!localPath || !remotePath) {
        alert("Pick both a local folder and a remote folder (use Browse for quick select).");
        return;
      }
      if (mirror && !confirmMirrorSync(direction, localPath, remotePath)) return;
      const isEdit = !!editingSyncId;
      const url = isEdit ? "/api/syncs/update" : "/api/syncs";
      const payload = isEdit
        ? { id: editingSyncId, local_path: localPath, remote_path: remotePath, direction: direction, mirror: mirror, always: always, interval: interval, run_now: true }
        : { server_id: currentServerId, local_path: localPath, remote_path: remotePath, direction: direction, mirror: mirror, always: always, interval: interval, run_now: true };
      closeSyncModal();
      showToast(isEdit ? `Saving folder sync...` : `Starting folder sync...`);
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        showToast(d.message || (d.ok ? "Sync complete" : "Sync failed"));
        fetchSyncs();
        fetchServers();
      } catch (err) {
        alert("Failed to save sync: " + err);
      }
    }

    const pageTitles = {
      home: "DevBoost Workspace",
      forwards: "DevBoost • Port Forward Manager",
      docker: "DevBoost • Docker Observability",
      syncs: "DevBoost • Folder Sync",
      services: "DevBoost • Service Discovery",
      usage: "DevBoost • AI Usage & Balances",
      servers: "DevBoost • Manage Servers"
    };
    function navigatePage(page, updateHash = true) {
      const valid = Object.prototype.hasOwnProperty.call(pageTitles, page) ? page : "home";
      document.querySelectorAll(".page-view").forEach(view => view.classList.toggle("active", view.id === "page-" + valid));
      document.querySelectorAll(".workspace-tab").forEach(tab => tab.classList.toggle("active", tab.dataset.page === valid));
      document.getElementById("clean-orphans-btn").style.display = valid === "forwards" ? "inline-flex" : "none";
      document.querySelector("#header-actions .btn-primary").style.display = valid === "forwards" ? "inline-flex" : "none";
      if (valid === "servers") renderServerManagement();
      // Entering Quotas is an explicit request for current values, rather
      // than waiting for the background minute refresh.
      if (valid === "usage") fetchUsage(true);
      document.title = pageTitles[valid];
      if (updateHash && window.location.hash !== "#" + valid) window.history.pushState(null, "", "#" + valid);
    }
    window.addEventListener("hashchange", () => navigatePage(window.location.hash.slice(1), false));
    window.addEventListener("popstate", () => navigatePage(window.location.hash.slice(1), false));
    navigatePage(window.location.hash.slice(1) || "home", false);

    fetchServers().then(() => { fetchStatus(); fetchDocker(); fetchSyncs(); fetchUsage(); });
    setInterval(fetchStatus, 4000);
    setInterval(fetchDocker, 5000);
    setInterval(fetchSyncs, 8000);
    setInterval(fetchLocalPorts, 10000);
    // Usage snapshots are persisted by the backend, so periodic reads must
    // explicitly refresh them to reflect provider/CLI changes.
    setInterval(() => fetchUsage(true), 60000);
    setInterval(refreshUsageAges, 1000);
    setInterval(fetchServers, 15000);
