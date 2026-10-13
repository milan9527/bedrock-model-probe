import { updateMultiSelect } from "/static/multiselect.js";

const $ = id => document.getElementById(id);
const escape = value => String(value ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const utc = value => value ? new Date(value * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "Not recorded";

export function initManagement(api, changed) {
  let data, regions = {}, selected = new Set(), busy = false, discovery, poll, loading, dispatchIssues = {};
  const message = (text, error = false) => {
    $("management-error").hidden = !error || !text;
    $("management-message").hidden = error || !text;
    $(error ? "management-error" : "management-message").textContent = text;
  };
  const visible = () => (data?.targets || []).filter(target => {
    const text = $("management-search").value.trim().toLowerCase();
    if (text && !target.model_id.toLowerCase().includes(text)) return false;
    return [...$("management-filters").querySelectorAll("select")].every(select => {
      const values = [...select.selectedOptions].map(option => option.value);
      return !values.length || values.includes(String(target[select.name]));
    });
  });
  function controls() {
    const writable = data?.can_manage && !busy;
    $("discover-models").disabled = !writable || Boolean(discovery);
    $("auto-add-models").disabled = !writable;
    $("enable-probes").disabled = !writable || !selected.size;
    $("pause-probes").disabled = !writable || !selected.size;
    $("management-reload").disabled = busy;
    $("management-selection").textContent = `${selected.size} selected · ${visible().length} shown`;
    const rows = visible();
    const count = rows.filter(t => selected.has(t.key)).length;
    $("select-visible-targets").disabled = !writable || !rows.length;
    $("select-visible-targets").checked = Boolean(rows.length) && count === rows.length;
    $("select-visible-targets").indeterminate = count > 0 && count < rows.length;
  }
  function renderRows() {
    $("management-results").innerHTML = visible().map(t => {
      const snapshot = data.regions[t.region].schedule;
      const fresh = snapshot.scheduled_at && Date.now() / 1000 - snapshot.scheduled_at <= 2 * (data.schedule_seconds || 3600);
      const applied = fresh && Number(snapshot.policy_revision) === Number(data.policy.revision);
      const collection = !fresh ? "Schedule not current"
        : !applied ? "Awaiting next schedule"
        : t.enabled && t.scheduled ? "Scheduled" : !t.enabled ? "Paused" : "Awaiting next schedule";
      return `<tr><td><input type="checkbox" data-key="${t.key}" aria-label="Select ${escape(t.model_id)} ${escape(t.region)} ${escape(t.endpoint)} ${escape(t.api)}" ${selected.has(t.key) ? "checked" : ""} ${!data.can_manage || busy ? "disabled" : ""}></td>
        <td>${escape(t.model_id)}<small>${escape(t.provider)}</small></td><td>${escape(regions[t.region] || t.region)}<small>${escape(t.region)}</small></td>
        <td>${escape(t.endpoint)}<small>${escape(t.api)}</small></td><td>${escape(t.routing)}</td>
        <td class="${t.enabled ? "selection-enabled" : ""}">${t.enabled ? "Enabled" : "Not enabled"}</td>
        <td>${collection}<small>${snapshot.scheduled_at ? "Last run " + utc(snapshot.scheduled_at) : "No scheduled run yet"}</small></td></tr>`;
    }).join("") || '<tr><td colspan="7" class="empty-cell">No discovered targets match these filters.</td></tr>';
    controls();
  }
  function render() {
    const valid = new Set(data.targets.map(t => t.key));
    selected = new Set([...selected].filter(key => valid.has(key)));
    $("auto-add-models").checked = data.policy.auto_add;
    $("management-access").textContent = data.can_manage ? "Administrator" : "View only · Administrator required to make changes";
    const enabled = data.targets.filter(t => t.enabled).length;
    const routes = ["regional", "geographic", "global"].map(route =>
      `${data.targets.filter(t => t.enabled && t.routing === route).length} ${route}`).join(" / ");
    $("management-summary").textContent = `${new Set(data.targets.map(t => t.model_id)).size} model/profile IDs · ${data.targets.length} targets · ${enabled} enabled (${routes}) · ${enabled * data.input_sizes.length} probes per run`;
    $("management-schedule").textContent = `Next run ${utc(data.next_schedule_at)} · Queued probes can finish after pausing`;
    $("discovery-regions").innerHTML = Object.entries(data.regions).map(([id, value]) => {
      const d = value.discovery;
      const waiting = discovery?.regions[id]?.accepted && (d.discovered_at || 0) < discovery.requested_at;
      return `<div class="discovery-region ${d.issues?.length || dispatchIssues[id] ? "discovery-warning" : ""}">
        <strong>${escape(regions[id] || id)}</strong><span>${waiting ? "Discovering…" : utc(d.discovered_at)}</span>
        ${dispatchIssues[id] ? `<details><summary>Discovery request failed</summary><p>${escape(dispatchIssues[id])}</p></details>` : ""}
        ${d.issues?.length ? `<details><summary>${d.issues.length} discovery issues</summary>${d.issues.map(i => `<p>${escape(i.surface)}: ${escape(i.error)}</p>`).join("")}</details>` : ""}</div>`;
    }).join("");
    renderRows();
  }
  async function load() {
    if (loading) return loading;
    loading = (async () => {
      try {
        data = await api("/api/management");
        render();
        if (discovery) {
          const accepted = Object.entries(discovery.regions).filter(([, v]) => v.accepted);
          const complete = accepted.every(([r]) => (data.regions[r].discovery.discovered_at || 0) >= discovery.requested_at);
          if (complete || Date.now() / 1000 - discovery.requested_at > 180) {
            const errors = Object.values(discovery.regions).filter(v => !v.accepted).length;
            const notices = accepted.filter(([r]) => data.regions[r].discovery.issues?.length).length;
            message(!complete ? "Discovery is still running in some regions. Reload to check progress."
              : errors || notices ? `Discovery finished with notices in ${errors + notices} regions. Review regional details.`
              : `Discovery complete across all ${accepted.length} regions. New eligible models ${data.policy.auto_add ? "are automatically enabled for the next run" : "are ready to select and add to probes"}.`);
            discovery = null;
            controls();
            changed();
          } else {
            clearTimeout(poll);
            poll = setTimeout(load, 4000);
          }
        }
      } catch (error) {
        message(error.message, true);
        if (discovery) {
          discovery = null;
          clearTimeout(poll);
        }
      } finally { loading = null; controls(); }
    })();
    return loading;
  }
  async function save(changes, autoAdd) {
    if (!data?.can_manage || busy) return;
    busy = true;
    renderRows();
    message("");
    try {
      const result = await api("/api/management/policy", undefined, {
        method: "PATCH", body: JSON.stringify({ revision: data.policy.revision, changes,
          ...(autoAdd === undefined ? {} : { auto_add: autoAdd }) }),
      });
      selected.clear();
      await load();
      message(`Saved. Probe selections apply at ${utc(result.next_schedule_at)}. Existing measurements remain available in Overview.`);
      changed();
    } catch (error) {
      message(error.message, true);
      await load();
    } finally { busy = false; if (data) render(); }
  }
  $("management-reload").addEventListener("click", () => { message(""); load(); });
  $("management-filters").addEventListener("submit", event => event.preventDefault());
  $("management-filters").addEventListener("change", () => { selected.clear(); renderRows(); });
  $("management-search").addEventListener("input", () => { selected.clear(); renderRows(); });
  $("management-results").addEventListener("change", event => {
    const key = event.target.dataset.key;
    if (!key) return;
    if (event.target.checked) selected.add(key); else selected.delete(key);
    controls();
  });
  $("select-visible-targets").addEventListener("change", event => {
    for (const target of visible()) {
      if (event.target.checked) selected.add(target.key); else selected.delete(target.key);
    }
    renderRows();
  });
  $("enable-probes").addEventListener("click", () => save(Object.fromEntries([...selected].map(key => [key, true]))));
  $("pause-probes").addEventListener("click", () => save(Object.fromEntries([...selected].map(key => [key, false]))));
  $("auto-add-models").addEventListener("change", event => save({}, event.target.checked));
  $("discover-models").addEventListener("click", async () => {
    if (busy || discovery) return;
    busy = true;
    controls();
    message("Fetching the latest Runtime and Mantle models across all regions…");
    try {
      discovery = await api("/api/management/discover", undefined, { method: "POST" });
      dispatchIssues = Object.fromEntries(Object.entries(discovery.regions)
        .filter(([, value]) => !value.accepted).map(([region, value]) => [region, value.error]));
      await load();
    } catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  });
  return {
    load,
    setRegions(value) {
      regions = value;
      const select = $("management-filters").elements.region;
      const chosen = new Set([...select.selectedOptions].map(option => option.value));
      select.innerHTML = Object.entries(regions).map(([id, name]) =>
        `<option value="${escape(id)}" ${chosen.has(id) ? "selected" : ""}>${escape(name)}</option>`).join("");
      updateMultiSelect(select);
    },
  };
}
