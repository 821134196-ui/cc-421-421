/* 工装夹具借用与寿命记录系统 — 前端 SPA（原生 JS，零依赖） */
const App = (() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTs = (t) => t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";

  let meta = null;
  let currentPage = "ledger";

  // ------------------------------------------------------------ api
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  const actor = () => $("#actor").value;
  const post = (path, extra = {}) =>
    api(path, { method: "POST", body: { actor_id: actor(), ...extra } });

  function toast(msg, isErr = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.className = "toast", 3200);
  }
  const action = async (fn, okMsg, reload = true) => {
    try { await fn(); if (okMsg) toast(okMsg); if (reload) render(); }
    catch (e) { toast(e.message, true); }
  };

  // ------------------------------------------------------------ meta helpers
  const stationName = id => meta.stations.find(s => s.id === id)?.name || id;
  const modelName = id => meta.models.find(m => m.id === id)?.name || id;
  const lineName = id => meta.lines.find(l => l.id === id)?.name || id;
  const staffName = id => meta.staff.find(s => s.id === id)?.name || id || "—";
  const fxStatus = s => meta.status_labels[s] || s;
  const rqStatus = s => meta.request_status_labels[s] || s;
  const typeLabel = t => meta.type_labels[t] || t;

  // ------------------------------------------------------------ isometric icon
  function isoIcon(fx) {
    const palettes = {
      available: { top: "#4a505b", edge: "#6b7280", sideR: "#33373e", sideL: "#2b2f36", pin: "#ffd23f", accent: "#ffd23f" },
      reserved:  { top: "#3d4c5e", edge: "#5aa7e8", sideR: "#2c3744", sideL: "#262f3a", pin: "#7fc0f2", accent: "#7fc0f2" },
      in_use:    { top: "#6f5f1c", edge: "#ffd23f", sideR: "#4d4214", sideL: "#3d3510", pin: "#ffe88a", accent: "#ffd23f" },
      in_repair: { top: "#5e3a3d", edge: "#e5484d", sideR: "#44292c", sideL: "#382225", pin: "#ff8d91", accent: "#e5484d" },
      scrapped:  { top: "#343941", edge: "#4a505b", sideR: "#262a30", sideL: "#20242a", pin: "#6b7280", accent: "#6b7280" },
    };
    const p = palettes[fx.status] || palettes.available;
    const pin = (cx, cy) =>
      `<ellipse cx="${cx}" cy="${cy}" rx="4.6" ry="2.6" fill="${p.pin}"/>
       <ellipse cx="${cx}" cy="${cy - 1.4}" rx="4.6" ry="2.4" fill="${p.edge}" opacity=".55"/>`;
    const frozen = fx.frozen ? `<path d="M52 14 L52 50 M36 26 L68 38 M68 26 L36 38"
        stroke="#e5484d" stroke-width="2.4" stroke-linecap="round"/>` : "";
    const scrapX = fx.status === "scrapped"
      ? `<path d="M40 22 L64 44 M64 22 L40 44" stroke="#8a9099" stroke-width="3" stroke-linecap="round"/>` : "";
    return `<svg class="iso" viewBox="0 0 104 76" xmlns="http://www.w3.org/2000/svg">
      <!-- side faces -->
      <polygon points="98,30 98,44 52,66 52,52" fill="${p.sideR}"/>
      <polygon points="6,30 6,44 52,66 52,52" fill="${p.sideL}"/>
      <!-- top plate -->
      <polygon points="52,8 98,30 52,52 6,30" fill="${p.top}" stroke="${p.edge}" stroke-width="1.6"/>
      <!-- center clamp diamond -->
      <polygon points="52,24 62,30 52,36 42,30" fill="none" stroke="${p.accent}" stroke-width="1.6"/>
      ${pin(30, 22)}${pin(74, 22)}${pin(30, 40)}${pin(74, 40)}
      ${frozen}${scrapX}
    </svg>`;
  }

  // ------------------------------------------------------------ life bar
  function lifeBar(fx) {
    const pct = Math.min(100, fx.life_pct);
    const cls = fx.over_life ? "over" : pct >= 80 ? "warn" : "";
    return `<div class="life">
      <div class="bar"><div class="fill ${cls}" style="width:${pct}%"></div></div>
      <span class="num ${fx.over_life ? "over" : ""}">${fx.use_count}/${fx.life_threshold}</span>
    </div>`;
  }
  const badge = (cls, txt) => `<span class="badge ${cls}">${esc(txt)}</span>`;

  // ============================================================ pages
  // ---------- 台账
  async function ledger() {
    const list = await api("/api/fixtures");
    const frozenN = list.filter(f => f.frozen).length;
    const overN = list.filter(f => f.over_life).length;
    const busyN = list.filter(f => ["reserved", "in_use", "in_repair"].includes(f.status)).length;
    $("#page").innerHTML = `
      <div class="page-head">
        <h2>夹具台账</h2>
        <span class="hint">共 ${list.length} 件 · 占用/维修 ${busyN} · 冻结 ${frozenN} · 超寿命 ${overN}</span>
      </div>
      <div class="section-toggle">
        ${["", "available", "reserved", "in_use", "in_repair", "scrapped"].map(s =>
          `<button class="${s === "" ? "on" : ""}" data-f="${s}">${s ? fxStatus(s) : "全部"}</button>`).join("")}
      </div>
      <div class="fx-grid" id="fxgrid"></div>`;
    const grid = $("#fxgrid");
    const renderGrid = (status) => {
      const items = list.filter(f => !status || f.status === status);
      grid.innerHTML = items.length ? items.map(f => `
        <div class="fx-card" data-id="${f.id}">
          ${isoIcon(f)}
          <div class="body">
            <div class="row1">
              <span class="name">${esc(f.name)}</span>
              ${badge(f.status, fxStatus(f.status))}${f.frozen ? badge("frozen", "冻结") : ""}
            </div>
            <div class="spec" title="${esc(f.spec)}">${esc(f.spec)}</div>
            <div class="stn">📍 <span class="code">${esc(f.id)}</span> ·
              ${esc(f.station_name || "未挂工位")} · ${esc(f.line_name)}</div>
            ${lifeBar(f)}
            <div class="stn" style="margin-top:6px">
              ${f.models.map(m => `<span class="pill y">${esc(m)}</span>`).join("")}
            </div>
          </div>
        </div>`).join("") : `<div class="empty">该状态下暂无夹具</div>`;
      $$(".fx-card", grid).forEach(c => c.onclick = () => fixtureDetail(c.dataset.id));
    };
    renderGrid("");
    $$(".section-toggle button").forEach(b => b.onclick = () => {
      $$(".section-toggle button").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      renderGrid(b.dataset.f);
    });
  }

  async function fixtureDetail(id) {
    const f = await api(`/api/fixtures/${id}`);
    $("#page").innerHTML = `
      <div class="page-head"><h2>夹具档案 <span class="code">${esc(f.id)}</span></h2>
        <button class="btn ghost" id="back">← 返回台账</button></div>
      <div class="panel">
        <div style="display:flex;gap:20px;align-items:center">
          ${isoIcon(f)}
          <div style="flex:1">
            <h3 style="margin:0"><span class="tag">●</span>${esc(f.name)}</h3>
            <div class="dim" style="margin:4px 0 8px">${esc(f.spec)}</div>
            ${lifeBar(f)}
          </div>
          <div style="text-align:right">
            ${badge(f.status, fxStatus(f.status))}${f.frozen ? badge("frozen", "冻结") : ""}
            <div class="dim" style="margin-top:6px">剩余寿命 <b style="color:${f.remaining ? "var(--cool-white)" : "#ff8d91"}">${f.remaining}</b> 次</div>
          </div>
        </div>
        <hr style="border-color:var(--graphite-600);margin:14px 0">
        <div class="detail-grid">
          <span class="k">当前工位</span><span>${esc(f.station_name || "—")}（${esc(f.line_name)}）</span>
          <span class="k">适配机型</span><span>${f.models.map(m => `<span class="pill y">${esc(m)} ${esc(modelName(m))}</span>`).join("")}</span>
          <span class="k">可服务工位</span><span>${f.serving_stations.map(s =>
            `<span class="pill">${esc(s.id)} ${esc(s.name)} · ${esc(s.model_id)}</span>`).join("") || "—"}</span>
          <span class="k">保养周期</span><span>每 ${f.maintenance_every} 次 ·
            下次计划点 <b class="code">${f.next_maintenance_at ?? "—"}</b></span>
        </div>
      </div>
      <div class="panel"><h3><span class="tag">●</span>最近点检</h3>
        ${f.inspections.length ? `<table><thead><tr><th>单号</th><th>结果</th><th>发现</th><th>点检人</th><th>照片</th><th>时间</th></tr></thead><tbody>
          ${f.inspections.map(i => `<tr>
            <td class="code">${esc(i.id)}</td>
            <td>${i.result === "pass" ? badge("ok", "合格") : badge("bad", "不合格")}</td>
            <td>${esc(i.findings) || "—"}</td>
            <td>${esc(i.inspector_name)}</td>
            <td>${i.photo_path ? `<a href="/photos/${esc(i.photo_path)}" target="_blank"
               style="color:var(--info)">查看照片</a>` : "—"}</td>
            <td class="dim">${fmtTs(i.created_at)}</td></tr>`).join("")}
        </tbody></table>` : `<div class="empty">暂无点检记录</div>`}
      </div>`;
    $("#back").onclick = render;
  }

  // ---------- 申请队列
  async function queue() {
    const list = await api("/api/requests");
    const tabs = ["", "pending", "approved", "picked", "returned", "inspected", "rejected"];
    $("#page").innerHTML = `
      <div class="page-head"><h2>借用申请队列</h2>
        <span class="hint">申请须同时匹配工位与机型；冻结/维修中的夹具不可预订</span></div>
      <div class="section-toggle">
        ${tabs.map(s => `<button class="${s === "" ? "on" : ""}" data-f="${s}">${s ? rqStatus(s) : "全部"}</button>`).join("")}
      </div>
      <div class="panel" style="padding:0;overflow-x:auto"><table id="rqtable">
        <thead><tr><th>申请号</th><th>夹具</th><th>工位 / 机型</th><th>申请人</th><th>状态</th>
          <th>期限</th><th>操作</th></tr></thead><tbody></tbody></table></div>`;
    const body = $("#rqtable tbody");
    const draw = (f) => {
      const items = list.filter(r => !f || r.status === f);
      body.innerHTML = items.length ? items.map(r => `
        <tr data-id="${r.id}">
          <td class="code">${esc(r.id)}</td>
          <td><b>${esc(r.fixture_name)}</b><div class="dim">${esc(r.fixture_id)}</div></td>
          <td>${esc(r.station_name)}<div class="dim">${esc(r.model_id)} ${esc(r.model_name)}</div></td>
          <td>${esc(r.applicant_name)}</td>
          <td>${badge(r.status === "picked" ? "in_use" : r.status === "approved" ? "reserved"
              : r.status === "returned" ? "warn" : r.status === "rejected" ? "gray"
              : r.status === "inspected" ? "ok" : "gray", rqStatus(r.status))}
            ${r.overdue ? badge("bad", "逾期") : ""}</td>
          <td class="dim">${fmtTs(r.due_at)}</td>
          <td>${actionsFor(r)}</td>
        </tr>`).join("") : `<tr><td colspan="7" class="empty">暂无申请</td></tr>`;
      bindRowActions(items);
    };
    function actionsFor(r) {
      const B = (cls, act, txt, extra = "") =>
        `<button class="btn tiny ${cls}" data-act="${act}" ${extra}>${txt}</button>`;
      switch (r.status) {
        case "pending": return B("", "approve", "批准") + B("ghost", "reject", "驳回");
        case "approved": return B("", "pickup", "扫码领用");
        case "picked": return B("ghost", "return", "归还") +
          (r.overdue ? badge("bad", "已生成逾期处置单") : "");
        case "returned": return B("", "inspect", "归还点检");
        default: return `<span class="dim">${esc(r.note || "")}</span>`;
      }
    }
    function bindRowActions(items) {
      $$("#rqtable tbody button").forEach(btn => {
        btn.onclick = async () => {
          const id = btn.closest("tr").dataset.id;
          const r = items.find(x => x.id === id);
          const act = btn.dataset.act;
          if (act === "approve") return action(() => post(`/api/requests/${id}/approve`), "已批准并锁定夹具");
          if (act === "reject") {
            const note = prompt("驳回原因（可空）", "");
            if (note === null) return;
            return action(() => post(`/api/requests/${id}/reject`, { note }), "已驳回");
          }
          if (act === "pickup") return action(() => post(`/api/requests/${id}/pickup`),
            `领用成功，夹具 ${r.fixture_id} 累计次数 +1`);
          if (act === "return") return action(() => post(`/api/requests/${id}/return`), "已归还，等待点检");
          if (act === "inspect") return inspectDialog(r);
        };
      });
    }
    draw("");
    $$(".section-toggle button").forEach(b => b.onclick = () => {
      $$(".section-toggle button").forEach(x => x.classList.remove("on"));
      b.classList.add("on"); draw(b.dataset.f);
    });
  }

  async function inspectDialog(r) {
    $("#page").insertAdjacentHTML("beforeend", inspectFormHtml(r));
    const dlg = $("#inspect-dlg");
    $("#insp-cancel").onclick = () => dlg.remove();
    $("#insp-photo").onchange = async () => {
      const file = $("#insp-photo").files[0];
      if (!file) return;
      const buf = await file.arrayBuffer();
      const name = `IN-${Date.now()}-${file.name.replace(/[^\w.\-]/g, "_")}`;
      await fetch(`/api/photos?name=${encodeURIComponent(name)}`, { method: "POST", body: buf });
      $("#insp-photo-path").value = name;
      toast("照片已保存到本地 data/photos/");
    };
    $("#insp-submit").onclick = () =>
      action(() => post(`/api/requests/${r.id}/inspect`, {
        result: $("#insp-result").value,
        findings: $("#insp-findings").value,
        photo_path: $("#insp-photo-path").value,
      }), $("#insp-result").value === "fail"
        ? "点检不合格：夹具冻结，已生成处置单并转维修"
        : "点检合格，夹具恢复可用");
  }
  function inspectFormHtml(r) {
    return `<div class="panel" id="inspect-dlg" style="border-color:var(--yellow);margin-top:16px">
      <h3><span class="tag">●</span>归还点检 · ${esc(r.fixture_id)} / ${esc(r.id)}</h3>
      <div class="form-row">
        <div class="field"><label>点检结果</label>
          <select id="insp-result"><option value="pass">合格</option><option value="fail">不合格</option></select></div>
        <div class="field" style="flex:1"><label>发现（含“变形”/“定位销”关键词将自动生成处置单）</label>
          <input id="insp-findings" placeholder="例：定位销缺失 1 处 / 压紧面变形"></div>
        <div class="field"><label>点检照片</label>
          <input type="file" id="insp-photo" accept=".svg,.png,.jpg,.jpeg" style="min-width:200px"></div>
        <input type="hidden" id="insp-photo-path" value="IN-SAMPLE.svg">
        <button class="btn" id="insp-submit">提交点检</button>
        <button class="btn ghost" id="insp-cancel">取消</button>
      </div>
      <div class="dim" style="margin-top:8px">未选择照片时使用内置样例照片 IN-SAMPLE.svg，存于本地 data/photos/。</div>
    </div>`;
  }

  // ---------- 工位匹配
  async function matchPage() {
    $("#page").innerHTML = `
      <div class="page-head"><h2>工位 × 机型匹配</h2>
        <span class="hint">先选工位与机型，再从适配夹具中发起借用申请</span></div>
      <div class="panel">
        <div class="form-row">
          <div class="field"><label>工位</label><select id="m-station">
            ${meta.stations.map(s => `<option value="${s.id}">${s.id} ${esc(s.name)}（${lineName(s.line_id)}）</option>`).join("")}
          </select></div>
          <div class="field"><label>机型</label><select id="m-model">
            ${meta.models.map(m => `<option value="${m.id}">${m.id} ${esc(m.name)}</option>`).join("")}
          </select></div>
          <button class="btn" id="m-go">匹配校验</button>
        </div>
      </div>
      <div id="m-result"></div>`;
    $("#m-go").onclick = async () => {
      const station = $("#m-station").value, model = $("#m-model").value;
      let d;
      try { d = await api(`/api/match?station=${station}&model=${model}`); }
      catch (e) { return toast(e.message, true); }
      $("#m-result").innerHTML = `
        <div class="panel">
          <h3><span class="tag">●</span>匹配结果
            <span class="${d.station_supports_model ? "match-ok" : "match-no"}" style="font-size:13px">
              ${d.station_supports_model ? "✔ 该工位生产此机型" : "✘ 该工位不生产此机型，申请将被拒绝"}</span></h3>
          <table><thead><tr><th></th><th>夹具编号</th><th>名称</th><th>所属线</th>
            <th>当前工位</th><th>状态</th><th>寿命</th><th>判定</th><th></th></tr></thead><tbody>
          ${d.fixtures.map(f => {
            const reason = !d.station_supports_model ? "工位不生产该机型"
              : !f.fits_station ? "跨线夹具" : f.status !== "available" ? fxStatus(f.status)
              : f.frozen ? "点检冻结" : f.over_life ? "已超寿命" : f.at_station ? "就在本工位" : "本线可调入";
            const usable = d.station_supports_model && f.fits_station &&
              f.status === "available" && !f.frozen && !f.over_life;
            return `<tr>
              <td>${isoIcon(f)}</td>
              <td class="code">${f.id}</td><td>${esc(f.name)}</td>
              <td>${esc(f.line_name)}</td>
              <td>${esc(f.station_name)}</td>
              <td>${badge(f.status, fxStatus(f.status))}${f.frozen ? badge("frozen", "冻结") : ""}</td>
              <td style="min-width:130px">${lifeBar(f)}</td>
              <td>${usable ? badge("ok", "可申请") : badge("gray", reason)}
                ${f.at_station ? badge("ok", "在位") : ""}</td>
              <td>${usable ? `<button class="btn tiny" data-fx="${f.id}">发起申请</button>` : ""}</td>
            </tr>`;
          }).join("")}</tbody></table>
        </div>`;
      $$("#m-result button[data-fx]").forEach(b => b.onclick = () =>
        action(async () => {
          const purpose = prompt("借用用途", "工位匹配快速申请") ?? "";
          await post("/api/requests", {
            fixture_id: b.dataset.fx, station, model, purpose, hours: 24,
          });
        }, `申请已提交（${b.dataset.fx} → ${station} / ${model}）`));
    };
    $("#m-go").click();
  }

  // ---------- 借还扫码
  async function scanPage() {
    const approved = await api("/api/requests?status=approved");
    const picked = await api("/api/requests?status=picked");
    $("#page").innerHTML = `
      <div class="page-head"><h2>借还扫码</h2>
        <span class="hint">扫描/输入申请单号完成领用或归还</span></div>
      <div class="scan-wrap">
        <div class="scan-box" id="scan-pick">
          <div class="scan-corners"><span class="corner c-tl"></span><span class="corner c-tr"></span></div>
          <div class="scan-label">领 用 出 库</div>
          <input id="pick-code" placeholder="扫描申请单号 RQ-…" style="text-align:center;font-size:18px;margin:12px 0;min-width:260px">
          <button class="btn" id="pick-btn">确认领用</button>
          <div class="scan-corners" style="margin-top:14px"><span class="corner c-bl"></span><span class="corner c-br"></span></div>
          <div id="pick-info"></div>
          <div class="dim" style="margin-top:14px">待领用申请：
            ${approved.map(r => `<button class="btn tiny ghost" data-fill="${r.id}">${r.id}</button>`).join(" ") || "无"}</div>
        </div>
        <div class="scan-box" id="scan-back">
          <div class="scan-corners"><span class="corner c-tl"></span><span class="corner c-tr"></span></div>
          <div class="scan-label">归 还 入 库</div>
          <input id="back-code" placeholder="扫描申请单号 RQ-…" style="text-align:center;font-size:18px;margin:12px 0;min-width:260px">
          <button class="btn" id="back-btn">确认归还（进入点检）</button>
          <div class="scan-corners" style="margin-top:14px"><span class="corner c-bl"></span><span class="corner c-br"></span></div>
          <div id="back-info"></div>
          <div class="dim" style="margin-top:14px">在借申请：
            ${picked.map(r => `<button class="btn tiny ghost" data-fill="${r.id}">${r.id}</button>`).join(" ") || "无"}</div>
        </div>
      </div>`;
    const fillInfo = async (rid, kind) => {
      try {
        const all = await api("/api/requests");
        const r = all.find(x => x.id === rid.trim());
        if (!r) throw new Error("申请单不存在");
        $(`#${kind}-info`).innerHTML = `<div class="scan-result ok">
          <div class="scan-label">工位编号</div><div class="big-station">${esc(r.station_name)}
            <span class="code" style="font-size:16px">（${esc(r.station_id)}）</span></div>
          <div class="scan-label" style="margin-top:8px">夹具编号</div>
          <div class="big-code">${esc(r.fixture_id)}</div>
          <div class="dim">${esc(r.fixture_name)} · ${esc(r.model_id)} · 申请人 ${esc(r.applicant_name)}
          · 当前状态 ${rqStatus(r.status)}${r.overdue ? " · <b style='#ff8d91'>已逾期</b>" : ""}</div>
        </div>`;
      } catch (e) {
        $(`#${kind}-info`).innerHTML = `<div class="scan-result err">${esc(e.message)}</div>`;
      }
    };
    $$("[data-fill]").forEach(b => b.onclick = () => {
      const kind = b.closest("#scan-pick") ? "pick" : "back";
      $(`#${kind}-code`).value = b.dataset.fill;
      fillInfo(b.dataset.fill, kind);
    });
    $("#pick-code").onkeydown = e => e.key === "Enter" && fillInfo(e.target.value, "pick");
    $("#back-code").onkeydown = e => e.key === "Enter" && fillInfo(e.target.value, "back");
    $("#pick-btn").onclick = () => action(async () => {
      const rid = $("#pick-code").value.trim();
      if (!rid) throw new Error("请扫描/输入申请单号");
      const res = await post(`/api/requests/${rid}/pickup`);
      if (res.over_life_disposal)
        toast(`领用成功（累计 ${res.use_count} 次），已达寿命并生成超寿命处置单 ${res.over_life_disposal}`);
      else toast(`领用成功，${res.fixture_id} 累计使用 ${res.use_count} 次`);
    }, null);
    $("#back-btn").onclick = () => action(async () => {
      const rid = $("#back-code").value.trim();
      if (!rid) throw new Error("请扫描/输入申请单号");
      await post(`/api/requests/${rid}/return`);
      toast("归还成功，等待维修班点检后恢复可用");
    }, null);
  }

  // ---------- 保养计划
  async function maintPage() {
    const plans = await api("/api/plans");
    $("#page").innerHTML = `
      <div class="page-head"><h2>保养计划</h2>
        <span class="hint">进程内任务每 30 秒扫描寿命累计，到期自动生成提醒</span></div>
      <div class="panel" style="padding:0;overflow-x:auto"><table>
        <thead><tr><th>计划号</th><th>夹具</th><th>进度（累计 / 计划点）</th>
          <th>周期</th><th>状态</th><th>操作</th></tr></thead><tbody>
        ${plans.length ? plans.map(p => `
          <tr>
            <td class="code">${esc(p.id)}</td>
            <td><b>${esc(p.fixture_name)}</b><div class="dim">${esc(p.fixture_id)}</div></td>
            <td style="min-width:240px">
              <div class="life"><div class="bar"><div class="fill ${p.due ? "warn" : ""}"
                style="width:${p.pct}%"></div></div>
                <span class="num">${p.use_count}/${p.due_count}</span></div>
            </td>
            <td>每 ${p.maintenance_every} 次</td>
            <td>${p.due ? badge("warn", "已到保养点") : badge("ok", "未到期")}
                ${p.fixture_status === "in_repair" ? badge("in_repair", "维修中") : ""}</td>
            <td>${p.due ? `<button class="btn tiny" data-plan="${p.id}"
                   ${["scrapped"].includes(p.fixture_status) ? "disabled" : ""}>登记保养</button>` : "—"}</td>
          </tr>`).join("") : `<tr><td colspan="6" class="empty">暂无保养计划</td></tr>`}
        </tbody></table></div>`;
    $$("button[data-plan]").forEach(b => b.onclick = () =>
      action(() => post(`/api/plans/${b.dataset.plan}/done`),
        "保养已登记，并按周期生成下一计划点"));
  }

  // ---------- 报废审批
  async function scrapPage() {
    const list = await api("/api/scrap");
    $("#page").innerHTML = `
      <div class="page-head"><h2>报废审批</h2>
        <span class="hint">超寿命 / 变形 / 定位销缺失处置单由工段长判定</span></div>
      <div class="scrap-grid">
        ${list.length ? list.map(d => {
          const i = d.latest_inspection;
          return `<div class="scrap-card ${d.status}">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <b>${esc(d.fixture_name)} <span class="code">${esc(d.fixture_id)}</span></b>
              ${badge(d.type === "over_life" ? "bad" : "warn", typeLabel(d.type))}
              ${badge(d.status === "approved" ? "ok" : d.status === "rejected" ? "gray" : "warn",
                d.status === "open" ? "待审批" : d.status === "approved" ? "准予报废" : "驳回续用")}
            </div>
            <div class="scrap-fixed">
              <div><div class="k">累计使用次数</div>
                <div class="v ${d.use_count >= d.life_threshold ? "bad" : "warn"}">${d.use_count}</div></div>
              <div><div class="k">寿命阈值</div><div class="v">${d.life_threshold}</div></div>
              <div><div class="k">最近点检</div>
                <div class="v" style="font-size:15px;padding-top:4px">
                  ${i ? (i.result === "pass"
                    ? `<span style="color:#6fd997">合格</span>`
                    : `<span style="color:#ff8d91">不合格</span>`) : "无"}
                </div></div>
            </div>
            <div class="insp-box">
              ${i ? `<div><span class="dim">${esc(i.id)} · ${esc(i.inspector_name)} · ${fmtTs(i.created_at)}</span></div>
                <div style="margin-top:3px">${esc(i.findings) || "无异常描述"}</div>
                ${i.photo_path ? `<img src="/photos/${esc(i.photo_path)}" alt="点检照片">` : ""}`
                : `<span class="dim">暂无点检记录</span>`}
              <div style="margin-top:6px" class="dim">处置说明：${esc(d.detail)}</div>
            </div>
            ${d.status === "open" ? `<div class="scrap-actions">
                <input placeholder="审批意见（可空）" id="note-${d.id}">
                <button class="btn danger" data-approve="1" data-id="${d.id}">准予报废</button>
                <button class="btn ghost" data-approve="0" data-id="${d.id}">驳回续用</button>
              </div>` : `<div class="dim" style="margin-top:8px">判定人：${staffName(d.decider_id)} · ${fmtTs(d.decided_at)}</div>`}
          </div>`;
        }).join("") : `<div class="empty">暂无报废相关处置单</div>`}
      </div>`;
    $$(".scrap-card button[data-id]").forEach(b => b.onclick = () => {
      const id = b.dataset.id, approved = b.dataset.approve === "1";
      const note = $(`#note-${id}`).value;
      action(() => post(`/api/scrap/${id}/decide`, { approved, note }),
        approved ? "已准予报废，夹具状态置为已报废" : "已驳回，夹具恢复可用");
    });
  }

  // ---------- 跨线调拨
  async function transferPage() {
    const [list, fixtures] = await Promise.all([api("/api/transfers"), api("/api/fixtures?status=available")]);
    $("#page").innerHTML = `
      <div class="page-head"><h2>跨线调拨</h2>
        <span class="hint">操作员无权发起；须由调入线工段长审批，通过后生成跨线处置单</span></div>
      <div class="panel">
        <h3><span class="tag">●</span>新建调拨申请（维修班身份）</h3>
        <div class="form-row">
          <div class="field"><label>夹具</label><select id="t-fx">
            ${fixtures.map(f => `<option value="${f.id}">${f.id} ${esc(f.name)}（${lineName(f.line_id)} / ${esc(f.station_name || "")}）</option>`).join("")}
          </select></div>
          <div class="field"><label>目标工位（须跨线）</label><select id="t-to">
            ${meta.stations.map(s => `<option value="${s.id}">${s.id} ${esc(s.name)}（${lineName(s.line_id)}）</option>`).join("")}
          </select></div>
          <button class="btn" id="t-go">提交调拨申请</button>
        </div>
      </div>
      <div class="panel" style="padding:0;overflow-x:auto"><table>
        <thead><tr><th>调拨单</th><th>夹具</th><th>去向</th><th>申请人</th><th>审批人</th>
          <th>状态</th><th>操作</th></tr></thead><tbody>
        ${list.length ? list.map(t => `
          <tr>
            <td class="code">${esc(t.id)}</td>
            <td>${esc(t.fixture_name)}<div class="dim">${esc(t.fixture_id)}</div></td>
            <td>${esc(t.from_name || "—")} → <b>${esc(t.to_name)}</b></td>
            <td>${esc(t.raiser_name)}</td>
            <td>${t.approver_id ? esc(staffName(t.approver_id)) : "—"}</td>
            <td>${badge(t.status === "approved" ? "ok" : t.status === "rejected" ? "gray" : "warn",
              t.status === "pending" ? "待审批" : t.status === "approved" ? "已调拨" : "已驳回")}</td>
            <td>${t.status === "pending"
              ? `<button class="btn tiny" data-act="approve" data-id="${t.id}">审批通过</button>
                 <button class="btn tiny ghost" data-act="reject" data-id="${t.id}">驳回</button>`
              : "—"}</td>
          </tr>`).join("") : `<tr><td colspan="7" class="empty">暂无调拨单</td></tr>`}
        </tbody></table></div>`;
    $("#t-go").onclick = () => action(async () => {
      await post("/api/transfers", {
        fixture_id: $("#t-fx").value, to_station: $("#t-to").value,
        note: "产线产能调配",
      });
      toast("跨线调拨申请已提交，等待调入线工段长审批");
    }, null);
    $$("button[data-act]").forEach(b => b.onclick = () =>
      action(() => post(`/api/transfers/${b.dataset.id}/${b.dataset.act}`,
        b.dataset.act === "reject" ? { note: "产能未确认" } : {}),
        b.dataset.act === "approve" ? "调拨完成，已生成跨线处置单" : "已驳回"));
  }

  // ---------- 维修记录
  async function repairPage() {
    const [list, fixtures] = await Promise.all([
      api("/api/repairs"), api("/api/fixtures")]);
    const repairable = fixtures.filter(f => f.status !== "scrapped");
    $("#page").innerHTML = `
      <div class="page-head"><h2>维修记录与复核</h2>
        <span class="hint">维修完成须由<b>另一名员工</b>复核；复核通过后解除冻结</span></div>
      <div class="panel">
        <h3><span class="tag">●</span>发起维修</h3>
        <div class="form-row">
          <div class="field"><label>夹具</label><select id="r-fx">
            ${repairable.map(f => `<option value="${f.id}">${f.id} ${esc(f.name)} · ${fxStatus(f.status)}</option>`).join("")}
          </select></div>
          <div class="field"><label>承修人（维修班）</label><select id="r-who">
            ${meta.staff.filter(s => s.role === "maintainer").map(s =>
              `<option value="${s.id}">${s.id} ${esc(s.name)}</option>`).join("")}
          </select></div>
          <div class="field" style="flex:1"><label>故障原因</label>
            <input id="r-why" placeholder="例：定位销磨损，压紧力不足"></div>
          <button class="btn" id="r-go">开维修单</button>
        </div>
      </div>
      <div class="panel" style="padding:0;overflow-x:auto"><table>
        <thead><tr><th>维修单</th><th>夹具</th><th>报修</th><th>承修</th><th>复核</th>
          <th>原因</th><th>状态</th><th>操作</th></tr></thead><tbody>
        ${list.length ? list.map(r => `
          <tr>
            <td class="code">${esc(r.id)}</td>
            <td>${esc(r.fixture_name)}<div class="dim">${esc(r.fixture_id)}</div></td>
            <td>${esc(r.requester_name)}</td>
            <td>${esc(r.repairer_name)}</td>
            <td>${r.reviewer_id ? esc(r.reviewer_name) : "—"}</td>
            <td style="max-width:220px">${esc(r.reason)}${r.review_note ? `<div class="dim">${esc(r.review_note)}</div>` : ""}</td>
            <td>${badge(r.status === "done" ? "ok" : r.status === "review" ? "warn" : "in_repair",
              r.status === "open" ? "维修中" : r.status === "review" ? "待另一员工复核" : "已完成")}</td>
            <td>${r.status === "open"
              ? `<button class="btn tiny" data-act="finish" data-id="${r.id}">维修完成</button>`
              : r.status === "review"
                ? `<button class="btn tiny" data-act="review-ok" data-id="${r.id}">复核通过</button>
                   <button class="btn tiny ghost" data-act="review-no" data-id="${r.id}">复核驳回</button>`
                : "—"}</td>
          </tr>`).join("") : `<tr><td colspan="8" class="empty">暂无维修单</td></tr>`}
        </tbody></table></div>
      <div class="dim">提示：承修人本人点“复核通过”会被系统拒绝，请在左下角切换为另一名维修班/工段长。</div>`;
    $("#r-go").onclick = () => action(async () => {
      await post("/api/repairs", {
        fixture_id: $("#r-fx").value, repairer_id: $("#r-who").value,
        reason: $("#r-why").value,
      });
      toast("维修单已开立，夹具冻结且不可被预订");
    }, null);
    $$("button[data-act]").forEach(b => b.onclick = () => {
      const id = b.dataset.id, act = b.dataset.act;
      if (act === "finish") return action(() => post(`/api/repairs/${id}/finish`), "已提交，等待另一名员工复核");
      if (act === "review-ok") return action(() => post(`/api/repairs/${id}/review`, { approved: true, note: "复核合格" }),
        "复核通过，夹具解除冻结恢复可用");
      return action(() => post(`/api/repairs/${id}/review`, { approved: false, note: "仍有偏差，需返工" }),
        "已驳回返工");
    });
  }

  // ---------- 处置单
  async function disposalPage() {
    const list = await api("/api/disposals");
    $("#page").innerHTML = `
      <div class="page-head"><h2>处置单</h2>
        <span class="hint">变形 · 定位销缺失 · 超寿命 · 跨线调拨 · 归还逾期</span></div>
      <div class="panel" style="padding:0;overflow-x:auto"><table>
        <thead><tr><th>处置单号</th><th>类型</th><th>夹具</th><th>关联单据</th>
          <th>提报人</th><th>说明</th><th>状态</th><th>时间</th></tr></thead><tbody>
        ${list.length ? list.map(d => `
          <tr>
            <td class="code">${esc(d.id)}</td>
            <td>${badge(["overdue", "transfer"].includes(d.type) ? "warn" : "bad", typeLabel(d.type))}</td>
            <td><b>${esc(d.fixture_name)}</b><div class="dim">${esc(d.fixture_id)}</div></td>
            <td class="dim">${esc(d.related_id || "—")}</td>
            <td>${esc(d.raiser_name)}</td>
            <td style="max-width:320px">${esc(d.detail)}</td>
            <td>${badge(d.status === "approved" ? "ok" : d.status === "rejected" ? "gray" : "warn",
              d.status === "open" ? "待处理" : d.status === "approved" ? "已批准" : "已驳回")}</td>
            <td class="dim">${fmtTs(d.created_at)}</td>
          </tr>`).join("") : `<tr><td colspan="8" class="empty">暂无处置单</td></tr>`}
        </tbody></table></div>`;
  }

  // ------------------------------------------------------------ shell
  const PAGES = {
    ledger, queue, match, scan, maint, scrap, transfer, repair, disposal,
  };
  const TITLES = {
    ledger: "夹具台账", queue: "申请队列", match: "工位匹配", scan: "借还扫码",
    maint: "保养计划", scrap: "报废审批", transfer: "跨线调拨",
    repair: "维修记录", disposal: "处置单",
  };

  async function render() {
    try {
      await PAGES[currentPage]();
    } catch (e) {
      $("#page").innerHTML =
        `<div class="panel scan-result err">页面加载失败：${esc(e.message)}</div>`;
    }
  }

  async function loadReminders() {
    try {
      const list = await api("/api/reminders");
      const box = $("#reminders");
      if (!list.length) { box.classList.remove("show"); return; }
      $("#reminder-list").innerHTML = list.slice(0, 6)
        .map(r => `<li>[${r.kind === "overdue" ? "逾期" : "保养"}] ${esc(r.fixture_name)}：${esc(r.message)}</li>`)
        .join("");
      box.classList.add("show");
    } catch { /* ignore */ }
  }

  return {
    async init() {
      meta = await api("/api/meta");
      const sel = $("#actor");
      sel.innerHTML = meta.staff.map(s => {
        const role = { operator: "操作员", maintainer: "维修班", supervisor: "工段长" }[s.role];
        return `<option value="${s.id}">${s.id} ${s.name} · ${role} · ${lineName(s.home_line)}</option>`;
      }).join("");
      sel.value = localStorage.getItem("actor") || "EMP-021";
      sel.onchange = () => { localStorage.setItem("actor", sel.value); render(); };

      $$("#nav button").forEach(b => b.onclick = () => {
        $$("#nav button").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        currentPage = b.dataset.page;
        document.title = `${TITLES[currentPage]} · 工装夹具系统`;
        render();
      });
      await render();
      await loadReminders();
      setInterval(loadReminders, 15000);
      setInterval(() => currentPage === "scan" && render(), 20000);
    },
    dismissReminders() {
      api("/api/reminders/read", { method: "POST" }).then(loadReminders);
    },
  };
})();

window.addEventListener("DOMContentLoaded", () => App.init());
