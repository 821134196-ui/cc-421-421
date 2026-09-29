/* 工装夹具系统前端（原生 JS，无依赖） */
'use strict';

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? undefined : { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || '请求失败'), { code: data.code });
  return data;
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const STATUS_TEXT = {
  available: '可用', locked: '已领用', inspecting: '待点检',
  repairing: '维修中', pending_scrap: '待报废', scrapped: '已报废',
};
const DISP_TEXT = {
  deformation: '夹具变形', missing_pin: '定位销缺失', over_life: '超寿命',
  transfer: '跨线调拨', overdue: '归还逾期',
};
const DISP_STATUS = { open: '待处理', done: '已处置', scrapped: '已报废', rejected: '已驳回' };

let META = { lines: [], stations: [], models: [], employees: [] };
const byId = (arr, id) => arr.find((x) => x.id === id);
const stationLine = (sid) => {
  const st = byId(META.stations, sid);
  return st ? byId(META.lines, st.line_id) : null;
};

/* ---------------------------------------------------------- 等轴测夹具小图 */
function isoIcon(status) {
  const colors = {
    available: ['#3f8f5b', '#8fd1a6'],
    locked: ['#d9a800', '#f5c400'],
    inspecting: ['#3d6e9e', '#8fb6d8'],
    repairing: ['#c64033', '#ef8d83'],
    pending_scrap: ['#c64033', '#ef8d83'],
    scrapped: ['#6b727b', '#a7adb5'],
  };
  const [dark, light] = colors[status] || colors.available;
  return `<svg class="fx-iso" width="64" height="52" viewBox="0 0 64 52">
    <polygon points="32,4 58,16 32,28 6,16" fill="${light}" stroke="${dark}" stroke-width="1.4"/>
    <polygon points="6,16 32,28 32,46 6,34" fill="${dark}" opacity=".82"/>
    <polygon points="58,16 32,28 32,46 58,34" fill="${dark}" opacity=".55"/>
    <rect x="26" y="10" width="4" height="13" rx="1" fill="#1c1f23" opacity=".75"/>
    <rect x="34" y="10" width="4" height="13" rx="1" fill="#1c1f23" opacity=".75"/>
    <circle cx="22" cy="15" r="1.7" fill="#1c1f23"/>
    <circle cx="42" cy="15" r="1.7" fill="#1c1f23"/>
    ${status === 'repairing' || status === 'pending_scrap'
      ? `<path d="M44 30 l8 8 m0 -8 l-8 8" stroke="#fff" stroke-width="2.4"/>` : ''}
  </svg>`;
}

function lifeBar(f) {
  const cls = f.use_count >= f.life_threshold ? 'over'
    : f.life_pct >= 80 ? 'warn' : '';
  return `<div class="life">
    <div class="life-bar"><div class="life-fill ${cls}" style="width:${Math.max(f.life_pct, 2)}%"></div></div>
    <div class="life-label">
      <span>累计 <b>${f.use_count}</b> / 阈值 ${f.life_threshold}</span>
      <span>${f.life_pct}%${f.life_pct >= 100 ? ' · 已超寿命' : ` · 余 ${f.life_remaining}`}</span>
    </div></div>`;
}

/* ---------------------------------------------------------- 提醒条 */
function renderReminders() {
  const rs = META.reminders || [];
  const bar = $('reminder-bar');
  if (!rs.length) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  bar.innerHTML = `<b>${rs.length}</b> 条系统提醒：` +
    rs.slice(0, 6).map((r) => `<span class="reminder-item">${esc(r.message)}</span>`).join('');
}

/* ---------------------------------------------------------- 夹具台账 */
async function loadLedger() {
  const line = $('ledger-line').value;
  const status = $('ledger-status').value;
  const q = new URLSearchParams();
  if (line) q.set('line', line);
  if (status) q.set('status', status);
  const list = await api(`/api/fixtures?${q}`);
  $('ledger-grid').innerHTML = list.length ? list.map((f) => `
    <article class="fx-card s-${f.status}">
      <div class="fx-head">
        ${isoIcon(f.status)}
        <div class="fx-title">
          <div class="fx-code">${esc(f.code)}</div>
          <div class="fx-name">${esc(f.name)}</div>
        </div>
        <span class="badge ${f.status}">${f.status_text}</span>
      </div>
      <div class="fx-spec">${esc(f.spec)}</div>
      ${lifeBar(f)}
      <div class="fx-meta">
        <span>工位 <b>${f.station ? esc(f.station.code) : '—'}</b> · ${esc(f.line ? f.line.code : '')}</span>
        <span>${f.models.map((m) => esc(m.code)).join(' / ')}</span>
      </div>
      <div class="fx-meta">
        <span>保养周期 ${f.maintenance_cycle_days} 天${f.next_maintenance_at
          ? ` · 下次 ${f.next_maintenance_at.slice(0, 10)}` : ''}</span>
        <span>${f.latest_inspection
          ? `最近点检 ${f.latest_inspection.result === 'pass' ? '合格' : '不合格'}`
          : '无点检记录'}</span>
      </div>
    </article>`).join('') : '<div class="empty">没有符合条件的夹具</div>';
}

/* ---------------------------------------------------------- 工位匹配 */
function modelOptions(select, models, blank) {
  select.innerHTML = (blank ? `<option value="">${blank}</option>` : '') +
    models.map((m) => `<option value="${m.id}">${esc(m.code)} · ${esc(m.name)}</option>`).join('');
}

async function loadMatch() {
  const sid = $('match-station').value;
  $('match-model').innerHTML = '<option value="">请先选工位</option>';
  if (!sid) return;
  const view = await api(`/api/stations/${sid}`);
  modelOptions($('match-model'), view.allowed_models, '选择机型');
}

async function runMatch() {
  const sid = $('match-station').value;
  const mid = $('match-model').value;
  const box = $('match-result');
  const cross = $('crossline-result');
  if (!sid || !mid) { box.innerHTML = '<p class="match-empty">请选择工位和机型。</p>'; return; }
  try {
    const view = await api(`/api/stations/${sid}?model=${mid}`);
    const st = view.station;
    box.innerHTML = `<p>工位 <span class="chip">${esc(st.code)}</span>
      ${esc(st.name)}（${esc(st.line_code)}）适配机型校验通过，现场可用夹具：</p>` +
      (view.matches.length ? view.matches.map((f) => `
        <div class="item-card st-closed">
          <div class="item-main">
            <div class="item-code">${esc(f.code)}</div>
            <div class="item-line">${esc(f.name)} · 寿命 ${f.use_count}/${f.life_threshold}</div>
          </div>
          <div class="item-actions">
            <button class="btn btn-sm btn-primary"
              onclick="quickApply(${st.id}, ${mid}, ${f.id}, false)">去申请</button>
          </div>
        </div>`).join('')
        : '<p class="match-empty">本工位暂无可用夹具，可查看下方跨线候选。</p>');

    const others = await api(`/api/stations/${sid}/crossline?model=${mid}`);
    cross.innerHTML = others.length ? `<h3 class="section-title">其他产线候选（跨线须主任审批调拨）</h3>` +
      others.map((f) => `
        <div class="item-card">
          <div class="item-main">
            <div class="item-code">${esc(f.code)}</div>
            <div class="item-line">${esc(f.name)} · 属 ${esc(f.line.code)}
             工位 ${esc(f.station.code)} · 寿命 ${f.use_count}/${f.life_threshold}</div>
          </div>
          <div class="item-actions">
            <button class="btn btn-sm"
              onclick="quickApply(${st.id}, ${mid}, ${f.id}, true)">申请跨线调拨</button>
          </div>
        </div>`).join('') : '';
  } catch (e) {
    box.innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
    cross.innerHTML = '';
  }
}

window.quickApply = (stationId, modelId, fixtureId, isTransfer) => {
  switchView('queue');
  $('apply-station').value = stationId;
  $('apply-model').value = modelId;
  $('apply-fixture').value = isTransfer ? '' : fixtureId;
  if (isTransfer) {
    // 跨线用隐藏字段传递
    $('apply-fixture').dataset.transfer = fixtureId;
  } else {
    delete $('apply-fixture').dataset.transfer;
  }
  const msg = $('apply-msg');
  msg.className = 'msg';
  msg.textContent = isTransfer
    ? '已带入跨线调拨夹具，选择申请人后提交将生成调拨处置单。'
    : '已带入工位/机型/夹具，选择申请人后提交申请。';
};

/* ---------------------------------------------------------- 申请队列 */
function reqBadge(r) {
  const map = { pending: '待领用', locked: '已领用', inspecting: '待点检',
    closed: '已闭环', failed: '点检不合格', cancelled: '已取消' };
  return `<span class="badge ${r.status === 'closed' ? 'available'
    : r.status === 'failed' ? 'repairing'
    : r.status === 'locked' ? 'locked' : 'inspecting'}">${map[r.status] || r.status}</span>`;
}

async function submitApply() {
  const msg = $('apply-msg');
  try {
    const payload = {
      applicant: $('apply-applicant').value,
      station_id: Number($('apply-station').value),
      model_id: Number($('apply-model').value),
      note: $('apply-note').value,
    };
    const transferId = $('apply-fixture').dataset.transfer;
    if (transferId) payload.transfer_fixture_id = Number(transferId);
    else if ($('apply-fixture').value) payload.fixture_id = Number($('apply-fixture').value);
    const r = await api('/api/requests', { method: 'POST', body: JSON.stringify(payload) });
    msg.className = 'msg ok';
    msg.textContent = `申请 ${r.code} 已提交` +
      (r.transfer_disposition ? `，跨线调拨单 ${r.transfer_disposition.code} 待主任审批` : '');
    $('apply-note').value = '';
    delete $('apply-fixture').dataset.transfer;
    loadQueue();
  } catch (e) { msg.className = 'msg err'; msg.textContent = e.message; }
}

async function loadQueue() {
  const list = await api('/api/requests');
  $('queue-list').innerHTML = list.length ? list.map((r) => {
    let action = '';
    if (r.status === 'pending') {
      if (r.transfer_disp_status === 'open') {
        action = '<span class="badge inspecting">跨线调拨待审批</span>';
      } else {
        action = `<button class="btn btn-sm btn-primary"
          onclick="goCheckout('${esc(r.code)}', '${esc(r.fixture_code || '')}')">去领用</button>`;
      }
    } else if (r.status === 'inspecting') {
      action = `<button class="btn btn-sm" onclick="goInspect(${r.id})">去点检</button>`;
    }
    return `<div class="item-card st-${r.status}">
      <div class="item-main">
        <div class="item-code">${esc(r.code)}</div>
        <div class="item-line">
          申请人 <b>${esc(r.applicant_name)}</b> ${r.frozen ? '<span class="badge repairing">已冻结</span>' : ''}
          · 工位 <b>${esc(r.station_code)}</b>（${esc(r.station_line_code)}）
          · 机型 <b>${esc(r.model_code)}</b>
          ${r.fixture_code ? `· 夹具 <b>${esc(r.fixture_code)}</b>` : ''}
          · 应还 ${r.due_at ? esc(r.due_at.replace('T', ' ')) : ''}
        </div>
        ${r.note ? `<div class="item-line">${esc(r.note)}</div>` : ''}
      </div>
      <div class="item-actions">${reqBadge(r)}${action}</div>
    </div>`;
  }).join('') : '<div class="empty">暂无申请</div>';
}

window.goCheckout = (reqCode, fixtureCode) => {
  switchView('scan');
  $('co-request').value = reqCode;
  $('co-fixture').value = fixtureCode;
  const op = META.employees.find((e) => e.code === 'OP-101');
  if (op) $('co-operator').value = op.code;
};

/* ---------------------------------------------------------- 借还扫码 */
async function doCheckout() {
  const msg = $('co-msg');
  try {
    const reqCode = $('co-request').value.trim().toUpperCase();
    const fxCode = $('co-fixture').value.trim().toUpperCase();
    const reqs = await api('/api/requests');
    const req = reqs.find((x) => x.code === reqCode);
    if (!req) throw new Error(`申请单 ${reqCode} 不存在`);
    const fixtures = await api('/api/fixtures');
    const fx = fixtures.find((x) => x.code === fxCode);
    if (!fx) throw new Error(`夹具编号 ${fxCode} 不存在`);
    const out = await api('/api/borrows/checkout', {
      method: 'POST',
      body: JSON.stringify({
        request_id: req.id, fixture_id: fx.id,
        operator: $('co-operator').value.trim().toUpperCase(),
      }),
    });
    msg.className = 'msg ok';
    msg.textContent = `✔ 领用成功：${out.fixture_code} 已锁定至工位 ${out.station_code}，操作人 ${out.operator}`;
    loadScan();
  } catch (e) { msg.className = 'msg err'; msg.textContent = '✘ ' + e.message; }
}

async function doReturn(borrowId, btn) {
  const delta = Number(btn.parentElement.querySelector('.ret-delta')?.value || 1);
  await api(`/api/borrows/${borrowId}/return`, {
    method: 'POST', body: JSON.stringify({ use_delta: Math.max(delta, 1) }),
  });
  loadScan();
}

let INSPECT_REQ = null;
window.goInspect = (requestId) => {
  switchView('scan');
  openInspect(requestId);
};

async function openInspect(requestId) {
  INSPECT_REQ = requestId;
  const reqs = await api('/api/requests');
  const r = reqs.find((x) => x.id === requestId);
  $('inspect-target').textContent = r ? `${r.code} · ${r.fixture_code || ''}` : `#${requestId}`;
  $('inspect-dock').classList.remove('hidden');
  $('inspect-dock').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function submitInspection() {
  const msg = $('in-msg');
  try {
    const fd = new FormData();
    fd.append('request_id', INSPECT_REQ);
    fd.append('inspector', $('in-inspector').value);
    fd.append('result', $('in-result').value);
    fd.append('defect', $('in-defect').value);
    fd.append('note', $('in-note').value);
    const photo = $('in-photo').files[0];
    if (photo) fd.append('photo', photo);
    const r = await api('/api/inspections', { method: 'POST', body: fd });
    msg.className = 'msg ok';
    let text = `点检单 ${r.inspection.code} 已提交（${r.inspection.result === 'pass' ? '合格' : '不合格'}）`;
    if (r.disposition) text += `；生成处置单 ${r.disposition.code}（${DISP_TEXT[r.disposition.type]}）`;
    if (r.repair) text += `；维修单 ${r.repair.code} 已开，借用人已冻结`;
    msg.textContent = text;
    $('in-note').value = '';
    $('in-photo').value = '';
    loadScan();
  } catch (e) { msg.className = 'msg err'; msg.textContent = '✘ ' + e.message; }
}

function employeeOptions(role) {
  return META.employees.filter((e) => !role || e.role === role)
    .map((e) => `<option value="${esc(e.code)}">${esc(e.code)} ${esc(e.name)}（${
      { operator: '操作员', maintainer: '维修工', inspector: '点检员', manager: '主任' }[e.role]}${
      e.frozen ? '·冻结' : ''}）</option>`).join('');
}

async function loadScan() {
  const borrows = await api('/api/borrows/active');
  const reqs = await api('/api/requests');
  const inspecting = reqs.filter((r) => r.status === 'inspecting');

  const rows = borrows.map((b) => `
    <div class="item-card st-locked">
      <div class="item-main">
        <div class="item-code">${esc(b.fixture_code)} <span class="dim">被 ${esc(b.borrower_name)} 持有</span></div>
        <div class="item-line">工位 <b>${esc(b.station_code)}</b> · 申请 ${esc(b.request_code)}
          · 借出 ${esc(b.checked_out_at.replace('T', ' '))} · 应还 ${esc(b.due_at.replace('T', ' '))}</div>
      </div>
      <div class="item-actions">
        <span class="badge locked">已锁定</span>
        <label style="font-size:11px">使用次数
          <input class="ret-delta" type="number" value="1" min="1" style="width:64px;min-width:0">
        </label>
        <button class="btn btn-sm" onclick="doReturn(${b.id}, this)">归还 → 待点检</button>
      </div>
    </div>`).join('');

  const inspRows = inspecting.map((r) => `
    <div class="item-card st-inspecting">
      <div class="item-main">
        <div class="item-code">${esc(r.fixture_code || '')} 待点检</div>
        <div class="item-line">申请 ${esc(r.code)} · ${esc(r.applicant_name)} 已归还，点检合格后恢复可用</div>
      </div>
      <div class="item-actions">
        <button class="btn btn-sm btn-primary" onclick="openInspect(${r.id})">打开点检</button>
      </div>
    </div>`).join('');

  $('active-borrows').innerHTML = (rows || inspRows)
    ? (rows ? `<h3 class="section-title">在借中</h3>${rows}` : '') +
      (inspRows ? `<h3 class="section-title">待点检</h3>${inspRows}` : '')
    : '<div class="empty">当前没有在借或待点检的夹具</div>';

  loadRepairs();
}

/* ---------------------------------------------------------- 维修复核 */
async function loadRepairs() {
  const repairs = await api('/api/repairs');
  $('repair-list').innerHTML = repairs.length ? repairs.map((r) => {
    let action = '';
    if (r.status === 'open' && !r.repairman_id) {
      action = `<select class="rep-man">${employeeOptions('maintainer')}</select>
        <button class="btn btn-sm" onclick="repairDone(${r.id}, this)">填报完成</button>`;
    } else if (r.status === 'open' && r.repairman_id) {
      action = `<span class="badge repairing">复核打回重修</span>
        <button class="btn btn-sm" onclick="repairDone(${r.id}, this, true)">重新完成</button>`;
    } else if (r.status === 'in_review') {
      action = `<select class="rep-rev">${employeeOptions('inspector')}${employeeOptions('maintainer')}</select>
        <button class="btn btn-sm btn-ok" onclick="repairReview(${r.id}, true, this)">复核通过</button>
        <button class="btn btn-sm btn-danger" onclick="repairReview(${r.id}, false, this)">打回</button>`;
    } else {
      action = `<span class="badge available">已闭环</span>`;
    }
    return `<div class="item-card st-${r.status === 'done' ? 'closed' : 'failed'}">
      <div class="item-main">
        <div class="item-code">${esc(r.code)} · ${esc(r.fixture_code)}</div>
        <div class="item-line">${esc(r.description)}
          ${r.repairman_name ? `· 维修 <b>${esc(r.repairman_name)}</b>` : ''}
          ${r.reviewer_name ? `· 复核 <b>${esc(r.reviewer_name)}</b>（${r.review_result === 'pass' ? '通过' : '打回'}）` : ''}</div>
      </div>
      <div class="item-actions">${action}</div>
    </div>`;
  }).join('') : '<div class="empty">暂无维修记录</div>';
}

async function repairDone(id, btn, again) {
  const man = btn.parentElement.querySelector('select')?.value || 'MT-301';
  await api(`/api/repairs/${id}/done`, {
    method: 'POST',
    body: JSON.stringify({ repairman: man, description: again ? '返修后重新提交复核' : '维修完成，定位/形变已校正' }),
  });
  loadScan();
}
async function repairReview(id, passed, btn) {
  const rev = btn.parentElement.querySelector('select').value;
  try {
    await api(`/api/repairs/${id}/review`, {
      method: 'POST',
      body: JSON.stringify({ reviewer: rev, passed, note: passed ? '复核合格' : '仍有偏差' }),
    });
    loadScan();
  } catch (e) { alert(e.message); }
}

/* ---------------------------------------------------------- 保养 */
async function loadMaint() {
  const plans = await api('/api/maintenance');
  $('maint-list').innerHTML = plans.length ? plans.map((p) => {
    const overdue = !p.done_at && p.due_at.slice(0, 10) <= new Date().toISOString().slice(0, 10);
    return `<div class="item-card ${p.done_at ? 'st-closed' : 'st-pending'}">
      <div class="item-main">
        <div class="item-code">${esc(p.fixture_code)} · ${esc(p.fixture_name)}</div>
        <div class="item-line">应完成 <b>${esc(p.due_at.replace('T', ' '))}</b>
          ${overdue ? '<span class="badge repairing">已逾期</span>' : ''}
          ${p.done_at ? `· 已于 ${esc(p.done_at.replace('T', ' '))} 完成：${esc(p.note)}` : '· 待执行'}</div>
      </div>
      <div class="item-actions">${p.done_at ? '<span class="badge available">已完成</span>' : `
        <select class="mt-who">${employeeOptions('maintainer')}${employeeOptions('inspector')}</select>
        <button class="btn btn-sm btn-primary" onclick="maintDone(${p.id}, this)">完成保养</button>`}
      </div>
    </div>`;
  }).join('') : '<div class="empty">暂无保养计划（点击“立即扫描到期”生成）</div>';
}
async function maintDone(id, btn) {
  await api(`/api/maintenance/${id}/complete`, {
    method: 'POST',
    body: JSON.stringify({ employee: btn.parentElement.querySelector('select').value, note: '按周期完成点检保养' }),
  });
  loadMaint();
}

/* ---------------------------------------------------------- 报废 / 调拨 / 处置 */
function latestInspCell(insp) {
  if (!insp) return '<div class="cell"><div class="k">最近点检</div><div class="v" style="font-size:13px">无记录</div></div>';
  return `<div class="cell"><div class="k">最近点检</div>
    <div class="v" style="font-size:14px">${insp.result === 'pass'
      ? '<span style="color:var(--ok)">合格</span>'
      : '<span style="color:var(--danger)">不合格</span>'}
      <small>${esc((insp.defect ? DISP_TEXT[insp.defect] + ' ' : ''))}${esc(insp.created_at.replace('T', ' ').slice(0, 16))}</small></div></div>`;
}

function scrapCard(d) {
  return `<div class="item-card st-failed" style="display:block">
    <div class="item-main">
      <div class="item-code">${esc(d.code)} · ${esc(d.fixture_code)} · ${esc(d.fixture_name)}</div>
      <div class="item-line">${esc(d.detail)}</div>
    </div>
    <div class="scrap-fixed">
      <div class="cell"><div class="k">累计使用次数</div>
        <div class="v ${d.use_count >= d.life_threshold ? 'over' : ''}">${d.use_count} <small>次</small></div></div>
      <div class="cell"><div class="k">寿命阈值</div><div class="v">${d.life_threshold} <small>次</small></div></div>
      ${latestInspCell(d.latest_inspection)}
    </div>
    <div class="item-actions">
      <input class="mgr-code" value="MGR-001" style="min-width:120px">
      <button class="btn btn-sm btn-danger" onclick="scrapDecide(${d.id}, true, this)">批准报废</button>
      <button class="btn btn-sm btn-ok" onclick="scrapDecide(${d.id}, false, this)">驳回（大修后复用）</button>
    </div>
  </div>`;
}

async function loadScrap() {
  const disps = await api('/api/dispositions');
  const openScrap = disps.filter((d) => d.type === 'over_life' && d.status === 'open');
  const transfers = disps.filter((d) => d.type === 'transfer');
  $('scrap-list').innerHTML = openScrap.length
    ? openScrap.map(scrapCard).join('')
    : '<div class="empty">没有待审批的超寿命报废单</div>';

  $('transfer-list').innerHTML = transfers.length ? transfers.map((d) => `
    <div class="item-card ${d.status === 'done' ? 'st-closed' : d.status === 'rejected' ? 'st-cancelled' : ''}">
      <div class="item-main">
        <div class="item-code">${esc(d.code)} · ${esc(d.fixture_code)}</div>
        <div class="item-line">${esc(d.detail)} · 目标工位 <b>${esc(d.target_station_code || '')}</b>
          · 状态 <b>${DISP_STATUS[d.status]}</b>${d.decided_by_name ? `（${esc(d.decided_by_name)}）` : ''}</div>
      </div>
      <div class="item-actions">${d.status === 'open' ? `
        <input class="mgr-code" value="MGR-001" style="min-width:120px">
        <button class="btn btn-sm btn-primary" onclick="transferDecide(${d.id}, true, this)">批准调拨</button>
        <button class="btn btn-sm btn-danger" onclick="transferDecide(${d.id}, false, this)">驳回</button>`
        : `<span class="badge ${d.status === 'done' ? 'available' : 'scrapped'}">${DISP_STATUS[d.status]}</span>`}
      </div>
    </div>`).join('') : '<div class="empty">暂无跨线调拨单</div>';

  $('disp-list').innerHTML = disps.filter((d) => ['deformation', 'missing_pin', 'overdue'].includes(d.type))
    .map((d) => `
    <div class="item-card st-${d.status === 'done' ? 'closed' : d.status === 'scrapped' ? 'failed' : 'pending'}">
      <div class="item-main">
        <div class="item-code">${esc(d.code)} · ${esc(d.fixture_code)}</div>
        <div class="item-line"><span class="badge ${d.type === 'overdue' ? 'locked' : 'repairing'}">${DISP_TEXT[d.type]}</span>
          ${esc(d.detail)} · ${DISP_STATUS[d.status]}</div>
      </div>
    </div>`).join('') || '<div class="empty">暂无变形/定位销缺失/逾期处置单</div>';
}

async function scrapDecide(id, approve, btn) {
  try {
    await api(`/api/dispositions/${id}/scrap`, {
      method: 'POST',
      body: JSON.stringify({ manager: btn.parentElement.querySelector('.mgr-code').value,
        approve, note: approve ? '超寿命批准报废' : '安排大修降级复用' }),
    });
    loadScrap();
  } catch (e) { alert(e.message); }
}
async function transferDecide(id, approve, btn) {
  try {
    await api(`/api/dispositions/${id}/transfer`, {
      method: 'POST',
      body: JSON.stringify({ manager: btn.parentElement.querySelector('.mgr-code').value,
        approve, note: approve ? '同意跨线' : '不同意跨线' }),
    });
    loadScrap();
  } catch (e) { alert(e.message); }
}

/* ---------------------------------------------------------- 视图切换 */
const LOADERS = {
  ledger: loadLedger, match: loadMatch, queue: loadQueue,
  scan: loadScan, maint: loadMaint, scrap: loadScrap,
};
function switchView(name) {
  document.querySelectorAll('.tabs button').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) =>
    v.classList.toggle('hidden', v.id !== `view-${name}`));
  LOADERS[name]?.();
}

/* ---------------------------------------------------------- 初始化 */
async function init() {
  META = await api('/api/meta');
  renderReminders();

  $('ledger-line').innerHTML = '<option value="">全部产线</option>' +
    META.lines.map((l) => `<option value="${l.id}">${esc(l.code)} ${esc(l.name)}</option>`).join('');
  $('match-station').innerHTML = '<option value="">选择工位</option>' +
    META.stations.map((s) => `<option value="${s.id}">${esc(s.code)} ${esc(s.name)}</option>`).join('');
  $('apply-station').innerHTML = META.stations.map((s) =>
    `<option value="${s.id}">${esc(s.code)} ${esc(s.name)}</option>`).join('');
  $('apply-applicant').innerHTML = employeeOptions('operator');
  $('in-inspector').innerHTML = employeeOptions('inspector');

  const allFixtures = await api('/api/fixtures');
  $('apply-fixture').innerHTML = '<option value="">不指定，按工位匹配</option>' +
    allFixtures.map((f) => `<option value="${f.id}">${esc(f.code)} ${esc(f.name)}（${esc(f.status_text)}）</option>`).join('');

  const refreshModels = () => {
    const sid = Number($('apply-station').value);
    api(`/api/stations/${sid}`).then((v) => modelOptions($('apply-model'), v.allowed_models));
  };
  $('apply-station').addEventListener('change', refreshModels);
  refreshModels();

  document.querySelectorAll('.tabs button').forEach((b) =>
    b.addEventListener('click', () => switchView(b.dataset.view)));
  $('ledger-refresh').onclick = loadLedger;
  $('ledger-line').onchange = loadLedger;
  $('ledger-status').onchange = loadLedger;
  $('match-station').onchange = loadMatch;
  $('match-go').onclick = runMatch;
  $('queue-refresh').onclick = loadQueue;
  $('apply-submit').onclick = submitApply;
  $('scan-refresh').onclick = loadScan;
  $('co-go').onclick = doCheckout;
  $('in-submit').onclick = submitInspection;
  $('in-result').onchange = (e) => $('defect-wrap').style.opacity = e.target.value === 'fail' ? '1' : '.4';
  $('maint-refresh').onclick = loadMaint;
  $('maint-scan').onclick = async () => {
    const r = await api('/api/maintenance/scan', { method: 'POST' });
    META = await api('/api/meta'); renderReminders();
    alert(`扫描完成：新增保养计划 ${r.maintenance_created} 条，逾期处置 ${r.overdue_created} 条`);
    loadMaint();
  };
  $('scrap-refresh').onclick = loadScrap;

  loadLedger();
}
init();
