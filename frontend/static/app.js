import { initMultiSelects, updateMultiSelect } from "/static/multiselect.js";
import { initManagement } from "/static/management.js";

const $ = (id) => document.getElementById(id);
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const format = (value, digits = 0) => value == null ? "—" : Number(value).toLocaleString("en-US", { maximumFractionDigits: digits });
const utc = (time) => new Date(time * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC";
const colors = ["#9777da", "#659bdd", "#6caf98", "#d6a05e", "#b7789b", "#83a2b0", "#a5ac6e"];
let settings = {}, regions = {}, catalog = {}, controller;
let signingIn = false;
let thresholds = { ttft_ms: { warning: 5000, critical: 10000 }, ttot_ms: { warning: 30000, critical: 60000 },
  success_rate: { warning: .99, critical: .95 }, min_samples: 5 };
let autoRefreshSeconds = 60, lastRefreshAt = 0, nextRefreshAt = 0, refreshInFlight = false;
let lastQuery, analysisRequest, analysisKey = "", analysisPoll, analysisRetryAt = 0;
let activeDimension = "model_id";
let healthSignature = "";
let chartSeries = [], chartResize;
let chartWindow = { start: 0, end: 0, period: 3600 }, probeInterval = 3600;
const management = initManagement(api, refresh);
const token = () => sessionStorage.getItem("access_token");
const base64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function lockDashboard(message = "Redirecting to sign in…") {
  controller?.abort();
  analysisRequest?.abort();
  clearTimeout(analysisPoll);
  $("dashboard").hidden = true;
  $("signin-gate").hidden = false;
  $("signin-message").textContent = message;
  $("signin-retry").hidden = true;
}

function signInError(error) {
  lockDashboard(error.message);
  $("signin-retry").hidden = false;
}

async function signIn() {
  if (signingIn) return;
  signingIn = true;
  lockDashboard();
  try {
    const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
    const state = base64url(crypto.getRandomValues(new Uint8Array(24)));
    sessionStorage.setItem("pkce_verifier", verifier);
    sessionStorage.setItem("oauth_state", state);
    const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
    const params = new URLSearchParams({ client_id: settings.client_id, response_type: "code",
      redirect_uri: settings.redirect_uri, scope: "openid email", state,
      code_challenge_method: "S256", code_challenge: challenge });
    location.assign(`${settings.cognito_domain}/oauth2/authorize?${params}`);
  } catch (error) {
    signingIn = false;
    throw error;
  }
}

async function completeSignIn() {
  const params = new URLSearchParams(location.search);
  if (params.has("error")) throw new Error(params.get("error_description") || params.get("error"));
  if (!params.has("code")) return;
  const state = sessionStorage.getItem("oauth_state");
  if (!state || params.get("state") !== state) throw new Error("Sign-in state mismatch. Please sign in again.");
  const response = await fetch(`${settings.cognito_domain}/oauth2/token`, {
    method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", client_id: settings.client_id,
      redirect_uri: settings.redirect_uri, code: params.get("code"),
      code_verifier: sessionStorage.getItem("pkce_verifier") }),
  });
  if (!response.ok) throw new Error("Could not complete sign-in.");
  const data = await response.json();
  sessionStorage.setItem("access_token", data.access_token);
  sessionStorage.removeItem("oauth_state");
  sessionStorage.removeItem("pkce_verifier");
  history.replaceState({}, "", "/");
}

async function api(path, signal, options = {}) {
  const response = await fetch(path, { ...options, headers: {
    ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
    ...(options.body ? { "Content-Type": "application/json" } : {}),
  }, signal });
  const data = await response.json();
  if (response.status === 401) {
    sessionStorage.removeItem("access_token");
    $("auth").textContent = "Sign in";
    await signIn();
    throw new Error("Sign in to view probe measurements.");
  }
  if (!response.ok) throw new Error(data.detail || `API returned ${response.status}`);
  return data;
}

function updateModels() {
  const form = $("filters").elements;
  const selectedRegions = new Set([...form.region.selectedOptions].map(option => option.value));
  const selectedProviders = new Set([...form.provider.selectedOptions].map(option => option.value));
  const models = new Set();
  Object.keys(regions).forEach(region => {
    if (selectedRegions.size && !selectedRegions.has(region)) return;
    const entry = catalog[region] || {};
    const targets = [...(entry.available_targets || []), ...(entry.targets || [])];
    targets.forEach(t => {
      if (!selectedProviders.size || selectedProviders.has(t.provider)) models.add(t.model_id);
    });
  });
  const select = form.model;
  const allSelected = select.options.length > 0 && select.selectedOptions.length === select.options.length;
  const selected = new Set([...select.selectedOptions].map(option => option.value));
  // Keep valid choices and remove choices outside the new region/provider scope.
  select.innerHTML = [...models].sort().map(m => `<option value="${escape(m)}"${allSelected || selected.has(m) ? " selected" : ""}>${escape(m)}</option>`).join("");
  updateMultiSelect(select);
}

function renderDimensionTabs() {
  const dimensions = [...$("analysis-dimension").selectedOptions];
  if (!dimensions.some(option => option.value === activeDimension)) activeDimension = dimensions[0]?.value || "model_id";
  $("dimension-tabs").hidden = dimensions.length < 2;
  $("dimension-tabs").innerHTML = dimensions.map(option =>
    `<button type="button" class="view-tab ${option.value === activeDimension ? "active" : ""}"
      data-dimension="${option.value}" aria-pressed="${option.value === activeDimension}">${escape(option.text)}</button>`).join("");
}

function chart(id, series, metric) {
  const element = $(id);
  const { start, end, period } = chartWindow;
  const label = metric === "ttft_ms" ? "TTFT" : "Output throughput";
  const unit = metric === "ttft_ms" ? "ms" : "tok/s";
  const bucketLabel = { 600: "10-minute", 3600: "Hourly", 86400: "Daily" }[period] || `${period / 60}-minute`;
  $(`${id}-scope`).textContent = `${bucketLabel} means by region · UTC · Selected models and APIs combined`;
  const groups = new Map();
  for (const row of series) {
    const value = row[metric];
    if (!value?.n || !Number.isFinite(value.mean)) continue;
    const key = `${row.region}/${row.timestamp}`;
    const previous = groups.get(key) || { region: row.region, time: row.timestamp, sum: 0, n: 0 };
    previous.sum += value.mean * value.n;
    previous.n += value.n;
    groups.set(key, previous);
  }
  const points = [...groups.values()].map(p => ({ ...p, value: p.sum / p.n }));
  if (!points.length) {
    element.className = "chart-empty";
    element.textContent = "No successful measurements in this period.";
    return;
  }
  element.className = "chart";
  const containerWidth = element.clientWidth || (innerWidth > 720 ? (innerWidth - 44) / 2 : innerWidth - 22);
  const width = Math.max(260, containerWidth - 16), height = 156, left = 48, right = 12, top = 12, bottom = 28;
  const min = Math.floor(start / period) * period, max = Math.max(end, min + 1);
  const ceiling = Math.max(...points.map(p => p.value), 1) * 1.15;
  const x = t => left + (t - min) / (max - min) * (width - left - right);
  const y = v => height - bottom - v / ceiling * (height - top - bottom);
  const activeRegions = Object.keys(regions).filter(region => points.some(point => point.region === region));
  const color = region => colors[Object.keys(regions).indexOf(region) % colors.length];
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" tabindex="0"
    aria-label="${label} time series in ${unit}. Use Left and Right arrow keys to inspect time buckets."
    aria-describedby="${id}-scope" data-period="${period}" data-start="${min}" data-end="${max}">`;
  for (let i = 0; i <= 4; i++) {
    const value = ceiling * i / 4;
    svg += `<line x1="${left}" x2="${width-right}" y1="${y(value)}" y2="${y(value)}" stroke="#eff1f6"/><text x="${left-7}" y="${y(value)+3}" text-anchor="end">${format(value, ceiling < 10 ? 1 : 0)}</text>`;
  }
  const ticks = width < 450 ? 2 : 4;
  for (let i = 0; i <= ticks; i++) {
    const time = min + (max - min) * i / ticks;
    svg += `<text class="chart-time-tick" x="${x(time)}" y="${height-7}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${utc(time).slice(5,16)}</text>`;
  }
  for (const region of activeRegions) {
    const regional = points.filter(p => p.region === region).sort((a,b) => a.time-b.time);
    // Connect only consecutive buckets. Failed or missing buckets never become zeros.
    const path = regional.map((p, i) =>
      `${i && p.time - regional[i-1].time === period ? "L" : "M"}${x(p.time)},${y(p.value)}`).join(" ");
    svg += `<path class="chart-series-line" data-region="${escape(region)}" d="${path}" fill="none"
      stroke="${color(region)}" stroke-width="1.8" stroke-linejoin="round"/>`;
    svg += regional.map(p => `<circle cx="${x(p.time)}" cy="${y(p.value)}" r="2.6" fill="${color(region)}">
      <title>${escape(regions[region])}: ${format(p.value,1)} ${unit} · ${utc(p.time)} · ${format(p.n)} measurements</title></circle>`).join("");
  }
  svg += `<line class="chart-cursor" x1="${left}" x2="${left}" y1="${top}" y2="${height-bottom}" hidden/></svg>`;
  element.innerHTML = svg + `<div class="chart-tooltip" role="status" hidden></div><div class="legend">${activeRegions.map(r => `<span><i style="background:${color(r)}"></i>${escape(regions[r])}</span>`).join("")}</div>`;
  const plot = element.querySelector("svg"), tooltip = element.querySelector(".chart-tooltip");
  const cursor = element.querySelector(".chart-cursor");
  let inspected = min;
  const inspect = time => {
    const bucket = Math.max(min, Math.min(Math.floor((max - 1) / period) * period, Math.floor(time / period) * period));
    if (bucket === inspected && !tooltip.hidden) return;
    inspected = bucket;
    const rows = activeRegions.map(region => {
      const point = groups.get(`${region}/${inspected}`);
      return `<div><span><i style="background:${color(region)}"></i>${escape(regions[region])}</span><strong>${
        point ? `${format(point.sum / point.n, 1)} ${unit}` : "No measurements"
      }</strong>${point ? `<small>${format(point.n)} samples</small>` : ""}</div>`;
    }).join("");
    const partial = inspected < start || inspected + period > end;
    tooltip.innerHTML = `<strong>${utc(inspected)}${partial ? " · Partial bucket" : ""}</strong>${rows}`;
    tooltip.hidden = false;
    cursor.removeAttribute("hidden");
    cursor.setAttribute("x1", x(inspected));
    cursor.setAttribute("x2", x(inspected));
    const position = x(inspected) / width * plot.clientWidth;
    tooltip.style.left = `${Math.max(0, Math.min(position + 12, element.clientWidth - tooltip.offsetWidth - 8))}px`;
  };
  const hide = () => { tooltip.hidden = true; cursor.setAttribute("hidden", ""); };
  const inspectPointer = event => {
    const rect = plot.getBoundingClientRect();
    const position = (event.clientX - rect.left) / rect.width * width;
    if (position < left || position > width - right) return hide();
    inspect(min + (position - left) / (width - left - right) * (max - min));
  };
  plot.addEventListener("pointermove", inspectPointer);
  plot.addEventListener("pointerdown", inspectPointer);
  plot.addEventListener("pointerleave", event => { if (event.pointerType !== "touch") hide(); });
  plot.addEventListener("focus", () => inspect(inspected));
  plot.addEventListener("blur", hide);
  plot.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End", "Escape"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Escape") return hide();
    inspect(event.key === "Home" ? min : event.key === "End" ? max - 1
      : inspected + (event.key === "ArrowLeft" ? -period : period));
  });
}

const failureLabels = {
  data_retention_required: "AWS retention opt-in required",
  output_limit_no_text: "Output limit reached before visible text",
  no_visible_text: "No visible text returned",
  timeout: "Request timed out",
  access_denied: "Model access denied",
  throttled: "Rate limit reached",
  stale: "Probe started too late",
  worker_timeout: "Worker timed out",
  error: "Request failed",
};

function rowFailure(row) {
  const failure = row.last_failure;
  if (!failure) return "";
  let detail = failure.message;
  if (detail) {
    try { detail = JSON.parse(detail).error?.message || detail; } catch {}
  } else if (failure.reason === "output_limit_no_text") {
    detail = `The ${format(failure.max_output)}-token budget was exhausted before visible text. Reasoning tokens also use this budget.`;
  } else detail = failureLabels[failure.reason] || failure.status;
  const counts = Object.entries(row.failure_counts || {}).map(([reason, count]) =>
    `<li>${escape(failureLabels[reason] || reason)} · ${format(count)}</li>`).join("");
  return `<details class="row-failure"><summary>Failure history · ${format(row.errors)} in selected window</summary><div>
    <ul>${counts}</ul>${escape(detail)}<small>Most recent failure: ${utc(failure.scheduled_at)}</small>
    <small>Historical samples remain in the window success rate.</small></div></details>`;
}

function latencyLevel(metric, name) {
  if (!metric?.n || metric.n < thresholds.min_samples) return "unknown";
  if (metric.p95 >= thresholds[name].critical) return "critical";
  if (metric.p95 >= thresholds[name].warning) return "warning";
  return "healthy";
}

function healthState(row) {
  if (row.latest_health) {
    return { level: row.latest_health.failing ? "critical"
      : ["unknown", "historical", "pending"].includes(row.latest_health.state) ? "unknown" : "healthy" };
  }
  const completed = row.count - row.pending;
  if (!completed) return { level: "unknown" };
  if ((row.failure_counts?.data_retention_required || 0) + (row.failure_counts?.access_denied || 0) === completed) {
    return { level: "critical" };
  }
  if (row.errors) return {
    level: completed < thresholds.min_samples ? "warning"
      : row.success_rate < thresholds.success_rate.critical ? "critical"
      : row.success_rate < thresholds.success_rate.warning ? "warning" : "healthy",
  };
  if (completed < thresholds.min_samples) return { level: "unknown" };
  return { level: "healthy" };
}

function rowIssues(row, ttft, ttot) {
  const health = row.latest_health;
  const items = Object.entries(health?.active_failure_counts || row.failure_counts || {}).map(([reason, count]) =>
    `<li class="issue-critical">Latest failed result · ${escape(failureLabels[reason] || reason)} · ${format(count)}</li>`);
  if (health?.state === "historical") items.push("<li>Earlier configuration or retired target · historical results</li>");
  if (health?.recovered) items.push(`<li class="issue-recovered">Recovered in later scheduled probes
    <small>Latest success: ${utc(health.last_success_at)}</small></li>`);
  if (["warning", "critical"].includes(ttft)) items.push(`<li class="issue-${ttft}">Window TTFT P95 above threshold · ${format(row.ttft_ms.p95)} ms</li>`);
  if (["warning", "critical"].includes(ttot)) items.push(`<li class="issue-${ttot}">Window TTOT P95 above threshold · ${format(row.ttot_ms.p95)} ms</li>`);
  if (row.successes < thresholds.min_samples) items.push(`<li>Limited latency samples · ${format(row.successes)} successful</li>`);
  if (row.pending) items.push(`<li>${format(row.pending)} probes pending</li>`);
  return (items.length ? `<ul class="issue-list">${items.join("")}</ul>` : "—") + rowFailure(row);
}

function tableRow(row) {
  const state = healthState(row);
  const ttft = latencyLevel(row.ttft_ms, "ttft_ms"), ttot = latencyLevel(row.ttot_ms, "ttot_ms");
  const severity = [state.level, ttft, ttot].includes("critical") ? "critical"
    : [state.level, ttft, ttot].includes("warning") ? "warning" : state.level;
  return `<tr class="anomaly-${severity}">
    <td><span class="provider-dot ${row.provider === "OpenAI" ? "openai" : ""}"></span>${escape(row.model_id)}
      <small>${escape(row.endpoint)} · ${escape(row.api)} · ${row.max_output ? `${format(row.max_output)} cap` : escape(row.config_id)} · ${row.reasoning === "disabled" ? "Thinking off" : "Default reasoning"} · ${row.prompt_version === "short-64-v2" ? "64-word response" : "Story prompt"}</small></td>
    <td>${escape(regions[row.region] || row.region)}<small>${escape(row.routing)}</small></td>
    <td>${row.ttft_ms.p50 == null ? "—" : format(row.ttft_ms.p50)+" ms"}</td>
    <td class="metric-${ttft}" title="${row.ttft_ms.n} successful samples; warning ≥ ${format(thresholds.ttft_ms.warning)} ms, critical ≥ ${format(thresholds.ttft_ms.critical)} ms">${row.ttft_ms.p95 == null ? "—" : format(row.ttft_ms.p95)+" ms"}</td>
    <td class="metric-${ttot}" title="${row.ttot_ms.n} successful samples; warning ≥ ${format(thresholds.ttot_ms.warning)} ms, critical ≥ ${format(thresholds.ttot_ms.critical)} ms">${row.ttot_ms.p95 == null ? "—" : format(row.ttot_ms.p95)+" ms"}</td>
    <td>${format(row.output_tps.mean,1)}</td>
    <td title="Success rate for the selected window; latest outcomes are shown in Issues">${row.success_rate == null ? "—" : format(row.success_rate*100,1)+"%"}
      <small>${row.successes}/${row.count-row.pending} completed</small></td>
    <td class="issues-cell">${rowIssues(row, ttft, ttot)}</td></tr>`;
}

function render(data) {
  const health = data.health;
  healthSignature = health?.signature || "";
  if (health && $("health-summary")) {
    const element = $("health-summary");
    element.hidden = false;
    element.dataset.state = health.state;
    const headline = !health.cohorts ? "No scheduled results in this view"
      : health.state === "historical" ? "Only historical configurations in this view"
      : health.failing ? `${format(health.failing)} combinations have a latest failed result`
      : health.awaiting_first_result ? "Waiting for first scheduled results"
      : "Latest scheduled results: no probe failures";
    const detail = [
      health.recovered ? `${format(health.recovered)} combinations recovered in later scheduled probes.` : "",
      health.historical_cohorts ? `${format(health.historical_cohorts)} combinations are earlier configurations or retired targets.` : "",
      health.pending_samples ? `${format(health.pending_samples)} probes are collecting; pending work does not clear prior failures.` : "",
      "Latest results are evaluated per matching configuration in this view. Statistics below cover the full selected window.",
    ].filter(Boolean).join(" ");
    element.innerHTML = `<strong>${escape(headline)}</strong><details><summary>Details</summary><span>${escape(detail)}</span></details>`;
  }
  const mean = (metric) => {
    const values = data.summary.filter(s => s[metric]?.n);
    const n = values.reduce((sum, s) => sum + s[metric].n, 0);
    return n ? values.reduce((sum,s) => sum + s[metric].mean*s[metric].n, 0) / n : null;
  };
  const stat = (id, value, unit, digits = 0) => { $(id).innerHTML = `${format(value,digits)}${value != null ? `<small>${unit}</small>` : ""}`; };
  stat("ttft", mean("ttft_ms"), "ms");
  stat("ttot", mean("ttot_ms"), "ms");
  stat("tps", mean("output_tps"), "tok/s", 1);
  const count = data.summary.reduce((sum,s) => sum+s.count-s.pending,0);
  const successes = data.summary.reduce((sum,s) => sum+s.successes,0);
  stat("success", count ? successes/count*100 : null, "%", 1);
  $("sample-count").textContent = `${format(data.samples)} scheduled samples · ${format(count-successes)} unsuccessful in window`;
  $("series-count").textContent = `${data.summary.length} combinations`;
  $("freshness").textContent = data.latest_sample
    ? `Latest scheduled sample: ${utc(data.latest_sample)}${Date.now()/1000-data.latest_sample>2*probeInterval ? " · Data is stale" : ""}`
    : "No measurements in the selected period";
  const failures = {};
  for (const row of data.summary) for (const [reason, n] of Object.entries(row.failure_counts || {})) {
    failures[reason] = (failures[reason] || 0) + n;
  }
  const notices = [];
  if (failures.output_limit_no_text) notices.push(`${format(failures.output_limit_no_text)} output-budget failures occurred in the selected window.`);
  if (failures.data_retention_required) notices.push(`${format(failures.data_retention_required)} retention rejections occurred in the selected window. ${
    health?.active_failure_counts?.data_retention_required
      ? "Some latest scheduled results still show retention rejections."
      : "The latest scheduled results no longer show retention rejections."}`);
  if (!data.samples) notices.push("No measurements match these filters. Check Management for regional availability and enabled probes, or choose another time range.");
  $("coverage-notice").textContent = notices.join(" ");
  $("coverage-notice").hidden = notices.length === 0;
  chartSeries = data.series;
  chartWindow = { start: data.start, end: data.end, period: data.period };
  renderCharts();
  $("results").innerHTML = data.summary.length ? data.summary.sort((a,b) => a.model_id.localeCompare(b.model_id) || a.region.localeCompare(b.region)).map(tableRow).join("")
    : `<tr><td colspan="8" class="empty-cell">No probe results match these filters.<small>Check enabled probes in Management or choose another time range.</small></td></tr>`;
  $("failures").innerHTML = data.failures.length ? data.failures.map(f => `<div class="failure-row">
    <strong>${escape(f.model_id)}</strong> · ${escape(f.region)} · ${escape(f.endpoint)}/${escape(f.api)}
    · ${utc(f.scheduled_at)} · ${escape(f.status)}<br>${escape(f.provider_error || f.error || "The response contained no visible text.")}</div>`).join("") : "None in this period.";
}

function renderCharts() {
  chart("latency-chart", chartSeries, "ttft_ms");
  chart("throughput-chart", chartSeries, "output_tps");
}

function analysisIdentity(query) {
  const identity = new URLSearchParams(query);
  const end = Number(identity.get("end"));
  identity.set("range", end - Number(identity.get("start")));
  identity.delete("start");
  identity.set("end", Math.floor(end/probeInterval)*probeInterval);
  identity.set("dimension", activeDimension);
  identity.set("available_regions", Object.keys(regions).sort().join(","));
  identity.set("health", healthSignature);
  return identity.toString();
}

function clearAnalysis() {
  analysisRequest?.abort();
  clearTimeout(analysisPoll);
  analysisKey = "";
  analysisRetryAt = 0;
  $("analysis-status").textContent = "Waiting for statistics";
  $("analysis-model").textContent = "";
  $("analysis-scope").textContent = "Updating the analysis scope for the current filters…";
  $("observations").innerHTML = "<li>Loading statistical evidence…</li>";
  $("ai-text").textContent = "Waiting for statistics in the current view.";
  $("analysis-groups").innerHTML = "";
  $("analysis-comparison").textContent = "";
}

function renderAnalysis(data) {
  const labels = { ready: "AI updated", pending: "Generating analysis", insufficient_data: "Insufficient data",
    unavailable: "AI unavailable", limited: "Retry later", query_limit: "Narrow the range" };
  $("analysis-status").textContent = (labels[data.status] || "Waiting for analysis") + (data.cached ? " · Cached" : "");
  const evidence = data.evidence;
  if (evidence) {
    $("analysis-scope").textContent = `${utc(evidence.window.start)} → ${utc(evidence.window.end)} · ${format(evidence.overall.completed)} completed scheduled probes · Generated ${utc(data.generated_at)} · Historical trends exclude the collecting interval; latest outcomes also consider newer completed probes`;
    $("observations").innerHTML = evidence.observations.map(note => `<li>${escape(note)}</li>`).join("");
    $("analysis-comparison").textContent = `Trends compare identical configurations before and after ${evidence.window.midpoint_utc.replace("T"," ").replace("+00:00"," UTC")}, with ${evidence.comparable_cohorts} comparable groups. Showing ${evidence.dimension_groups.length}/${evidence.dimension_group_count} groups for the selected dimension. Mixed-model statistics do not establish regional superiority.`;
    $("analysis-groups").innerHTML = evidence.dimension_groups.map(group => `<tr>
      <td>${escape(regions[group.name] || group.name)}</td><td>${format(group.completed)}</td>
      <td>${group.success_rate == null ? "—" : format(group.success_rate*100,2)+"%"}</td>
      <td>${group.ttft_ms.p95 == null ? "—" : format(group.ttft_ms.p95)+" ms"}</td>
      <td>${group.ttot_ms.p95 == null ? "—" : format(group.ttot_ms.p95)+" ms"}</td>
      <td>${format(group.policy_failures)}</td></tr>`).join("");
  }
  $("analysis-model").textContent = data.ai ? data.ai.model_id + (data.ai.partial ? " · Output length limit reached" : "") : "";
  const aiText = data.ai?.text || data.message || ({
    pending: "Generating conclusions from the current statistics…",
    insufficient_data: "Too few completed scheduled probes to generate a conclusion. Select a longer time range.",
    limited: "The analysis call limit was reached. Retry later; statistical observations remain available.",
  }[data.status] || "AI is temporarily unavailable. Statistical observations remain available.");
  // Models occasionally add Markdown despite the plain-text instruction. Render as safe text.
  $("ai-text").textContent = aiText.replace(/\*\*([^*]+)\*\*/g, "$1").replace(/^#{1,6}\s+/gm, "")
    .replace(/\bdata_retention_required\b/g, "retention restrictions");
}

async function refreshAnalysis(query) {
  const key = analysisIdentity(query);
  if (key === analysisKey && Date.now() < analysisRetryAt) return;
  clearAnalysis();
  analysisKey = key;
  analysisRetryAt = Infinity;
  const current = new AbortController();
  analysisRequest = current;
  const requestQuery = new URLSearchParams(query);
  requestQuery.set("dimension", activeDimension);
  $("analysis-status").textContent = "Generating analysis";
  $("ai-text").textContent = "Analyzing stability, latency and changes over time…";
  const load = async (attempt = 0) => {
    try {
      const data = await api(`/api/analysis?${requestQuery}`, current.signal);
      if (analysisRequest !== current || analysisKey !== key) return;
      renderAnalysis(data);
      if (data.status === "pending" && attempt < 15) {
        analysisPoll = setTimeout(() => {
          if (document.hidden) analysisRetryAt = 0;
          else load(attempt + 1);
        }, (data.retry_after || 4) * 1000);
      } else if (data.status !== "ready") analysisRetryAt = Date.now() + 60000;
    } catch (error) {
      if (error.name === "AbortError" || analysisRequest !== current) return;
      renderAnalysis({ status: "unavailable", message: error.message });
      analysisRetryAt = Date.now() + 60000;
    }
  };
  await load();
}

function updateRefreshStatus() {
  const last = lastRefreshAt ? ` · Updated ${new Date(lastRefreshAt).toISOString().slice(11,19)} UTC` : "";
  $("refresh-status").textContent = refreshInFlight ? "Refreshing…"
    : !autoRefreshSeconds ? "Auto-refresh off" + last
    : document.hidden ? "Auto-refresh paused in background" + last
    : `Refresh in ${Math.max(0, Math.ceil((nextRefreshAt-Date.now())/1000))}s` + last;
}

async function refresh() {
  if (!settings.local && !token()) return;
  renderDimensionTabs();
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  $("refresh").disabled = true;
  refreshInFlight = true;
  updateRefreshStatus();
  $("error").hidden = true;
  try {
    const config = await api("/api/config", current.signal);
    if (controller !== current) return;
    thresholds = config.anomaly_thresholds || thresholds;
    regions = config.regions;
    probeInterval = config.schedule_seconds || 3600;
    $("region-coverage").textContent = `${Object.keys(regions).length} regions · ${probeInterval === 3600 ? "Hourly" : probeInterval / 60 + "-minute"} probes · 64-word target`;
    management.setRegions(regions);
    const select = $("filters").elements.region;
    const selectedRegions = new Set([...select.selectedOptions].map(option => option.value));
    select.replaceChildren();
    for (const [id, name] of Object.entries(regions)) {
      select.add(new Option(name, id, false, selectedRegions.has(id)));
    }
    updateMultiSelect(select);
    const discovered = await api("/api/catalog", current.signal);
    if (controller !== current) return;
    catalog = discovered.regions;
    updateModels();
    // Build the request after dependent model choices have been reconciled.
    const form = $("filters").elements;
    const end = Math.floor(Date.now()/1000);
    const query = new URLSearchParams({ start: end-Number(form.range.value), end, period: form.period.value });
    for (const select of $("filters").querySelectorAll("select[multiple]")) {
      // Empty and fully selected categorical filters both mean "all".
      if (select.selectedOptions.length === select.options.length) continue;
      for (const option of select.selectedOptions) query.append(select.name, option.value);
    }
    if (analysisIdentity(query) !== analysisKey) clearAnalysis();
    const data = await api(`/api/metrics?${query}`, current.signal);
    if (controller !== current) return;
    render(data);
    lastRefreshAt = Date.now();
    lastQuery = query;
    refreshAnalysis(query);
    if (!$("management").hidden) management.load();
  } catch (error) {
    if (error.name !== "AbortError") {
      $("error").textContent = error.message;
      $("error").hidden = false;
    }
  } finally {
    if (controller === current) {
      $("refresh").disabled = false;
      refreshInFlight = false;
      nextRefreshAt = Date.now() + autoRefreshSeconds * 1000;
      updateRefreshStatus();
    }
  }
}

document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(tab => { tab.hidden = tab.id !== button.dataset.tab; });
  document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("active",b === button));
  $("page-name").textContent = button.textContent.trim();
  if (button.dataset.tab === "management") management.load();
  else renderCharts();
}));
window.addEventListener("resize", () => {
  cancelAnimationFrame(chartResize);
  chartResize = requestAnimationFrame(renderCharts);
});
$("filters").addEventListener("change", event => {
  if (event.target.matches("select")) {
    if (["region", "provider"].includes(event.target.name)) updateModels();
    clearAnalysis();
    refresh();
  }
});
$("filters").addEventListener("submit", event => event.preventDefault());
$("refresh").addEventListener("click", refresh);
$("analysis-dimension").addEventListener("change", refresh);
$("dimension-tabs").addEventListener("click", event => {
  const button = event.target.closest("[data-dimension]");
  if (!button) return;
  activeDimension = button.dataset.dimension;
  refresh();
});
$("auto-refresh").addEventListener("change", () => {
  autoRefreshSeconds = Number($("auto-refresh").value);
  nextRefreshAt = Date.now() + autoRefreshSeconds * 1000;
  try { localStorage.setItem("bedrock-probe-refresh", String(autoRefreshSeconds)); } catch {}
  updateRefreshStatus();
});
document.addEventListener("visibilitychange", () => {
  updateRefreshStatus();
  if (!document.hidden && !refreshInFlight && !signingIn) {
    if (autoRefreshSeconds && Date.now() >= nextRefreshAt) refresh();
    else if (lastQuery && analysisRetryAt === 0) refreshAnalysis(lastQuery);
  }
});
$("signin-retry").addEventListener("click", () => {
  if (settings.cognito_domain) signIn().catch(signInError);
  else location.reload();
});
$("auth").addEventListener("click", async () => {
  if (settings.local) return;
  if (token()) {
    lockDashboard("Signing out…");
    sessionStorage.removeItem("access_token");
    location.assign(`${settings.cognito_domain}/logout?${new URLSearchParams({ client_id: settings.client_id, logout_uri: settings.redirect_uri })}`);
  } else await signIn();
});
try {
  initMultiSelects();
  const response = await fetch("/config.json", { cache: "no-store" });
  if (!response.ok) throw new Error("Dashboard configuration is unavailable.");
  settings = await response.json();
  await completeSignIn();
  $("mode").textContent = settings.local ? "Local preview" : "AWS connected";
  $("auth").textContent = settings.local ? "Local session" : token() ? "Sign out" : "Sign in";
  if (!settings.local && !token()) await signIn();
  else {
    // Validate an existing or newly issued session before revealing the dashboard.
    if (!settings.local) await api("/api/config");
    $("signin-gate").hidden = true;
    $("dashboard").hidden = false;
    try {
      const saved = localStorage.getItem("bedrock-probe-refresh");
      if (["0","30","60","300"].includes(saved)) {
        autoRefreshSeconds = Number(saved);
        $("auto-refresh").value = saved;
      }
    } catch {}
    await refresh();
    setInterval(() => {
      updateRefreshStatus();
      if (autoRefreshSeconds && !document.hidden && !refreshInFlight && !signingIn && Date.now() >= nextRefreshAt) refresh();
    }, 1000);
  }
} catch (error) {
  if (!signingIn) signInError(error);
}
