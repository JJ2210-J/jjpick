"use strict";

/* 캡컷 자동 편집기 UI
   원칙 (요청서 5절):
     · 색·간격·글꼴은 styles.css의 토큰만 씁니다. 여기서 값을 만들지 않습니다.
     · 선행 단계가 미완이면 비활성 + **이유**를 반드시 보여 줍니다.
     · 실패는 조용히 넘어가지 않고 무엇이/왜/어떻게를 보여 줍니다.
     · 오래 걸리는 작업은 SSE로 진행률과 현재 항목명을 실시간 표시합니다.
     · 이모지는 좌측 단계 아이콘에만 씁니다. */

const S = {
  status: null,
  session: null,
  data: {},
  steps: [],
  active: "prepare",
  sources: null,
  candidates: [],
  cutSummary: null,
  subtitles: [],
  subtitleMeta: null,
  profile: null,
  logSeq: 0,
  logOpen: false,
  pending: {},          // step -> job snapshot
  scratch: {},          // 화면별 임시 상태
};

const $ = (sel, root) => (root || document).querySelector(sel);
const el = (tag, attrs, children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};

/* ── 서버 통신 ─────────────────────────────────────────────────────────── */
async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    throw new ApiError("서버에 연결하지 못했습니다.\n실행 창(콘솔)이 닫히지 않았는지 확인하세요.", 0);
  }
  const text = await res.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch (_) { payload = null; }
  if (!res.ok) {
    const detail = (payload && (payload.detail || payload.message)) || text || `HTTP ${res.status}`;
    throw new ApiError(detail, res.status, payload && payload.kind);
  }
  return payload;
}

class ApiError extends Error {
  constructor(message, status, kind) {
    super(message);
    this.status = status;
    this.kind = kind;
  }
}

/* ── 알림 ──────────────────────────────────────────────────────────────── */
function note(kind, what, body) {
  const box = el("div", { class: `note ${kind}` }, [
    what ? el("span", { class: "what", text: what }) : null,
    body || "",
  ]);
  return box;
}

function toast(kind, what, body, ms) {
  const host = $("#globalNotes");
  const box = note(kind, what, body);
  box.style.margin = "var(--sp-3) var(--sp-5) 0";
  host.append(box);
  setTimeout(() => box.remove(), ms || (kind === "danger" ? 12000 : 6000));
  return box;
}

function showError(err) {
  const message = err instanceof ApiError ? err.message : String(err && err.message || err);
  const title = err && err.kind === "locked" ? "아직 할 수 없습니다"
    : err && err.kind === "draft" ? "드래프트 작업 실패"
    : err && err.kind === "media" ? "미디어 처리 실패"
    : err && err.kind === "stt" ? "음성 인식 실패"
    : "실패";
  toast("danger", title, message, 15000);
  console.error(err);
}

function fmtTime(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const m = Math.floor(s / 60), r = s - m * 60;
  const h = Math.floor(m / 60);
  const mm = String(h ? m % 60 : m).padStart(h ? 2 : 1, "0");
  return (h ? `${h}:` : "") + `${mm}:${r.toFixed(2).padStart(5, "0")}`;
}
const fmtDur = (s) => {
  const v = Math.max(0, Number(s) || 0);
  if (v < 60) return `${v.toFixed(1)}초`;
  const m = Math.floor(v / 60);
  return m < 60 ? `${m}분 ${Math.round(v - m * 60)}초` : `${Math.floor(m / 60)}시간 ${m % 60}분`;
};
const fmtMB = (bytes) => `${(bytes / 1048576).toFixed(1)}MB`;

/** 서버 설정값을 안전하게 읽습니다. 상태를 아직 못 받았어도 기본값으로 동작합니다. */
function setting(key, fallback) {
  const s = S.status && S.status.settings;
  const value = s ? s[key] : undefined;
  return value === undefined || value === null ? fallback : value;
}

/* ── 작업 실행 + SSE ───────────────────────────────────────────────────── */
function progressBox(step) {
  return el("div", { class: "progress", id: `progress-${step}` }, [
    el("div", { class: "progress-bar" }, [el("div", { class: "progress-fill", style: "width:0%" })]),
    el("div", { class: "progress-label" }, [
      el("span", { class: "msg", text: "준비 중" }),
      el("span", { class: "detail", text: "" }),
    ]),
  ]);
}

function updateProgress(step, snap) {
  const box = $(`#progress-${step}`);
  if (!box) return;
  $(".progress-fill", box).style.width = `${Math.round((snap.progress || 0) * 100)}%`;
  $(".msg", box).textContent = snap.message || "";
  $(".detail", box).textContent = snap.detail || `${Math.round((snap.progress || 0) * 100)}%`;
}

async function runJob(step, starter, onDone) {
  let started;
  try {
    started = await starter();
  } catch (err) { showError(err); return; }

  const job = started.job;
  S.pending[step] = job;
  render();

  const source = new EventSource(
    `/api/sessions/${S.session.id}/jobs/${job.job_id}/stream`
  );
  source.onmessage = (event) => {
    let snap;
    try { snap = JSON.parse(event.data); } catch (_) { return; }
    S.pending[step] = snap;
    updateProgress(step, snap);
    if (["done", "error", "cancelled"].includes(snap.status)) {
      source.close();
      delete S.pending[step];
      if (snap.status === "error") {
        showError(new ApiError(snap.error || "작업이 실패했습니다.", 500));
        render();
      } else if (snap.status === "cancelled") {
        toast("warn", "취소됨", "작업을 취소하고 부분 파일을 정리했습니다.");
        render();
      } else if (onDone) {
        Promise.resolve(onDone(snap.result)).catch(showError);
      } else { render(); }
    }
  };
  source.onerror = () => {
    // SSE가 끊기면 폴링으로 이어받습니다 (프록시·절전 등).
    source.close();
    pollJob(step, job.job_id, onDone);
  };
}

async function pollJob(step, jobId, onDone) {
  try {
    const snap = await api(`/api/sessions/${S.session.id}/jobs/${jobId}`);
    S.pending[step] = snap;
    updateProgress(step, snap);
    if (["done", "error", "cancelled"].includes(snap.status)) {
      delete S.pending[step];
      if (snap.status === "error") { showError(new ApiError(snap.error, 500)); render(); }
      else if (snap.status === "cancelled") { render(); }
      else if (onDone) { await onDone(snap.result); } else { render(); }
      return;
    }
  } catch (err) { showError(err); delete S.pending[step]; render(); return; }
  setTimeout(() => pollJob(step, jobId, onDone), 1200);
}

async function cancelJob(step) {
  const job = S.pending[step];
  if (!job) return;
  try {
    await api(`/api/sessions/${S.session.id}/jobs/${job.job_id}/cancel`, { method: "POST" });
  } catch (err) { showError(err); }
}

function runningPanel(step, label) {
  const job = S.pending[step];
  if (!job) return null;
  return el("div", {}, [
    progressBox(step),
    el("div", { class: "btn-row" }, [
      el("button", { class: "btn danger sm", onclick: () => cancelJob(step) }, "취소"),
      el("span", { class: "small muted", text: label || "진행 중입니다. 이 화면을 떠나도 계속됩니다." }),
    ]),
  ]);
}

/* ── 캡컷 실행 감지 (버튼 누르기 직전 확인) ────────────────────────────── */
async function guardCapcut() {
  try {
    const res = await api("/api/setup/capcut-running");
    if (res.running) {
      toast("danger", "캡컷이 실행 중입니다", res.message, 15000);
      return false;
    }
  } catch (_) { /* 감지 실패는 막지 않습니다 */ }
  return true;
}

/* ── 초기화 ────────────────────────────────────────────────────────────── */
async function boot() {
  bindTopbar();
  await refreshStatus();
  const saved = localStorage.getItem("capcut_session_id");
  if (saved) {
    try { await loadSession(saved); } catch (_) { localStorage.removeItem("capcut_session_id"); }
  }
  if (!S.session) await openSessionList(true);
  render();
  pollLogs();
}

function bindTopbar() {
  $("#btnNewSession").onclick = () => newSession();
  $("#btnSessionList").onclick = () => openSessionList(false);
  $("#btnSettings").onclick = () => openSettings();
  $("#btnToggleLog").onclick = () => { S.logOpen = !S.logOpen; $("#logPanel").classList.toggle("hidden", !S.logOpen); };
  $("#btnCloseLog").onclick = () => { S.logOpen = false; $("#logPanel").classList.add("hidden"); };
  $("#btnClearLogView").onclick = () => { $("#logLines").innerHTML = ""; };
}

async function refreshStatus() {
  try { S.status = await api("/api/setup/status"); } catch (err) { showError(err); }
}

async function loadSession(id) {
  const res = await api(`/api/sessions/${id}`);
  S.session = res.session;
  S.data = res.data || {};
  S.steps = res.session.steps;
  S.sources = null;
  S.candidates = [];
  S.subtitles = [];
  localStorage.setItem("capcut_session_id", id);
  $("#sessionName").textContent = res.session.name;

  if (S.data.sources) {
    const s = await api(`/api/editing/${id}/sources`);
    S.sources = s.sources;
  }
  if ((S.data.candidates || []).length) {
    const c = await api(`/api/editing/${id}/candidates`);
    S.candidates = c.candidates; S.cutSummary = c.summary;
  }
  if ((S.data.subtitles || []).length) {
    const t = await api(`/api/editing/${id}/subtitles`);
    S.subtitles = t.subtitles; S.subtitleMeta = t.meta;
  }
  const p = await api("/api/calibration/profile");
  S.profile = p.profile;
}

async function reloadSession() {
  if (S.session) await loadSession(S.session.id);
}

async function newSession() {
  const name = prompt("세션 이름을 입력하세요 (영상 한 편 = 한 세션)", "");
  if (name === null) return;
  try {
    const res = await api("/api/sessions", { method: "POST", body: { name } });
    await loadSession(res.session.id);
    S.active = "prepare";
    render();
  } catch (err) { showError(err); }
}

/* ── 좌측 네비 ─────────────────────────────────────────────────────────── */
function renderNav() {
  const nav = $("#nav");
  nav.innerHTML = "";
  if (!S.session) { nav.append(el("div", { class: "small muted", text: "세션을 먼저 만드세요." })); return; }

  for (const step of S.steps) {
    const item = el("button", {
      class: `nav-item ${S.active === step.key ? "active" : ""} ${step.locked ? "locked" : ""}`,
      title: step.locked ? step.lock_reason : "",
      onclick: () => {
        if (step.locked) { toast("warn", "아직 할 수 없습니다", step.lock_reason); return; }
        S.active = step.key; render();
      },
    }, [
      el("span", { class: "icon", text: step.icon }),
      el("span", { class: "body" }, [
        el("span", { class: "title", text: step.title }),
        step.locked ? el("span", { class: "sub", text: step.lock_reason }) : null,
      ]),
      step.done ? el("span", { class: "check", text: "완료" }) : null,
    ]);
    nav.append(item);
  }
}

/* ── 렌더 ──────────────────────────────────────────────────────────────── */
function render() {
  renderNav();
  const view = $("#view");
  view.innerHTML = "";

  if (!S.session) {
    view.append(note("info", "세션이 없습니다", "상단의 '새 세션'을 눌러 시작하세요. 영상 한 편이 한 세션입니다."));
    return;
  }

  const step = S.steps.find((s) => s.key === S.active);
  if (step && step.locked) {
    view.append(el("div", { class: "card" }, [
      el("h2", { text: step.title }),
      el("div", { class: "locked-banner", text: step.lock_reason }),
    ]));
    return;
  }

  const renderer = VIEWS[S.active];
  if (renderer) renderer(view);
}

function statusBanner() {
  if (!S.status) return null;
  const items = [];
  for (const b of S.status.blockers || []) items.push(note("danger", "해결이 필요합니다", b));
  for (const w of S.status.warnings || []) items.push(note("warn", "확인하세요", w));
  return items.length ? el("div", {}, items) : null;
}

const VIEWS = {};

/* ══════════════════════════════════════════════════════════════════════
   0차 프로젝트 준비
   ══════════════════════════════════════════════════════════════════════ */
VIEWS.prepare = function (view) {
  const banner = statusBanner();
  if (banner) view.append(banner);

  const picked = S.scratch.picked || (S.scratch.picked = []);

  const card = el("div", { class: "card" }, [
    el("h2", { text: "0차 프로젝트 준비" }),
    el("div", { class: "desc", text: "원본 영상을 순서대로 고르세요. 여러 개를 이어붙인 하나의 타임라인으로 다룹니다." }),
  ]);

  // ── 경로 입력 3종 ────────────────────────────────────────────────────
  card.append(el("h3", { text: "영상 고르기" }));
  card.append(el("div", { class: "btn-row" }, [
    el("button", { class: "btn", onclick: () => openBrowser() }, "앱에서 찾아보기"),
    el("button", {
      class: "btn",
      title: S.status && S.status.is_windows ? "" : "Windows에서만 씁니다",
      onclick: () => nativePick(),
    }, "윈도우 기본 선택 창"),
  ]));

  const pasteBox = el("textarea", {
    placeholder: "경로를 직접 붙여넣으세요. 한 줄에 하나씩.\n폴더 경로를 넣으면 그 안의 영상을 모두 불러옵니다.\n따옴표가 붙어 있어도 알아서 정리합니다.",
  });
  card.append(el("div", { class: "field mt3" }, [pasteBox]));
  card.append(el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn", onclick: async () => {
        const lines = pasteBox.value.split("\n").map((l) => l.trim()).filter(Boolean);
        if (!lines.length) { toast("warn", "경로가 비었습니다", "경로를 한 줄에 하나씩 붙여넣으세요."); return; }
        try {
          const res = await api("/api/files/resolve", { method: "POST", body: { paths: lines } });
          addPicked(res.videos);
          for (const p of res.problems || []) toast("warn", "확인하세요", p);
          pasteBox.value = "";
          render();
        } catch (err) { showError(err); }
      },
    }, "붙여넣은 경로 불러오기"),
  ]));

  // ── 선택 목록 ────────────────────────────────────────────────────────
  card.append(el("h3", { text: `선택한 영상 (${picked.length}개)` }));
  if (!picked.length) {
    card.append(el("div", { class: "locked-banner", text: "아직 선택한 영상이 없습니다." }));
  } else {
    const list = el("div", { class: "list" });
    let total = 0;
    picked.forEach((v, i) => {
      total += Number(v.duration || 0);
      list.append(el("div", { class: "list-item" }, [
        el("span", { class: "badge", text: String(i + 1) }),
        el("span", { class: "grow", title: v.path, text: v.name }),
        el("span", { class: "meta", text: v.duration ? `${fmtDur(v.duration)} · ${v.width}x${v.height} · ${v.fps}fps` : "정보 확인 전" }),
        el("button", { class: "btn sm", disabled: i === 0, onclick: () => { const t = picked[i - 1]; picked[i - 1] = picked[i]; picked[i] = t; render(); } }, "↑"),
        el("button", { class: "btn sm", disabled: i === picked.length - 1, onclick: () => { const t = picked[i + 1]; picked[i + 1] = picked[i]; picked[i] = t; render(); } }, "↓"),
        el("button", { class: "btn sm danger", onclick: () => { picked.splice(i, 1); render(); } }, "✕"),
      ]));
    });
    card.append(list);
    card.append(el("div", { class: "stat-row" }, [
      el("div", { class: "stat" }, [el("div", { class: "value", text: String(picked.length) }), el("div", { class: "label", text: "영상 수" })]),
      el("div", { class: "stat" }, [el("div", { class: "value", text: fmtDur(total) }), el("div", { class: "label", text: "합계 길이" })]),
    ]));
    card.append(el("div", { class: "btn-row" }, [
      el("button", { class: "btn primary", onclick: () => registerSources(picked) }, "이 순서로 등록"),
      el("button", { class: "btn", onclick: () => { S.scratch.picked = []; render(); } }, "비우기"),
    ]));
  }
  view.append(card);

  // ── 등록 결과 ────────────────────────────────────────────────────────
  if (S.sources) {
    const c = el("div", { class: "card" }, [
      el("h2", { text: "등록된 소스 타임라인" }),
      el("div", { class: "desc", text: "무음 감지·전사·컷 편집·자막을 전부 이 가상 타임라인 위에서 합니다." }),
    ]);
    for (const w of S.sources.warnings || []) c.append(note("warn", "확인하세요", w));
    c.append(el("div", { class: "stat-row" }, [
      el("div", { class: "stat" }, [el("div", { class: "value", text: String(S.sources.count) }), el("div", { class: "label", text: "영상" })]),
      el("div", { class: "stat" }, [el("div", { class: "value", text: fmtDur(S.sources.total_duration) }), el("div", { class: "label", text: "총 길이" })]),
      el("div", { class: "stat" }, [el("div", { class: "value", text: `${S.sources.canvas.width}×${S.sources.canvas.height}` }), el("div", { class: "label", text: "캔버스" })]),
      el("div", { class: "stat" }, [el("div", { class: "value", text: `${S.sources.fps}` }), el("div", { class: "label", text: "fps" })]),
    ]));
    const tb = el("tbody");
    for (const clip of S.sources.clips) {
      tb.append(el("tr", {}, [
        el("td", { text: String(clip.index + 1) }),
        el("td", { title: clip.path, text: clip.name }),
        el("td", { class: "num", text: `${fmtTime(clip.offset)} ~ ${fmtTime(clip.end)}` }),
        el("td", { class: "num", text: `${clip.width}×${clip.height}` }),
        el("td", { class: "num", text: `${clip.fps}` }),
        el("td", { class: "num", text: clip.audio_track_count ? `${clip.audio_track_count}개` : "없음" }),
      ]));
    }
    c.append(el("div", { class: "table-wrap" }, [
      el("table", {}, [
        el("thead", {}, [el("tr", {}, ["#", "파일", "타임라인 위치", "해상도", "fps", "오디오"].map((h) => el("th", { text: h })))]),
        tb,
      ]),
    ]));
    view.append(c);
  }

  view.append(backupCard());
};

function addPicked(videos) {
  const picked = S.scratch.picked || (S.scratch.picked = []);
  const seen = new Set(picked.map((v) => v.path));
  for (const v of videos || []) if (!seen.has(v.path)) { seen.add(v.path); picked.push(v); }
}

async function registerSources(picked) {
  if (!picked.length) return;
  try {
    const res = await api(`/api/editing/${S.session.id}/sources`, {
      method: "POST", body: { paths: picked.map((v) => v.path) },
    });
    S.sources = res.sources;
    for (const p of res.problems || []) toast("warn", "일부 파일을 건너뛰었습니다", p);
    await reloadSession();
    toast("ok", "등록 완료", `${res.sources.count}개 영상, 합계 ${fmtDur(res.sources.total_duration)}`);
    render();
  } catch (err) { showError(err); }
}

async function nativePick() {
  try {
    const avail = await api("/api/files/native/available");
    if (!avail.available) { toast("warn", "쓸 수 없습니다", avail.reason); return; }
    toast("info", "선택 창을 띄웠습니다", "다른 창 뒤에 가려져 있을 수 있습니다. 작업 표시줄을 확인하세요.", 8000);
    const res = await api("/api/files/native/pick", { method: "POST", body: { mode: "files" } });
    if (!res.ok) { toast("danger", "선택 창 실패", res.error || "알 수 없는 오류"); return; }
    addPicked(res.videos);
    for (const p of res.problems || []) toast("warn", "확인하세요", p);
    render();
  } catch (err) { showError(err); }
}

/* ── 앱 내장 찾아보기 모달 ─────────────────────────────────────────────── */
async function openBrowser(path) {
  let data;
  try { data = await api(`/api/files/browse?path=${encodeURIComponent(path || "")}`); }
  catch (err) { showError(err); return; }

  const chosen = new Set();
  const body = el("div", {});

  const header = el("div", { class: "row" }, [
    el("div", { class: "field" }, [
      el("label", { text: "현재 폴더" }),
      el("input", { type: "text", value: data.path, id: "browsePath" }),
    ]),
    el("button", { class: "btn", onclick: () => { closeModal(); openBrowser($("#browsePath").value); } }, "이동"),
  ]);
  body.append(header);

  if (!data.ok) body.append(note("warn", "폴더를 열지 못했습니다", data.message));

  const shortcuts = el("div", { class: "btn-row" });
  if (data.parent) shortcuts.append(el("button", { class: "btn sm", onclick: () => { closeModal(); openBrowser(data.parent); } }, "상위 폴더"));
  for (const sc of data.shortcuts || []) {
    shortcuts.append(el("button", { class: "btn sm", onclick: () => { closeModal(); openBrowser(sc.path); } }, sc.label));
  }
  body.append(shortcuts);

  if (data.truncated) body.append(note("warn", "일부만 표시합니다", data.message));

  const list = el("div", { class: "list mt3", style: "max-height:44vh;overflow:auto" });
  for (const entry of data.entries || []) {
    if (entry.is_dir) {
      list.append(el("div", { class: "list-item" }, [
        el("span", { class: "badge", text: "폴더" }),
        el("span", { class: "grow", text: entry.name }),
        el("button", { class: "btn sm", onclick: () => { closeModal(); openBrowser(entry.path); } }, "열기"),
      ]));
    } else {
      const cb = el("input", { type: "checkbox", onchange: (e) => { e.target.checked ? chosen.add(entry.path) : chosen.delete(entry.path); } });
      list.append(el("div", { class: "list-item" }, [
        cb,
        el("span", { class: "badge " + (entry.kind === "video" ? "silence" : ""), text: entry.kind }),
        el("span", { class: "grow", text: entry.name }),
        el("span", { class: "meta", text: fmtMB(entry.size) }),
      ]));
    }
  }
  if (!(data.entries || []).length) list.append(el("div", { class: "list-item muted" }, "이 폴더에는 영상·이미지·오디오 파일이 없습니다."));
  body.append(list);

  body.append(el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn primary", onclick: async () => {
        if (!chosen.size) { toast("warn", "선택 없음", "파일을 하나 이상 체크하세요."); return; }
        try {
          const res = await api("/api/files/resolve", { method: "POST", body: { paths: [...chosen] } });
          addPicked(res.videos);
          for (const p of res.problems || []) toast("warn", "확인하세요", p);
        } catch (err) { showError(err); }
        closeModal(); render();
      },
    }, "선택한 파일 추가"),
    el("button", {
      class: "btn", onclick: async () => {
        try {
          const res = await api(`/api/files/videos-in-folder?path=${encodeURIComponent(data.path)}`);
          if (!res.ok || !res.videos.length) { toast("warn", "영상 없음", res.message || "이 폴더에 영상이 없습니다."); return; }
          addPicked(res.videos);
        } catch (err) { showError(err); }
        closeModal(); render();
      },
    }, "이 폴더의 영상 전부 추가"),
    el("button", { class: "btn", onclick: () => closeModal() }, "닫기"),
  ]));

  openModal("영상 찾아보기", body);
}

function backupCard() {
  const card = el("div", { class: "card" }, [
    el("h2", { text: "백업과 되돌리기" }),
    el("div", { class: "desc", text: "드래프트를 고치는 작업 전에는 항상 스냅샷을 남깁니다. 언제든 되돌릴 수 있습니다." }),
  ]);
  if (!S.data.draft_name) {
    card.append(el("div", { class: "locked-banner", text: "1차에서 드래프트를 만들면 여기서 백업·되돌리기를 할 수 있습니다." }));
    return card;
  }
  card.append(el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn", onclick: async () => {
        try {
          const res = await api(`/api/editing/${S.session.id}/backup`, { method: "POST", body: { tag: "manual" } });
          toast("ok", "백업 완료", res.backup);
          render();
        } catch (err) { showError(err); }
      },
    }, "지금 백업"),
    el("button", { class: "btn", onclick: () => openBackups() }, "백업 목록 / 되돌리기"),
  ]));
  return card;
}

async function openBackups() {
  let data;
  try { data = await api(`/api/editing/${S.session.id}/backups`); } catch (err) { showError(err); return; }
  const list = el("div", { class: "list" });
  for (const b of data.backups || []) {
    list.append(el("div", { class: "list-item" }, [
      el("span", { class: "grow mono small", text: b.name }),
      el("button", {
        class: "btn sm danger", onclick: async () => {
          if (!confirm(`'${b.name}' 상태로 되돌립니다.\n지금 상태도 백업해 둡니다. 진행할까요?`)) return;
          if (!(await guardCapcut())) return;
          try {
            const res = await api(`/api/editing/${S.session.id}/restore`, { method: "POST", body: { backup_path: b.path } });
            toast("ok", "되돌렸습니다", res.message);
            closeModal();
          } catch (err) { showError(err); }
        },
      }, "이걸로 되돌리기"),
    ]));
  }
  if (!(data.backups || []).length) list.append(el("div", { class: "list-item muted" }, "백업이 없습니다."));
  openModal("백업 목록", el("div", {}, [
    note("info", "되돌리기 전에", "캡컷이 실행 중이면 되돌리기가 차단됩니다. 캡컷을 완전히 종료하세요."),
    list,
  ]));
}

/* ══════════════════════════════════════════════════════════════════════
   자막 스타일 캘리브레이션 (1차보다 먼저)
   ══════════════════════════════════════════════════════════════════════ */
VIEWS.calibration = function (view) {
  const card = el("div", { class: "card" }, [
    el("h2", { text: "자막 스타일 캘리브레이션" }),
    el("div", { class: "desc", text: "캡컷에서 직접 만든 자막의 스타일을 그대로 가져옵니다. 이게 있어야 2차 자막이 캡컷 화면에 제대로 보입니다." }),
    note("info", "왜 필요한가요",
      "pyCapCut이 만드는 자막 소재는 필드가 20개뿐이라 캡컷이 채우는 값 대부분이 비어 있습니다.\n" +
      "그래서 빈 껍데기에 스타일만 얹지 않고, 캡컷 원본 소재를 통째로 복제해 텍스트만 갈아끼웁니다."),
  ]);

  if (S.profile) {
    const p = S.profile;
    const r = p.readable || {};
    card.append(el("div", { class: "stat-row" }, [
      el("div", { class: "stat" }, [el("div", { class: "value", text: String(p.material_field_count || 0) }), el("div", { class: "label", text: "소재 필드 수" })]),
      el("div", { class: "stat" }, [el("div", { class: "value", text: String(p.sample_count || 0) }), el("div", { class: "label", text: "참조한 자막" })]),
      el("div", { class: `stat ${r.check_flag & 16 ? "ok" : "danger"}` }, [el("div", { class: "value", text: String(r.check_flag ?? "-") }), el("div", { class: "label", text: "check_flag" })]),
      el("div", { class: `stat ${(r.background_style || 0) >= 1 ? "ok" : "danger"}` }, [el("div", { class: "value", text: String(r.background_style ?? "-") }), el("div", { class: "label", text: "배경 스타일" })]),
    ]));

    const font = p.font || {};
    card.append(el("div", { class: "note " + (font.path ? "ok" : "danger") }, [
      el("span", { class: "what", text: font.path ? "글꼴 확인됨" : "글꼴 파일을 찾지 못했습니다" }),
      font.path ? `${font.name || ""}\n${font.path}`
        : "자막 글꼴이 시스템 기본으로 바뀝니다. 아래에서 설치된 글꼴을 직접 고르세요.",
    ]));

    if (p.estimated) card.append(note("warn", "추정값입니다", "수동으로 입력한 값이라 캡컷 실제 스타일과 다를 수 있습니다. 캡컷에서 자막을 만든 드래프트로 캘리브레이션하는 편이 정확합니다."));

    const stats = p.stats || {};
    if (stats.count) {
      card.append(note("info", "참조 드래프트의 자막 길이 분포",
        `자막 ${stats.count}건 · 중앙값 ${stats.median}자 · 90% ${stats.p90}자 · 최대 ${stats.max}자\n` +
        `모두 한 줄: ${stats.all_single_line ? "예" : "아니오 (최대 " + stats.max_lines + "줄)"}\n` +
        "이 도구는 항상 한 줄로 만들고 기본 상한은 36자입니다."));
    }
    card.append(el("div", { class: "small muted", text: `참조: ${p.source_draft_name || "-"} · 캡처 ${p.captured_at || "-"}` }));
    card.append(el("div", { class: "btn-row" }, [el("button", { class: "btn", onclick: () => pickFont() }, "글꼴 바꾸기")]));
  } else {
    card.append(note("warn", "아직 캘리브레이션하지 않았습니다", "아래에서 참조 드래프트를 고르세요."));
  }
  view.append(card);

  // ── 참조 드래프트 선택 ────────────────────────────────────────────────
  const pick = el("div", { class: "card" }, [
    el("h2", { text: "참조 드래프트 고르기" }),
    el("div", { class: "desc", text: "캡컷에서 자막(과 원하면 트랜지션)을 넣어 둔 프로젝트를 고르세요." }),
  ]);
  const holder = el("div", {}, [el("div", { class: "muted small", text: "드래프트 목록을 불러오는 중…" })]);
  pick.append(holder);
  view.append(pick);

  api("/api/calibration/drafts").then((data) => {
    holder.innerHTML = "";
    if (!data.drafts.length) {
      holder.append(note("warn", "드래프트가 없습니다", data.message || "캡컷에서 프로젝트를 하나 만든 뒤 다시 시도하세요."));
      return;
    }
    const select = el("select", { id: "refDraft" }, data.drafts.map((d) => el("option", { value: d.name, text: d.name })));
    holder.append(el("div", { class: "field" }, [el("label", { text: "드래프트" }), select]));
    holder.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          try {
            const res = await api("/api/calibration/capture", { method: "POST", body: { draft_name: select.value } });
            S.profile = res.profile;
            for (const n of res.notes || []) toast("warn", "확인하세요", n);
            toast("ok", "캘리브레이션 완료", `소재 필드 ${res.profile.material_field_count}개를 통째로 저장했습니다.`);
            await reloadSession(); render();
          } catch (err) { showError(err); }
        },
      }, "이 드래프트에서 가져오기"),
      el("button", {
        class: "btn", onclick: async () => {
          try {
            const res = await api("/api/calibration/transitions/capture", { method: "POST", body: { draft_name: select.value } });
            if (res.message) toast("warn", "트랜지션 없음", res.message);
            else toast("ok", "트랜지션 가져옴", res.transitions.map((t) => t.enum_name || t.effect_id).join(", "));
            render();
          } catch (err) { showError(err); }
        },
      }, "트랜지션만 가져오기"),
    ]));
  }).catch(showError);

  // ── 트랜지션 ─────────────────────────────────────────────────────────
  const tcard = el("div", { class: "card" }, [
    el("h2", { text: "트랜지션" }),
    el("div", { class: "desc", text: "트랜지션 이름은 대부분 중국어라 한국어 UI 이름과 매칭되지 않습니다. effect_id로만 정확히 찾습니다." }),
  ]);
  const thold = el("div", {});
  tcard.append(thold);
  view.append(tcard);
  api("/api/calibration/transitions").then((data) => {
    thold.innerHTML = "";
    if (!(data.captured || []).length) {
      thold.append(note("info", "가져온 트랜지션이 없습니다", data.note));
      return;
    }
    const list = el("div", { class: "list" });
    for (const t of data.captured) {
      list.append(el("div", { class: "list-item" }, [
        el("span", { class: "badge ok", text: t.enum_name || "매칭 실패" }),
        el("span", { class: "grow mono small", text: `effect_id ${t.effect_id}` }),
        el("span", { class: "meta", text: t.duration ? `${(t.duration / 1e6).toFixed(2)}초` : "" }),
      ]));
    }
    thold.append(list);
  }).catch(showError);

  // ── 수동 폴백 ────────────────────────────────────────────────────────
  const manual = el("div", { class: "card" }, [
    el("h2", { text: "수동 입력 (폴백)" }),
    el("div", { class: "desc", text: "참조 드래프트를 못 쓸 때만 사용하세요. 이 값은 추정값입니다." }),
  ]);
  const mf = {
    font_name: el("input", { type: "text", value: "Pretendard-Bold" }),
    font_size: el("input", { type: "number", step: "0.5", value: "5.0" }),
    text_color: el("input", { type: "text", value: "#000000" }),
    background_color: el("input", { type: "text", value: "#ffffff" }),
    bottom_y: el("input", { type: "number", step: "0.0001", value: "-0.7394" }),
    top_y: el("input", { type: "number", step: "0.0001", value: "0.6338" }),
  };
  manual.append(el("div", { class: "grid2" }, [
    el("div", { class: "field" }, [el("label", { text: "글꼴 이름" }), mf.font_name]),
    el("div", { class: "field" }, [el("label", { text: "글자 크기" }), mf.font_size]),
    el("div", { class: "field" }, [el("label", { text: "글자 색" }), mf.text_color]),
    el("div", { class: "field" }, [el("label", { text: "배경 색" }), mf.background_color]),
    el("div", { class: "field" }, [el("label", { text: "하단 자막 Y (정규화)" }), mf.bottom_y,
      el("div", { class: "hint", text: "화면 중앙이 0, 위쪽이 양수입니다. 1080 캔버스에서 -394px = -0.7296" })]),
    el("div", { class: "field" }, [el("label", { text: "상단 문구 Y (정규화)" }), mf.top_y]),
  ]));
  manual.append(el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn", onclick: async () => {
        try {
          const body = {};
          for (const [k, node] of Object.entries(mf)) body[k] = node.type === "number" ? Number(node.value) : node.value;
          if (S.sources) { body.canvas_width = S.sources.canvas.width; body.canvas_height = S.sources.canvas.height; }
          const res = await api("/api/calibration/manual", { method: "POST", body });
          S.profile = res.profile;
          for (const n of res.notes || []) toast("warn", "확인하세요", n);
          render();
        } catch (err) { showError(err); }
      },
    }, "수동 값으로 저장"),
  ]));
  view.append(manual);
};

async function pickFont() {
  let data;
  try { data = await api("/api/setup/fonts"); } catch (err) { showError(err); return; }
  const select = el("select", {}, data.fonts.map((f) => el("option", { value: f.path, text: `${f.stem}  (${f.file})` })));
  openModal("글꼴 고르기", el("div", {}, [
    note("info", "글꼴은 절대경로로만 참조됩니다",
      "캡컷은 font_path에 실제 파일의 절대경로를 씁니다. 이름만 맞고 파일이 없으면 시스템 기본 글꼴로 바뀝니다."),
    el("div", { class: "field" }, [el("label", { text: `설치된 글꼴 ${data.fonts.length}개` }), select]),
    el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          try {
            const opt = select.selectedOptions[0];
            const res = await api("/api/calibration/font", {
              method: "POST", body: { font_path: select.value, font_name: opt.text.split("  (")[0] },
            });
            S.profile = res.profile;
            toast("ok", "글꼴을 바꿨습니다", res.profile.font.path);
            closeModal(); render();
          } catch (err) { showError(err); }
        },
      }, "이 글꼴 쓰기"),
      el("button", { class: "btn", onclick: () => closeModal() }, "닫기"),
    ]),
  ]));
}

/* ══════════════════════════════════════════════════════════════════════
   1차 컷 편집
   ══════════════════════════════════════════════════════════════════════ */
VIEWS.cut = function (view) {
  const settings = (S.data.analyze_settings || {});
  const f = S.scratch.cutForm || (S.scratch.cutForm = {
    silence_threshold_db: settings.silence_threshold_db ?? setting("silence_threshold_db", -35),
    min_silence_sec: settings.min_silence_sec ?? setting("min_silence_sec", 0.6),
    tail_pad_sec: settings.tail_pad_sec ?? setting("tail_pad_sec", 0.35),
    head_pad_sec: settings.head_pad_sec ?? setting("head_pad_sec", 0.15),
    whisper_model: settings.whisper_model ?? setting("whisper_model", "medium"),
  });

  const card = el("div", { class: "card" }, [
    el("h2", { text: "1차 컷 편집" }),
    el("div", { class: "desc", text: "무음·필러워드·말더듬을 찾아 컷 후보를 만듭니다. 자동으로 잘라내지 않고 반드시 검수를 거칩니다." }),
  ]);

  const slider = (key, label, min, max, step, hint) => {
    const input = el("input", { type: "number", min, max, step, value: String(f[key]) });
    input.oninput = () => { f[key] = Number(input.value); const o = $(`#out-${key}`); if (o) o.textContent = input.value; };
    return el("div", { class: "field" }, [
      el("label", {}, [label, " ", el("span", { class: "muted small", id: `out-${key}`, text: String(f[key]) })]),
      input,
      hint ? el("div", { class: "hint", text: hint }) : null,
    ]);
  };

  card.append(el("div", { class: "grid2" }, [
    slider("silence_threshold_db", "무음 임계값 (dB)", -60, -10, 1, "이 값보다 조용하면 무음으로 봅니다."),
    slider("min_silence_sec", "최소 무음 길이 (초)", 0.2, 3, 0.1, "너무 공격적으로 잡으면 자연스러운 호흡까지 잘립니다."),
    slider("tail_pad_sec", "말 끝난 뒤 여유 (초)", 0, 1.5, 0.05, "짧으면 마지막 음절이 잘려 들립니다. 넉넉히 두세요."),
    slider("head_pad_sec", "다음 말 시작 전 여유 (초)", 0, 1.5, 0.05, "앞뒤 여유는 서로 다른 역할이라 따로 둡니다."),
  ]));
  card.append(note("info", "앞뒤 여유를 왜 따로 두나요",
    "무음 구간의 시작은 말이 끝난 직후, 끝은 다음 말 시작 직전입니다.\n" +
    "무음 감지는 소리가 임계값 아래로 떨어지는 순간을 무음 시작으로 보는데, 말끝의 자음과 여운은 이미 그 아래에 있습니다.\n" +
    "그래서 꼬리 여유를 머리 여유보다 크게 잡습니다. (기본 0.35 / 0.15)"));

  const models = (S.status && S.status.whisper_models) || ["medium"];
  if (!models.includes(f.whisper_model)) {
    // 저장된 모델이 목록에 없으면(버전이 바뀌었거나 값이 손상됨) 기본값으로 되돌립니다.
    // 그대로 두면 드롭다운에 보이는 것과 실제로 쓰는 모델이 달라집니다.
    f.whisper_model = setting("whisper_model", "medium");
    if (!models.includes(f.whisper_model)) f.whisper_model = models[0];
  }
  const modelSelect = el("select", {}, models.map(
    (m) => el("option", { value: m, text: m, selected: m === f.whisper_model })));
  modelSelect.onchange = () => { f.whisper_model = modelSelect.value; checkModel(); };
  card.append(el("div", { class: "field" }, [
    el("label", { text: "음성 인식 모델" }), modelSelect,
    el("div", { class: "hint", text: "GPU가 없으면 CPU로 돕니다. medium이 기본이며 large-v3는 훨씬 느립니다." }),
  ]));
  const modelNotice = el("div", { id: "modelNotice" });
  card.append(modelNotice);

  const reuse = el("input", { type: "checkbox", checked: true });
  card.append(el("label", { class: "checkline" }, [reuse, "이미 끝난 음성 인식 구간은 건너뛰기 (설정을 바꿨다면 체크 해제)"]));

  if (S.pending.analyze) {
    card.append(runningPanel("analyze", "분석 중입니다. 창을 닫아도 서버에서 계속 돌아갑니다."));
  } else {
    card.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          if (!(await guardCapcut())) return;
          runJob("analyze", () => api(`/api/editing/${S.session.id}/analyze`, {
            method: "POST", body: Object.assign({}, f, { reuse_stt: reuse.checked }),
          }), async () => { await reloadSession(); const c = await api(`/api/editing/${S.session.id}/candidates`); S.candidates = c.candidates; S.cutSummary = c.summary; render(); });
        },
      }, S.candidates.length ? "다시 분석" : "분석 시작"),
    ]));
  }
  view.append(card);
  checkModel();

  if (S.candidates.length) view.append(candidateCard());
  if (S.candidates.length) view.append(buildDraftCard());
};

async function checkModel() {
  const host = $("#modelNotice");
  if (!host) return;
  const f = S.scratch.cutForm || {};
  try {
    const st = await api(`/api/setup/model-status?model=${encodeURIComponent(f.whisper_model || "")}`);
    host.innerHTML = "";
    if (!st.downloaded) {
      host.append(note("warn", "모델을 먼저 내려받습니다", st.notice +
        "\n진행률이 한동안 0에 머물 수 있습니다. 받은 파일을 임시 폴더에 모았다가 마지막에 옮기기 때문입니다."));
    } else {
      host.append(note("ok", "모델 준비됨", `${st.model} (${st.cached_mb}MB) — 바로 시작합니다.`));
    }
  } catch (_) { /* 조용히 무시 */ }
}

function candidateCard() {
  const sum = S.cutSummary || {};
  const card = el("div", { class: "card" }, [
    el("h2", { text: "컷 검수" }),
    el("div", { class: "desc", text: "체크된 항목이 잘려나갑니다. 자동 판정이라 반드시 눈으로 확인하세요." }),
  ]);

  card.append(el("div", { class: "stat-row" }, [
    el("div", { class: "stat" }, [el("div", { class: "value", text: `${sum.selected_candidates}/${sum.total_candidates}` }), el("div", { class: "label", text: "선택된 컷" })]),
    el("div", { class: "stat ok" }, [el("div", { class: "value", text: fmtDur(sum.kept_duration) }), el("div", { class: "label", text: "남는 길이" })]),
    el("div", { class: `stat ${sum.cut_ratio_warn ? "danger" : ""}` }, [el("div", { class: "value", text: `${(sum.cut_ratio * 100).toFixed(1)}%` }), el("div", { class: "label", text: "잘리는 비율" })]),
    el("div", { class: "stat" }, [el("div", { class: "value", text: String(sum.segment_count) }), el("div", { class: "label", text: "구간 수" })]),
  ]));

  for (const w of sum.warnings || []) card.append(note("danger", "확인이 필요합니다", w));

  const bulk = el("div", { class: "btn-row" });
  for (const [type, label] of Object.entries(sum.type_labels || {})) {
    const info = (sum.by_type || {})[type];
    if (!info) continue;
    bulk.append(el("button", { class: "btn sm", onclick: () => bulkSelect("by_type", { type, selected: true }) }, `${label} 전체 선택 (${info.total})`));
    bulk.append(el("button", { class: "btn sm", onclick: () => bulkSelect("by_type", { type, selected: false }) }, `${label} 전체 해제`));
  }
  if (sum.long_silence_count) {
    bulk.append(el("button", { class: "btn sm primary", onclick: () => bulkSelect("revive_long_silence", {}) },
      `긴 무음(3초 이상) 살리기 (${sum.long_silence_count})`));
  }
  card.append(bulk);

  const tbody = el("tbody");
  for (const c of S.candidates) {
    const cb = el("input", { type: "checkbox", checked: c.selected });
    cb.onchange = () => bulkSelect("set", { selection: { [c.id]: cb.checked } });
    tbody.append(el("tr", {}, [
      el("td", {}, [cb]),
      el("td", {}, [el("span", { class: `badge ${c.type}`, text: c.type_label }), c.is_long_silence ? el("span", { class: "badge est", text: "긴 무음" }) : null]),
      el("td", { class: "num", text: `${fmtTime(c.start)} ~ ${fmtTime(c.end)}` }),
      el("td", { class: "num", text: `${c.duration.toFixed(2)}초` }),
      el("td", { class: "small" }, [
        el("div", { text: c.reason }),
        (c.context_before || c.context_after)
          ? el("div", { class: "muted", text: `…${c.context_before} ⟨잘림⟩ ${c.context_after}…` }) : null,
      ]),
      el("td", {}, [el("button", { class: "btn sm", onclick: () => playPreview(c) }, "미리듣기")]),
    ]));
  }
  card.append(el("div", { class: "table-wrap" }, [
    el("table", {}, [
      el("thead", {}, [el("tr", {}, ["", "유형", "구간", "길이", "근거 · 앞뒤 문맥", ""].map((h) => el("th", { text: h })))]),
      tbody,
    ]),
  ]));

  const low = S.data.stt && S.data.stt.low_confidence;
  if (low && low.length) {
    card.append(note("warn", `음성 인식 신뢰도가 낮은 구간 ${low.length}건`,
      "대본이 없으면 이 구간의 자막에 오타가 있을 수 있습니다. 2차 검수에서 확인하세요."));
  }
  return card;
}

async function bulkSelect(action, extra) {
  try {
    const res = await api(`/api/editing/${S.session.id}/candidates/selection`, {
      method: "POST", body: Object.assign({ action }, extra),
    });
    S.candidates = res.candidates; S.cutSummary = res.summary;
    if (res.signature_check && !res.signature_check.ok) {
      toast("warn", "드래프트와 어긋납니다", res.signature_check.message, 15000);
    }
    render();
  } catch (err) { showError(err); }
}

async function playPreview(c) {
  try {
    await api(`/api/editing/${S.session.id}/preview`, {
      method: "POST", body: { start: c.start, duration: c.duration, pad: 1.0 },
    });
    const audio = new Audio(`/api/editing/${S.session.id}/preview.wav?t=${Date.now()}`);
    audio.play().catch(() => toast("warn", "재생 실패", "브라우저가 자동 재생을 막았을 수 있습니다."));
  } catch (err) { showError(err); }
}

function buildDraftCard() {
  const card = el("div", { class: "card" }, [
    el("h2", { text: "드래프트 만들기" }),
    el("div", { class: "desc", text: "검수한 컷을 반영한 캡컷 드래프트를 만듭니다." }),
  ]);
  const nameInput = el("input", { type: "text", value: S.data.draft_name || `자동편집_${S.session.name}` });
  card.append(el("div", { class: "field" }, [el("label", { text: "드래프트 이름" }), nameInput]));

  card.append(note("info", "이 시점의 컷 설정을 기록합니다",
    "자막 시각은 '지금 컷 설정' 기준으로 계산되는데 드래프트는 '만들 당시 컷 설정'입니다.\n" +
    "드래프트를 만든 뒤 컷 선택을 바꾸고 자막만 넣으면 앞은 맞고 뒤로 갈수록 어긋납니다.\n" +
    "그래서 여기서 컷 서명(남는 길이 + 구간 수)을 기록하고, 2차에서 비교해 다르면 차단합니다."));

  if (S.data.cut_signature) {
    const sig = S.data.cut_signature;
    card.append(note("ok", "기록된 컷 서명", `${sig.kept_duration}초 / ${sig.segment_count}개 구간`));
  }

  if (S.pending.build_draft) {
    card.append(runningPanel("build_draft"));
  } else {
    card.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          if (!(await guardCapcut())) return;
          runJob("build_draft", () => api(`/api/editing/${S.session.id}/build-draft`, {
            method: "POST", body: { draft_name: nameInput.value },
          }), async (result) => {
            await reloadSession();
            let msg = `세그먼트 ${result.segment_count}개 · ${fmtDur(result.timeline_duration)}`;
            if ((result.skipped || []).length) msg += `\n건너뛴 구간 ${result.skipped.length}개`;
            if ((result.clamped || []).length) msg += `\n소재 길이 초과로 잘린 구간 ${result.clamped.length}개`;
            if (result.postprocess) {
              const rf = result.postprocess.render_index_fixed || {};
              msg += `\n레이어 교정: 트랙 ${rf.tracks || 0} / 세그먼트 ${rf.segments || 0}`;
              msg += `\n프로젝트 목록 등록: ${result.postprocess.registered ? "완료" : "건너뜀"}`;
            }
            toast("ok", "드래프트를 만들었습니다", msg, 12000);
            if (result.capcut_warning) toast("danger", "주의", result.capcut_warning, 20000);
            S.active = "subtitle"; render();
          });
        },
      }, S.data.draft_name ? "드래프트 다시 만들기" : "드래프트 만들기"),
    ]));
  }
  return card;
}

/* ══════════════════════════════════════════════════════════════════════
   2차 자막 생성
   ══════════════════════════════════════════════════════════════════════ */
VIEWS.subtitle = function (view) {
  const card = el("div", { class: "card" }, [
    el("h2", { text: "2차 자막 생성" }),
    el("div", { class: "desc", text: "대본이 있으면 대본을 정본으로, 타임코드는 음성 인식에서 가져옵니다. 자막은 항상 한 줄입니다." }),
  ]);

  // 대본
  const script = S.data.script;
  card.append(el("h3", { text: "대본 (선택)" }));
  if (script) {
    card.append(el("div", { class: "list" }, [
      el("div", { class: "list-item" }, [
        el("span", { class: "badge ok", text: "연결됨" }),
        el("span", { class: "grow", text: script.name }),
        el("span", { class: "meta", text: `${script.line_count}문장` }),
        el("button", {
          class: "btn sm danger", onclick: async () => {
            try { await api(`/api/editing/${S.session.id}/script`, { method: "DELETE" }); await reloadSession(); render(); }
            catch (err) { showError(err); }
          },
        }, "해제"),
      ]),
    ]));
  } else {
    const file = el("input", { type: "file", accept: ".txt,.srt,.md" });
    file.onchange = async () => {
      if (!file.files.length) return;
      const fd = new FormData();
      fd.append("file", file.files[0]);
      try {
        const res = await api(`/api/editing/${S.session.id}/script`, { method: "POST", body: fd });
        if (res.message) toast("warn", "확인하세요", res.message);
        else toast("ok", "대본 연결됨", `${res.line_count}문장을 읽었습니다.`);
        await reloadSession(); render();
      } catch (err) { showError(err); }
    };
    card.append(el("div", { class: "field" }, [file,
      el("div", { class: "hint", text: "txt · srt · md를 지원합니다. 없으면 음성 인식 결과만 씁니다 (신뢰도 낮은 구간을 표시합니다)." })]));
  }

  // 컷 서명 확인
  const sig = S.data.cut_signature;
  if (!sig) {
    card.append(note("danger", "드래프트가 없습니다", "1차에서 드래프트를 먼저 만드세요."));
  }

  const maxChars = el("input", { type: "number", min: 10, max: 60, value: String(setting("subtitle_max_chars", 36)) });
  card.append(el("div", { class: "field mt3" }, [
    el("label", { text: "한 줄 상한 (자)" }), maxChars,
    el("div", { class: "hint", text: "참조 드래프트 실측 기준 기본 36자입니다. 상한을 넘는 구절은 어절 단위로 고르게 나눕니다." }),
  ]));

  card.append(el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn primary", disabled: !sig, onclick: () => buildSubtitles(Number(maxChars.value), false),
    }, S.subtitles.length ? "자막 다시 만들기" : "자막 만들기"),
  ]));
  view.append(card);

  if (S.subtitles.length) view.append(subtitleTableCard());
  if (S.subtitles.length) view.append(injectCard());
};

async function buildSubtitles(maxChars, force) {
  try {
    const res = await api(`/api/editing/${S.session.id}/subtitles`, {
      method: "POST", body: { max_chars: maxChars, force: !!force },
    });
    S.subtitles = res.subtitles; S.subtitleMeta = res.meta;
    const st = res.stats;
    toast("ok", "자막을 만들었습니다",
      `${st.total}건 (대본 기준 ${st.from_script} · 즉흥 ${st.from_stt})\n` +
      `모두 한 줄: ${res.meta.lines.all_single_line ? "예" : "아니오"} · 상한 초과 ${res.meta.lines.over_limit}건`);
    const san = res.meta.sanitize || {};
    if (san.fixed_overlap || san.merged_punct || san.dropped) {
      toast("info", "자동 정리",
        `겹침 ${san.fixed_overlap || 0}건 · 부호 조각 병합 ${san.merged_punct || 0}건 · 버림 ${san.dropped || 0}건`);
    }
    await reloadSession(); render();
  } catch (err) {
    if (err.status === 409) {
      // 컷 서명 불일치 — 요청서 3.18
      const box = el("div", {}, [
        note("danger", "컷 설정이 드래프트와 다릅니다", err.message),
        el("div", { class: "btn-row" }, [
          el("button", { class: "btn", onclick: () => { closeModal(); S.active = "cut"; render(); } }, "1차로 가서 드래프트 다시 만들기"),
          el("button", {
            class: "btn danger", onclick: () => { closeModal(); buildSubtitles(maxChars, true); },
          }, "그래도 진행 (자막이 어긋납니다)"),
        ]),
      ]);
      openModal("자막 싱크가 어긋납니다", box);
      return;
    }
    showError(err);
  }
}

function subtitleTableCard() {
  const meta = S.subtitleMeta || {};
  const lines = meta.lines || {};
  const card = el("div", { class: "card" }, [
    el("h2", { text: "자막 검수" }),
    el("div", { class: "desc", text: "텍스트를 직접 고칠 수 있습니다. 고친 뒤에는 겹침을 자동으로 다시 정리합니다." }),
  ]);

  card.append(el("div", { class: "stat-row" }, [
    el("div", { class: "stat" }, [el("div", { class: "value", text: String(lines.count || 0) }), el("div", { class: "label", text: "자막 수" })]),
    el("div", { class: `stat ${lines.over_limit ? "warn" : "ok"}` }, [el("div", { class: "value", text: String(lines.over_limit || 0) }), el("div", { class: "label", text: "상한 초과" })]),
    el("div", { class: `stat ${lines.multiline ? "danger" : "ok"}` }, [el("div", { class: "value", text: String(lines.multiline || 0) }), el("div", { class: "label", text: "여러 줄" })]),
    el("div", { class: "stat" }, [el("div", { class: "value", text: String(lines.max_len || 0) }), el("div", { class: "label", text: "최대 길이" })]),
  ]));

  const edits = {};
  const deletes = new Set();
  const tbody = el("tbody");
  for (const s of S.subtitles) {
    const input = el("input", { type: "text", value: s.text });
    input.oninput = () => { edits[s.id] = { id: s.id, text: input.value }; };
    tbody.append(el("tr", {}, [
      el("td", { class: "num", text: `${fmtTime(s.start)}\n${fmtTime(s.end)}` }),
      el("td", { class: "num", text: `${s.duration.toFixed(2)}s` }),
      el("td", {}, [el("span", { class: `badge ${s.source === "script" ? "ok" : "filler"}`, text: s.source === "script" ? "대본" : "STT" })]),
      el("td", { style: "width:52%" }, [input]),
      el("td", { class: "num", text: `${s.length}자` }),
      el("td", {}, [el("button", {
        class: "btn sm danger", onclick: (e) => { deletes.add(s.id); e.target.closest("tr").style.opacity = ".4"; },
      }, "삭제")]),
    ]));
  }
  card.append(el("div", { class: "table-wrap" }, [
    el("table", {}, [
      el("thead", {}, [el("tr", {}, ["시각", "길이", "출처", "자막", "글자", ""].map((h) => el("th", { text: h })))]),
      tbody,
    ]),
  ]));

  card.append(el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn primary", onclick: async () => {
        try {
          const res = await api(`/api/editing/${S.session.id}/subtitles/update`, {
            method: "POST", body: { edits: Object.values(edits), delete: [...deletes] },
          });
          S.subtitles = res.subtitles; S.subtitleMeta = res.meta;
          for (const w of res.warnings || []) toast("warn", "확인하세요", w);
          toast("ok", "수정 반영됨", `자막 ${res.subtitles.length}건`);
          await reloadSession(); render();
        } catch (err) { showError(err); }
      },
    }, "수정 저장"),
    el("button", {
      class: "btn", onclick: () => {
        window.open(`/api/editing/${S.session.id}/subtitles/srt`, "_blank");
      },
    }, "SRT 내보내기"),
  ]));
  return card;
}

function injectCard() {
  const card = el("div", { class: "card" }, [
    el("h2", { text: "드래프트에 자막 넣기" }),
    el("div", { class: "desc", text: "넣은 뒤 파일을 다시 읽어 실제로 반영됐는지 확인합니다." }),
  ]);
  if (!S.profile) {
    card.append(note("danger", "캘리브레이션이 필요합니다",
      "자막 스타일 캘리브레이션을 먼저 하세요. 이게 없으면 자막이 캡컷 화면에 제대로 보이지 않습니다."));
    return card;
  }

  const pos = el("select", {}, [
    el("option", { value: "bottom", text: "하단 (기본)" }),
    el("option", { value: "top", text: "상단" }),
  ]);
  card.append(el("div", { class: "field" }, [el("label", { text: "자막 위치" }), pos]));

  if (S.pending.inject_subtitles) {
    card.append(runningPanel("inject_subtitles"));
  } else {
    card.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          if (!(await guardCapcut())) return;
          runJob("inject_subtitles", () => api(`/api/editing/${S.session.id}/subtitles/inject`, {
            method: "POST", body: { position: pos.value },
          }), async (result) => {
            await reloadSession();
            if (result.ok) {
              toast("ok", "자막을 넣었습니다",
                `요청 ${result.requested}건 중 파일에서 확인된 자막 ${result.verified}건\n` +
                "캡컷에서 열어 화면에 실제로 보이는지 확인하세요.");
            } else {
              toast("danger", `자막 ${result.verified}건은 들어갔지만 문제가 있습니다`,
                result.problems.join("\n"), 20000);
            }
            if (result.capcut_warning) toast("danger", "주의", result.capcut_warning, 20000);
            render();
          });
        },
      }, "자막 넣기"),
    ]));
  }
  card.append(note("info", "캡컷에서 확인할 때",
    "이미 캡컷에 열어 둔 프로젝트라면 완전히 종료했다가 다시 열어야 반영됩니다.\n" +
    "자막이 파일에는 있는데 화면에 안 보이면 레이어 문제, 흰 배경이 안 나오면 배경 비트 문제입니다. 둘 다 도구가 저장 후 검사합니다."));
  return card;
}

/* ══════════════════════════════════════════════════════════════════════
   3차 트랜지션 및 효과음
   ══════════════════════════════════════════════════════════════════════ */
VIEWS.transition = function (view) {
  const card = el("div", { class: "card" }, [
    el("h2", { text: "3차 트랜지션 및 효과음" }),
    el("div", { class: "desc", text: "드래프트의 이미지 클립 앞뒤에 트랜지션을 넣고, 로컬 효과음을 배치합니다." }),
    note("info", "트랜지션은 앞쪽 세그먼트에 붙습니다",
      "pyCapCut 규칙입니다. '이미지 앞에 넣기'는 곧 이전 클립에 붙이는 것과 같습니다.\n" +
      "적용 뒤에는 캡컷에서 육안으로 확인하세요. pyCapCut은 베타라 의도대로 적용되지 않는 사례가 보고돼 있습니다."),
  ]);
  const holder = el("div", {}, [el("div", { class: "muted small", text: "이미지 클립을 찾는 중…" })]);
  card.append(holder);
  view.append(card);

  Promise.all([
    api(`/api/publishing/${S.session.id}/image-clips`),
    api("/api/calibration/transitions"),
  ]).then(([clips, trans]) => {
    holder.innerHTML = "";
    if (!clips.clips.length) { holder.append(note("warn", "이미지 클립이 없습니다", clips.message)); return; }

    const options = [];
    for (const t of trans.captured || []) {
      options.push(el("option", { value: t.effect_id, text: `${t.enum_name || "?"} (가져온 것)` }));
    }
    for (const t of trans.all || []) options.push(el("option", { value: t.effect_id, text: t.name }));

    const selections = [];
    const tbody = el("tbody");
    for (const clip of clips.clips) {
      const sel = el("select", {}, options.map((o) => o.cloneNode(true)));
      const before = el("input", { type: "checkbox", disabled: !clip.has_prev });
      const after = el("input", { type: "checkbox", disabled: !clip.has_next });
      selections.push({ clip, sel, before, after });
      tbody.append(el("tr", {}, [
        el("td", { text: clip.name }),
        el("td", { class: "num", text: `${fmtTime(clip.start)} (${clip.duration.toFixed(1)}s)` }),
        el("td", {}, [el("label", { class: "checkline" }, [before, "앞"])]),
        el("td", {}, [el("label", { class: "checkline" }, [after, "뒤"])]),
        el("td", { style: "width:40%" }, [sel]),
      ]));
    }
    holder.append(el("div", { class: "table-wrap" }, [
      el("table", {}, [
        el("thead", {}, [el("tr", {}, ["이미지 클립", "위치", "앞", "뒤", "트랜지션"].map((h) => el("th", { text: h })))]),
        tbody,
      ]),
    ]));

    const dur = el("input", { type: "number", step: "0.1", min: "0.1", max: "3", value: String(setting("transition_duration_sec", 0.5)) });
    holder.append(el("div", { class: "field mt3" }, [el("label", { text: "트랜지션 길이 (초)" }), dur]));

    holder.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          if (!(await guardCapcut())) return;
          const payload = [];
          for (const s of selections) {
            if (s.before.checked) payload.push({ track_index: s.clip.track_index, segment_index: s.clip.segment_index, side: "before", effect_id: s.sel.value, name: s.clip.name });
            if (s.after.checked) payload.push({ track_index: s.clip.track_index, segment_index: s.clip.segment_index, side: "after", effect_id: s.sel.value, name: s.clip.name });
          }
          if (!payload.length) { toast("warn", "선택 없음", "앞/뒤 중 하나 이상을 체크하세요."); return; }
          try {
            const res = await api(`/api/publishing/${S.session.id}/transitions`, {
              method: "POST", body: { selections: payload, duration_sec: Number(dur.value) },
            });
            toast("ok", `트랜지션 ${res.applied}개 적용`, (res.applied_labels || []).join("\n") + "\n\n" + res.note, 14000);
            for (const p of res.problems || []) toast("warn", "적용하지 못한 항목", p);
            await reloadSession(); render();
          } catch (err) { showError(err); }
        },
      }, "트랜지션 적용"),
    ]));
  }).catch(showError);

  // ── 효과음 ───────────────────────────────────────────────────────────
  const sfxCard = el("div", { class: "card" }, [
    el("h2", { text: "효과음" }),
    el("div", { class: "desc", text: "assets/sfx 폴더의 wav 파일을 배치합니다." }),
  ]);
  const sfxHolder = el("div", {});
  sfxCard.append(sfxHolder);
  view.append(sfxCard);

  api("/api/files/assets").then((assets) => {
    sfxHolder.innerHTML = "";
    if (!assets.sfx.length) { sfxHolder.append(note("warn", "효과음이 없습니다", assets.sfx_hint)); return; }
    const rows = [];
    const list = el("div", { class: "list" });
    const addRow = () => {
      const sel = el("select", {}, assets.sfx.map((s) => el("option", { value: s.path, text: s.name })));
      const at = el("input", { type: "number", step: "0.1", min: "0", value: "0" });
      const vol = el("input", { type: "number", step: "0.05", min: "0", max: "2", value: String(setting("sfx_volume", 0.6)) });
      const row = el("div", { class: "list-item" }, [
        el("span", { style: "flex:2" }, [sel]),
        el("span", { class: "small muted", text: "시각" }), at,
        el("span", { class: "small muted", text: "볼륨" }), vol,
        el("button", { class: "btn sm danger", onclick: () => { row.remove(); const i = rows.findIndex((r) => r.row === row); if (i >= 0) rows.splice(i, 1); } }, "✕"),
      ]);
      rows.push({ row, sel, at, vol });
      list.append(row);
    };
    sfxHolder.append(list);
    sfxHolder.append(el("div", { class: "btn-row" }, [
      el("button", { class: "btn sm", onclick: addRow }, "효과음 추가"),
      el("button", {
        class: "btn primary", onclick: async () => {
          if (!rows.length) { toast("warn", "선택 없음", "효과음을 추가하세요."); return; }
          if (!(await guardCapcut())) return;
          try {
            const res = await api(`/api/publishing/${S.session.id}/sfx`, {
              method: "POST",
              body: { placements: rows.map((r) => ({ path: r.sel.value, start: Number(r.at.value), volume: Number(r.vol.value) })) },
            });
            toast("ok", `효과음 ${res.applied}개 배치`, "");
            for (const p of res.problems || []) toast("warn", "확인하세요", p);
          } catch (err) { showError(err); }
        },
      }, "효과음 넣기"),
    ]));
    addRow();
  }).catch(showError);
};

/* ══════════════════════════════════════════════════════════════════════
   4차 내보내기 및 마케팅 문구
   ══════════════════════════════════════════════════════════════════════ */
VIEWS.export = function (view) {
  const holder = el("div", {}, [el("div", { class: "muted small", text: "불러오는 중…" })]);
  view.append(holder);

  api(`/api/publishing/${S.session.id}/export-info`).then((info) => {
    holder.innerHTML = "";

    const card = el("div", { class: "card" }, [
      el("h2", { text: "4차 내보내기" }),
      note("ok", "드래프트 저장 완료", info.guidance),
    ]);

    const details = el("details", {}, [
      el("summary", { class: "small muted", style: "cursor:pointer", text: "자동 내보내기 (미검증, 기본 꺼짐)" }),
    ]);
    details.append(note("danger", "쓰기 전에 반드시 읽으세요", info.auto_export_warning));
    const confirm1 = el("input", { type: "checkbox" });
    const outPath = el("input", { type: "text", placeholder: "C:/출력/영상.mp4" });
    details.append(el("label", { class: "checkline" }, [confirm1, "위 내용을 이해했고 미검증 기능임을 감수합니다"]));
    details.append(el("div", { class: "field" }, [el("label", { text: "저장할 파일 경로" }), outPath]));
    details.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn danger", onclick: () => {
          if (!confirm1.checked) { toast("warn", "확인란 필요", "확인란을 체크해야 실행됩니다."); return; }
          runJob("auto_export", () => api(`/api/publishing/${S.session.id}/auto-export`, {
            method: "POST", body: { confirmed: true, output_path: outPath.value },
          }), (r) => { toast("ok", "내보내기 완료", r.output); render(); });
        },
      }, "자동 내보내기 실행"),
    ]));
    if (S.pending.auto_export) details.append(runningPanel("auto_export"));
    card.append(details);
    holder.append(card);

    // ── 마케팅 문구 프롬프트 ──────────────────────────────────────────
    const pcard = el("div", { class: "card" }, [
      el("h2", { text: "마케팅 문구 프롬프트" }),
      el("div", { class: "desc", text: "외부 AI를 부르지 않습니다. 완성된 프롬프트를 만들어 드릴 테니 원하는 AI에 붙여넣으세요." }),
    ]);

    const answers = {};
    for (const q of info.questions) {
      const input = el("textarea", { placeholder: q.placeholder, style: "min-height:56px" });
      input.value = (info.answers || {})[q.key] || "";
      answers[q.key] = input;
      pcard.append(el("div", { class: "field" }, [el("label", { text: q.label }), input]));
    }

    const ch = {};
    const chDefaults = info.channel_defaults || {};
    const chSaved = info.channel || {};
    for (const [key, label] of [["channel_name", "채널 이름"], ["audience", "타겟 시청자"], ["tone", "톤 가이드"]]) {
      const input = el("textarea", { style: "min-height:48px" });
      input.value = chSaved[key] || chDefaults[key] || "";
      ch[key] = input;
      pcard.append(el("div", { class: "field" }, [el("label", { text: label }), input]));
    }

    const out = el("textarea", { style: "min-height:280px", class: "mono small", readonly: true });
    pcard.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          try {
            const res = await api(`/api/publishing/${S.session.id}/prompt`, {
              method: "POST",
              body: {
                answers: Object.fromEntries(Object.entries(answers).map(([k, v]) => [k, v.value])),
                channel: Object.fromEntries(Object.entries(ch).map(([k, v]) => [k, v.value])),
              },
            });
            out.value = res.prompt;
            for (const w of res.warnings || []) toast("warn", "확인하세요", w);
            toast("ok", "프롬프트를 만들었습니다", "복사해서 원하는 AI에 붙여넣으세요.");
            await reloadSession();
          } catch (err) { showError(err); }
        },
      }, "프롬프트 만들기"),
      el("button", { class: "btn", onclick: () => copyText(out.value) }, "클립보드 복사"),
      el("button", { class: "btn", onclick: () => window.open(`/api/publishing/${S.session.id}/prompt.txt?kind=youtube`, "_blank") }, "txt로 저장"),
    ]));
    pcard.append(el("div", { class: "field mt3" }, [el("label", { text: "완성된 프롬프트" }), out]));
    holder.append(pcard);
  }).catch(showError);
};

async function copyText(text) {
  if (!text) { toast("warn", "복사할 내용이 없습니다", "먼저 프롬프트를 만드세요."); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast("ok", "복사했습니다", "");
  } catch (_) {
    toast("warn", "클립보드 접근 실패", "텍스트 상자에서 직접 선택해 복사하세요.");
  }
}

/* ══════════════════════════════════════════════════════════════════════
   5차 세로용 영상
   ══════════════════════════════════════════════════════════════════════ */
VIEWS.vertical = function (view) {
  const holder = el("div", {}, [el("div", { class: "muted small", text: "불러오는 중…" })]);
  view.append(holder);

  api(`/api/publishing/${S.session.id}/vertical/timeline`).then((data) => {
    holder.innerHTML = "";
    const card = el("div", { class: "card" }, [
      el("h2", { text: "5차 세로용 영상" }),
      el("div", { class: "desc", text: "자막 타임라인에서 구간을 고르면 1080x1920 드래프트를 만듭니다." }),
      note("info", "여기 시각은 편집본 기준입니다", data.note),
    ]);

    // 끝 기본값은 편집본 길이를 넘지 않게 잡습니다.
    // 25초를 그대로 두면 짧은 영상에서 존재하지 않는 구간을 가리킵니다.
    const total = Number(data.edited_duration) || 0;
    const defaultEnd = total > 0 ? Math.min(25, total) : 25;
    const start = el("input", { type: "number", step: "0.1", min: "0", value: "0" });
    const end = el("input", { type: "number", step: "0.1", min: "0", max: String(total || ""),
                              value: defaultEnd.toFixed(1) });
    card.append(el("div", { class: "grid2" }, [
      el("div", { class: "field" }, [el("label", { text: "시작 (초)" }), start]),
      el("div", { class: "field" }, [el("label", { text: "끝 (초)" }), end]),
    ]));
    card.append(el("div", { class: "small muted", text: `편집본 전체 길이: ${fmtDur(data.edited_duration)}` }));

    const tbody = el("tbody");
    for (const s of data.subtitles) {
      tbody.append(el("tr", {}, [
        el("td", { class: "num", text: fmtTime(s.start) }),
        el("td", { text: s.text }),
        el("td", {}, [el("button", { class: "btn sm", onclick: () => { start.value = s.start.toFixed(1); } }, "시작으로")]),
        el("td", {}, [el("button", { class: "btn sm", onclick: () => { end.value = s.end.toFixed(1); } }, "끝으로")]),
      ]));
    }
    card.append(el("div", { class: "table-wrap mt3" }, [
      el("table", {}, [el("thead", {}, [el("tr", {}, ["시각", "자막", "", ""].map((h) => el("th", { text: h })))]), tbody]),
    ]));

    const previewBox = el("div", { class: "mt3" });
    card.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn", onclick: async () => {
          try {
            const res = await api(`/api/publishing/${S.session.id}/vertical/preview-span`, {
              method: "POST", body: { start: Number(start.value), end: Number(end.value) },
            });
            previewBox.innerHTML = "";
            if (res.hint) previewBox.append(note("warn", "길이 안내", res.hint));
            previewBox.append(note("info", "원본으로 되돌린 구간",
              `${res.split_note}\n` + res.pieces.map((p) => `· ${p.name} ${p.local_start.toFixed(2)}~${p.local_end.toFixed(2)}초`).join("\n")));
          } catch (err) { showError(err); }
        },
      }, "원본 구간 미리 확인"),
    ]));
    card.append(previewBox);

    const bg = el("select", {}, [el("option", { value: "", text: "(배경 없음)" })].concat(
      (data.backgrounds || []).map((b) => el("option", { value: b.path, text: b.name }))));
    card.append(el("div", { class: "field mt3" }, [
      el("label", { text: "배경 이미지" }), bg,
      el("div", { class: "hint", text: `${data.bg_dir} 폴더의 이미지가 보입니다. 배경은 최하단에 깔리고 본편은 scale ${data.vertical.scale}, y ${data.vertical.transform_y}로 얹힙니다.` }),
    ]));

    const nameInput = el("input", { type: "text", value: `${S.data.draft_name || S.session.name}_세로` });
    card.append(el("div", { class: "field" }, [el("label", { text: "드래프트 이름" }), nameInput]));

    if (S.pending.build_vertical) {
      card.append(runningPanel("build_vertical"));
    } else {
      card.append(el("div", { class: "btn-row" }, [
        el("button", {
          class: "btn primary", onclick: async () => {
            if (!(await guardCapcut())) return;
            runJob("build_vertical", () => api(`/api/publishing/${S.session.id}/vertical/build`, {
              method: "POST",
              body: { start: Number(start.value), end: Number(end.value), background: bg.value, draft_name: nameInput.value },
            }), async (r) => {
              await reloadSession();
              let msg = `${r.canvas.width}×${r.canvas.height} · ${fmtDur(r.duration)} · 세그먼트 ${r.segment_count}개`;
              if (r.subtitles && r.subtitles.verified !== undefined) msg += `\n자막 확인 ${r.subtitles.verified}건`;
              toast("ok", "세로 드래프트를 만들었습니다", msg, 12000);
              if (r.capcut_warning) toast("danger", "주의", r.capcut_warning, 20000);
              render();
            });
          },
        }, "세로 드래프트 만들기"),
      ]));
    }
    holder.append(card);

    // ── 상단/하단 문구 프롬프트 ───────────────────────────────────────
    const pcard = el("div", { class: "card" }, [
      el("h2", { text: "상단 / 하단 문구 프롬프트" }),
      el("div", { class: "desc", text: "쇼츠용과 릴스용이 다릅니다. 릴스는 프로필 링크 CTA가 필수입니다." }),
    ]);
    const platform = el("select", {}, [
      el("option", { value: "shorts", text: "유튜브 쇼츠" }),
      el("option", { value: "reels", text: "인스타그램 릴스" }),
    ]);
    const link = el("input", { type: "text", placeholder: "https://instagram.com/..." });
    const pout = el("textarea", { style: "min-height:240px", class: "mono small", readonly: true });
    pcard.append(el("div", { class: "grid2" }, [
      el("div", { class: "field" }, [el("label", { text: "플랫폼" }), platform]),
      el("div", { class: "field" }, [el("label", { text: "프로필 링크 (릴스 필수)" }), link]),
    ]));
    pcard.append(el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          try {
            const res = await api(`/api/publishing/${S.session.id}/vertical/prompt`, {
              method: "POST",
              body: { start: Number(start.value), end: Number(end.value), platform: platform.value, profile_link: link.value },
            });
            pout.value = res.prompt;
            for (const w of res.warnings || []) toast("warn", "확인하세요", w);
          } catch (err) { showError(err); }
        },
      }, "프롬프트 만들기"),
      el("button", { class: "btn", onclick: () => copyText(pout.value) }, "클립보드 복사"),
      el("button", { class: "btn", onclick: () => window.open(`/api/publishing/${S.session.id}/prompt.txt?kind=vertical`, "_blank") }, "txt로 저장"),
    ]));
    pcard.append(el("div", { class: "field mt3" }, [pout]));
    holder.append(pcard);
  }).catch(showError);
};

/* ══════════════════════════════════════════════════════════════════════
   모달 · 세션 목록 · 설정 · 로그
   ══════════════════════════════════════════════════════════════════════ */
function openModal(title, body) {
  const root = $("#modalRoot");
  root.innerHTML = "";
  const backdrop = el("div", {
    class: "modal-backdrop",
    onclick: (e) => { if (e.target === backdrop) closeModal(); },
  }, [el("div", { class: "modal" }, [el("h2", { text: title }), body])]);
  root.append(backdrop);
}
function closeModal() { $("#modalRoot").innerHTML = ""; }

async function openSessionList(auto) {
  let data;
  try { data = await api("/api/sessions"); } catch (err) { showError(err); return; }

  if (auto && !data.sessions.length) {
    try {
      const res = await api("/api/sessions", { method: "POST", body: { name: "" } });
      await loadSession(res.session.id);
    } catch (err) { showError(err); }
    return;
  }
  if (auto && data.sessions.length) {
    await loadSession(data.sessions[0].id);
    return;
  }

  const list = el("div", { class: "list" });
  for (const s of data.sessions) {
    list.append(el("div", { class: "list-item" }, [
      el("span", { class: "grow" }, [
        el("div", { text: s.name }),
        el("div", { class: "small muted", text: `${s.progress_label} · 영상 ${s.clip_count}개 · ${fmtDur(s.total_duration)}` }),
      ]),
      el("button", { class: "btn sm", onclick: async () => { await loadSession(s.id); closeModal(); render(); } }, "열기"),
      el("button", {
        class: "btn sm danger", onclick: async () => {
          if (!confirm(`'${s.name}' 세션을 삭제합니다. 작업 파일도 함께 지워집니다.\n(캡컷 드래프트와 백업은 남습니다) 진행할까요?`)) return;
          try {
            await api(`/api/sessions/${s.id}`, { method: "DELETE" });
            if (S.session && S.session.id === s.id) { S.session = null; localStorage.removeItem("capcut_session_id"); }
            closeModal(); openSessionList(false); render();
          } catch (err) { showError(err); }
        },
      }, "삭제"),
    ]));
  }
  if (!data.sessions.length) list.append(el("div", { class: "list-item muted" }, "세션이 없습니다."));

  openModal("세션 목록", el("div", {}, [
    list,
    el("div", { class: "btn-row" }, [
      el("button", { class: "btn primary", onclick: () => { closeModal(); newSession(); } }, "새 세션"),
      el("button", { class: "btn", onclick: () => closeModal() }, "닫기"),
    ]),
  ]));
}

async function openSettings() {
  await refreshStatus();
  const st = S.status || { settings: {} };
  const body = el("div", {});

  body.append(note("info", "환경 상태",
    `Python ${st.python ? st.python.version : "?"}${st.python && st.python.supported ? "" : " (검증 버전은 3.11)"}\n` +
    `pycapcut ${st.versions ? st.versions.pycapcut : "?"} (검증 ${st.versions ? st.versions.pycapcut_verified : "?"})\n` +
    `검증된 캡컷 버전 ${st.capcut_verified}\n` +
    `ffmpeg ${st.media && st.media.ffmpeg ? st.media.ffmpeg : "찾지 못함"}\n` +
    `ffprobe ${st.media && st.media.ffprobe ? st.media.ffprobe : "찾지 못함"}`));

  const fields = {
    draft_root: el("input", { type: "text", value: st.settings.draft_root || st.draft_root || "" }),
    ffmpeg_path: el("input", { type: "text", value: st.settings.ffmpeg_path || "" }),
    ffprobe_path: el("input", { type: "text", value: st.settings.ffprobe_path || "" }),
    subtitle_max_chars: el("input", { type: "number", value: String(st.settings.subtitle_max_chars ?? 36) }),
    vertical_scale: el("input", { type: "number", step: "0.05", value: String(st.settings.vertical_scale ?? 1.8) }),
    vertical_transform_y: el("input", { type: "number", step: "0.000001", value: String(st.settings.vertical_transform_y ?? -0.078125) }),
  };
  const labels = {
    draft_root: "캡컷 드래프트 폴더 (비우면 자동 탐지)",
    ffmpeg_path: "ffmpeg 경로 (비우면 자동 탐지)",
    ffprobe_path: "ffprobe 경로 (비우면 자동 탐지)",
    subtitle_max_chars: "자막 한 줄 상한 (자)",
    vertical_scale: "세로 영상 본편 확대 배율",
    vertical_transform_y: "세로 영상 본편 Y 위치 (정규화)",
  };
  for (const [key, node] of Object.entries(fields)) {
    body.append(el("div", { class: "field" }, [el("label", { text: labels[key] }), node]));
  }

  body.append(el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn primary", onclick: async () => {
        const patch = {};
        for (const [k, node] of Object.entries(fields)) patch[k] = node.type === "number" ? Number(node.value) : node.value;
        try {
          await api("/api/setup/settings", { method: "POST", body: patch });
          await refreshStatus();
          toast("ok", "저장했습니다", "");
          closeModal(); render();
        } catch (err) { showError(err); }
      },
    }, "저장"),
    el("button", { class: "btn", onclick: () => openFillerWords() }, "필러워드 사전"),
    el("button", { class: "btn", onclick: () => openGlossary() }, "영어 용어 사전"),
    el("button", { class: "btn", onclick: () => closeModal() }, "닫기"),
  ]));
  openModal("설정", body);
}

async function openFillerWords() {
  let data;
  try { data = await api("/api/setup/filler-words"); } catch (err) { showError(err); return; }
  const area = el("textarea", { style: "min-height:220px" });
  area.value = data.words.join("\n");
  openModal("필러워드 사전", el("div", {}, [
    note("info", "한 줄에 하나씩", "여기 있는 말이 1차 컷 후보로 잡힙니다. 자동 판정이라 검수는 그대로 필요합니다."),
    el("div", { class: "field" }, [area]),
    el("div", { class: "btn-row" }, [
      el("button", {
        class: "btn primary", onclick: async () => {
          try {
            const res = await api("/api/setup/filler-words", {
              method: "POST", body: { words: area.value.split("\n").map((w) => w.trim()).filter(Boolean) },
            });
            toast("ok", "저장했습니다", `${res.words.length}개`);
            closeModal();
          } catch (err) { showError(err); }
        },
      }, "저장"),
      el("button", { class: "btn", onclick: () => openSettings() }, "뒤로"),
    ]),
  ]));
}

async function openGlossary() {
  let data;
  try { data = await api("/api/setup/glossary"); } catch (err) { showError(err); return; }
  const body = el("div", {}, [
    note("info", "영어 표현을 원문 그대로 유지합니다", "체크된 용어는 자막에서 사전 표기대로 맞춥니다."),
  ]);
  const boxes = {};
  for (const [group, entries] of Object.entries(data.glossary)) {
    body.append(el("h3", { text: group }));
    const wrap = el("div", { style: "display:flex;flex-wrap:wrap;gap:var(--sp-3)" });
    boxes[group] = [];
    for (const entry of entries) {
      const cb = el("input", { type: "checkbox", checked: entry.enabled !== false });
      boxes[group].push({ term: entry.term, cb });
      wrap.append(el("label", { class: "checkline" }, [cb, entry.term]));
    }
    body.append(wrap);
  }
  body.append(el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn primary", onclick: async () => {
        const payload = {};
        for (const [group, items] of Object.entries(boxes)) {
          payload[group] = items.map((i) => ({ term: i.term, enabled: i.cb.checked }));
        }
        try {
          await api("/api/setup/glossary", { method: "POST", body: { glossary: payload } });
          toast("ok", "저장했습니다", "");
          closeModal();
        } catch (err) { showError(err); }
      },
    }, "저장"),
    el("button", { class: "btn", onclick: () => openSettings() }, "뒤로"),
  ]));
  openModal("영어 용어 사전", body);
}

async function pollLogs() {
  try {
    const res = await api(`/api/setup/logs?after=${S.logSeq}`);
    const host = $("#logLines");
    for (const line of res.logs || []) {
      S.logSeq = Math.max(S.logSeq, line.seq);
      host.append(el("div", { class: `logline ${line.level}`, text: `${line.time} ${line.message}` }));
    }
    while (host.childElementCount > 400) host.firstChild.remove();
    if (res.logs && res.logs.length) host.scrollTop = host.scrollHeight;
  } catch (_) { /* 로그 폴링 실패는 조용히 넘어갑니다 */ }
  setTimeout(pollLogs, 2500);
}

boot().catch(showError);
