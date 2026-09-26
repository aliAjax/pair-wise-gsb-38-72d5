"use strict";

/* Pages only render data returned by the API. Sentence differences come from
   GET /api/versions/{id}/diff (computed by diff.py) and are never reimplemented here. */

const $ = (id) => document.getElementById(id);
const state = { projects: [], versions: [], current: null, parentDetail: null, deliveries: [] };

function authHeaders(extra = {}) {
  return Object.assign({ "Content-Type": "application/json", "X-User": $("user").value, "X-Role": $("role").value }, extra);
}

async function api(url, options) {
  const res = await fetch(url, options || {});
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.status);
  return body;
}

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtTime(value) {
  return value ? esc(value.replace("T", " ").replace("+00:00", "Z")) : "";
}

function statusTag(status) {
  return `<span class="tag ${esc(status)}">${esc(status)}</span>`;
}

async function refreshAll() {
  try {
    state.projects = (await api("/api/projects")).projects;
    state.deliveries = (await api("/api/deliveries")).deliveries;
    renderProjects();
    await onProjectChange();
    renderDeliveries();
  } catch (e) {
    alert(e.message);
  }
}

function renderProjects() {
  const select = $("projectSelect");
  const keep = select.value || (state.projects[0] && String(state.projects[0].id));
  select.innerHTML = state.projects.map((p) =>
    `<option value="${p.id}">#${p.id} ${esc(p.name)}（源语言 ${esc(p.source_language)}）</option>`
  ).join("");
  if (keep) select.value = keep;
}

async function onProjectChange() {
  const projectId = $("projectSelect").value;
  if (!projectId) return;
  state.versions = (await api(`/api/versions?project_id=${projectId}`)).versions;
  renderVersionList();
  renderParentOptions();
}

function renderVersionList() {
  const rows = state.versions.map((v) => `
    <tr>
      <td class="num">${v.id}</td>
      <td class="num">v${v.version_no}</td>
      <td>${esc(v.language)}</td>
      <td>${statusTag(v.status)}</td>
      <td>${v.parent_id ? `父版本 #${v.parent_id}` : "初始版本"}</td>
      <td>${v.revision_reason ? esc(v.revision_reason) : '<span class="muted">—</span>'}</td>
      <td><button onclick="openVersion(${v.id})">打开</button></td>
    </tr>`).join("");
  $("versionList").innerHTML = `
    <table><thead><tr><th>ID</th><th>版本号</th><th>语言</th><th>状态</th><th>来源</th><th>返修原因</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="7" class="muted">该项目还没有版本</td></tr>'}</tbody></table>`;
}

function renderParentOptions() {
  const language = $("newLanguage").value.trim();
  const options = state.versions
    .filter((v) => v.language === language)
    .map((v) => `<option value="${v.id}">#${v.id} v${v.version_no}（${esc(v.status)}）</option>`)
    .join("");
  $("newParent").innerHTML = `<option value="">— 空版本，不继承父版本 —</option>${options}`;
  onParentChange();
}

function onParentChange() {
  $("reasonWrap").hidden = !$("newParent").value;
  $("newReason").required = !!$("newParent").value;
}

async function createVersion(event) {
  event.preventDefault();
  const out = $("createResult");
  try {
    const payload = { language: $("newLanguage").value.trim() };
    if ($("newParent").value) {
      payload.parent_id = Number($("newParent").value);
      payload.revision_reason = $("newReason").value.trim();
    }
    const created = await api(`/api/projects/${$("projectSelect").value}/versions`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(payload),
    });
    out.className = "result ok";
    out.textContent = `已创建版本 #${created.id}${payload.parent_id ? `，已从父版本 #${payload.parent_id} 继承字幕` : ""}`;
    await onProjectChange();
    await openVersion(created.id);
  } catch (e) {
    out.className = "result error";
    out.textContent = e.message;
  }
}

async function openVersion(versionId) {
  try {
    state.current = await api(`/api/versions/${versionId}`);
    const [cueData, commentData, diffData, reviewData] = await Promise.all([
      api(`/api/versions/${versionId}/cues`),
      api(`/api/versions/${versionId}/comments`),
      api(`/api/versions/${versionId}/diff`),
      api(`/api/versions/${versionId}/reviews`),
    ]);
    state.parentDetail = state.current.parent
      ? await api(`/api/versions/${state.current.parent.id}`)
      : null;
    $("versionPanel").hidden = false;
    renderVersionMeta();
    renderDiff(diffData);
    renderCues(cueData.cues);
    renderComments(commentData.comments);
    renderReviews(reviewData.reviews);
    $("stateResult").textContent = "";
  } catch (e) {
    alert(e.message);
  }
}

function renderVersionMeta() {
  const v = state.current;
  $("versionTitle").textContent = `#${v.id} v${v.version_no}（${v.language}）`;
  let html = `<p>${statusTag(v.status)} 修订号 revision=${v.revision} · 创建人 ${esc(v.created_by)} · ${fmtTime(v.created_at)}</p>`;
  if (v.parent_id) {
    const pr = v.latest_review;
    html += `<p><b>返修版本</b>，父版本 <button class="linklike" onclick="openVersion(${v.parent_id})">#${v.parent_id} v${v.parent.version_no}（${esc(v.parent.status)}）</button></p>`;
    html += `<p>返修原因：${esc(v.revision_reason) || '<span class="muted">未填写</span>'}</p>`;
    if (state.parentDetail && state.parentDetail.latest_review) {
      const r = state.parentDetail.latest_review;
      html += `<p class="muted">父版本最新复核结论：${r.decision === "approve" ? "通过" : "退回"}（${esc(r.reviewer)}：${esc(r.comment) || "无批注"}，${fmtTime(r.created_at)}）</p>`;
    }
  }
  $("versionMeta").innerHTML = html;
}

function cueRow(cue) {
  return `<tr>
    <td class="num">${cue.cue_index}</td>
    <td class="num">${cue.start_ms}–${cue.end_ms}</td>
    <td>${esc(cue.text)}</td>
    <td>${esc(cue.updated_by)}</td>
  </tr>`;
}

function renderDiff(diffData) {
  const panel = $("diffPanel");
  if (!diffData.is_revision) {
    panel.innerHTML = '<p class="muted">该版本不是返修版本，无父版本差异。</p>';
    return;
  }
  const d = diffData.diff;
  const s = d.summary;
  const banner = diffData.retained
    ? `<div class="diff-banner"><b>返修差异保留中</b>：复核通过前持续对照父版本 #${diffData.parent_id}。新增 ${s.added} · 改写 ${s.modified} · 移除 ${s.removed} · 未变 ${s.unchanged}</div>`
    : `<div class="diff-banner closed"><b>复核已通过</b>：差异已归档仅供查看。新增 ${s.added} · 改写 ${s.modified} · 移除 ${s.removed} · 未变 ${s.unchanged}</div>`;

  const addedRows = d.added.map((c) =>
    `<tr class="diff-row diff-added"><td class="num">${c.cue_index}</td><td class="num">${c.start_ms}–${c.end_ms}</td><td>—</td><td>${esc(c.text)}</td><td>新增</td></tr>`).join("");
  const removedRows = d.removed.map((c) =>
    `<tr class="diff-row diff-removed"><td class="num">${c.cue_index}</td><td class="num">${c.start_ms}–${c.end_ms}</td><td>${esc(c.text)}</td><td>—</td><td>移除</td></tr>`).join("");
  const modifiedRows = d.modified.map((m) =>
    `<tr class="diff-row diff-modified">
      <td class="num">${m.cue_index}</td>
      <td class="num">${m.before.start_ms}–${m.before.end_ms}<br>${m.after.start_ms}–${m.after.end_ms}</td>
      <td>${esc(m.before.text)}</td>
      <td>${esc(m.after.text)}</td>
      <td>改写（${m.changes.includes("text") ? "文案" : ""}${m.changes.length === 2 ? "、" : ""}${m.changes.includes("timing") ? "时间轴" : ""}）</td>
    </tr>`).join("");

  panel.innerHTML = `${banner}
    <table>
      <thead><tr><th>序号</th><th>时间(ms) 父→子</th><th>父版本句子</th><th>当前句子</th><th>差异</th></tr></thead>
      <tbody>${addedRows}${modifiedRows}${removedRows || '<tr><td colspan="5" class="muted">没有移除的句子</td></tr>'}</tbody>
    </table>`;
}

function renderCues(cues) {
  const rows = cues.map(cueRow).join("");
  $("cueList").innerHTML = `<table><thead><tr><th>序号</th><th>时间(ms)</th><th>文案</th><th>更新人</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4" class="muted">暂无字幕</td></tr>'}</tbody></table>`;
}

function renderComments(comments) {
  const rows = comments.map((c) =>
    `<tr><td class="num">${c.time_ms}</td><td>${esc(c.user)}</td><td>${esc(c.body)}</td><td>${fmtTime(c.created_at)}</td></tr>`).join("");
  $("commentList").innerHTML = `<table><thead><tr><th>时间(ms)</th><th>人员</th><th>内容</th><th>时间</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4" class="muted">暂无评论</td></tr>'}</tbody></table>`;
}

function renderReviews(reviews) {
  const rows = reviews.map((r) =>
    `<tr><td>${fmtTime(r.created_at)}</td><td>${esc(r.reviewer)}</td><td>${r.decision === "approve" ? "通过" : "退回"}</td><td>${esc(r.comment) || '<span class="muted">—</span>'}</td></tr>`).join("");
  $("reviewList").innerHTML = `<table><thead><tr><th>时间</th><th>复核人</th><th>结论</th><th>意见</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4" class="muted">暂无复核记录</td></tr>'}</tbody></table>`;
}

function renderDeliveries() {
  const rows = state.deliveries.map((d) => `
    <tr>
      <td class="num">${d.id}</td>
      <td class="num">${d.version_id}</td>
      <td>${d.supersedes_version_id ? `取代交付 #${d.supersedes_version_id}` : '<span class="muted">首次交付</span>'}</td>
      <td><code>${esc(d.snapshot_hash.slice(0, 16))}…</code></td>
      <td>${esc(d.delivered_by)}</td>
      <td>${fmtTime(d.created_at)}</td>
      <td><details><summary>清单</summary><pre class="result">${esc(d.manifest)}</pre></details></td>
    </tr>`).join("");
  $("deliveryList").innerHTML = `<table><thead><tr><th>交付</th><th>版本</th><th>取代</th><th>快照哈希</th><th>交付人</th><th>时间</th><th>内容</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="7" class="muted">暂无交付</td></tr>'}</tbody></table>`;
}

async function saveCue() {
  const out = $("cueResult");
  try {
    const payload = JSON.parse($("cueInput").value);
    const saved = await api(`/api/versions/${state.current.id}/cues`, {
      method: "POST", headers: authHeaders(), body: JSON.stringify(payload),
    });
    out.className = "result ok";
    out.textContent = `已保存字幕 #${saved.id}，revision=${saved.version_revision}`;
    await openVersion(state.current.id);
  } catch (e) {
    out.className = "result error";
    out.textContent = e.message;
  }
}

async function addComment() {
  try {
    await api(`/api/versions/${state.current.id}/comments`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ time_ms: Number($("commentTime").value), body: $("commentBody").value }),
    });
    await openVersion(state.current.id);
  } catch (e) {
    alert(e.message);
  }
}

async function action(name) {
  const out = $("stateResult");
  try {
    const result = await api(`/api/versions/${state.current.id}/${name}`, {
      method: "POST", headers: authHeaders(), body: "{}",
    });
    out.className = "result ok";
    out.textContent = `状态已更新为 ${result.status}`;
    await Promise.all([onProjectChange(), openVersion(state.current.id)]);
    state.deliveries = (await api("/api/deliveries")).deliveries;
    renderDeliveries();
  } catch (e) {
    out.className = "result error";
    out.textContent = e.message;
  }
}

async function review(decision) {
  const out = $("stateResult");
  try {
    const result = await api(`/api/versions/${state.current.id}/review`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ decision, comment: "" }),
    });
    out.className = "result ok";
    out.textContent = `复核完成，状态为 ${result.status}`;
    await Promise.all([onProjectChange(), openVersion(state.current.id)]);
  } catch (e) {
    out.className = "result error";
    out.textContent = e.message;
  }
}

$("newLanguage").addEventListener("input", renderParentOptions);
refreshAll();
