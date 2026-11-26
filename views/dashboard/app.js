const bridge = window.AstrBotPluginView;
const el = (id) => document.getElementById(id);
const NAMES = { local: "本地", webdav: "WebDAV" };
const drafts = {};
const systemDarkQuery = window.matchMedia("(prefers-color-scheme: dark)");

let model = null;
let activeTab = "history";
let mode = "local";
let filterMode = "all";
let filterStatus = "all";
let archiveSource = "local";
let archiveLoading = false;
let confirmDeleteKey = null;
const archivesCache = { local: null, webdav: null };
let busy = false;
let loading = false;
let lastSyncAt = null;
let toastTimer = null;
let cronPreviewTimer = null;
let pollTimer = null;
let sseSubId = null;
let sseConnected = false;
let sseRetryTimer = null;
let sseRetryAttempt = 0;

function applyHostTheme(ctx) {
  const root = document.documentElement;
  const dark =
    typeof ctx?.isDark === "boolean"
      ? ctx.isDark
      : root.dataset.theme === "dark" ||
        (root.dataset.theme !== "light" && systemDarkQuery.matches);
  root.setAttribute("data-theme", dark ? "dark" : "light");
  root.style.setProperty("color-scheme", dark ? "dark" : "light");

  const colors = ctx?.themeColors;
  if (colors && typeof colors === "object") {
    const tokenMap = {
      primary: colors.primary,
      secondary: colors.secondary,
      "on-primary": colors["on-primary"],
      text: colors.primaryText || colors["on-surface"],
      "text-secondary": colors.secondaryText,
      background: colors.background,
      surface: colors.surface,
      border: colors.border,
      error: colors.error,
    };
    for (const [name, value] of Object.entries(tokenMap)) {
      if (typeof value === "string" && value.trim()) {
        root.style.setProperty(`--astrbot-color-${name}`, value);
      } else {
        root.style.removeProperty(`--astrbot-color-${name}`);
      }
    }
  }
}

function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (cls) element.className = cls;
  return element;
}

function formatTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

function formatBytes(size) {
  if (!Number.isFinite(size) || size < 0) return "未知大小";
  if (size === 0) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1048576) return `${(size / 1024).toFixed(1)} KiB`;
  if (size < 1073741824) return `${(size / 1048576).toFixed(1)} MiB`;
  return `${(size / 1073741824).toFixed(2)} GiB`;
}

function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${ms} ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m} 分 ${s} 秒`;
}

function formatRelative(iso, future = false) {
  if (!iso) return "—";
  const diffSec = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  const abs = Math.abs(future ? diffSec : -diffSec);
  if (abs < 45) return future ? "即将执行" : "刚刚";
  if (abs < 3600) {
    const mins = Math.round(abs / 60);
    return future ? `约 ${mins} 分钟后` : `${mins} 分钟前`;
  }
  if (abs < 86400) {
    const hrs = Math.round(abs / 3600);
    return future ? `约 ${hrs} 小时后` : `${hrs} 小时前`;
  }
  const days = Math.round(abs / 86400);
  return future ? `约 ${days} 天后` : `${days} 天前`;
}

function showToast(message, state = "info") {
  const toast = el("toast");
  clearTimeout(toastTimer);
  toast.dataset.state = state;
  toast.textContent = message;
  if (message && state !== "pending") {
    toastTimer = setTimeout(
      () => {
        toast.textContent = "";
      },
      state === "error" ? 7000 : 4000,
    );
  }
}

function hasAnyActivePhase() {
  return Boolean(
    model && Object.values(model.phases || {}).some((p) => p !== "空闲"),
  );
}

function summarizeHost(urlText, remoteDir) {
  if (!urlText) return "未配置服务地址";
  try {
    const parsed = new URL(urlText);
    const base = `${parsed.host}${parsed.pathname.replace(/\/$/, "")}`;
    const dir = (remoteDir || "/").trim();
    if (!dir || dir === "/") return base || parsed.host;
    return `${base}/${dir.replace(/^\/+|\/+$/g, "")}`;
  } catch {
    return urlText;
  }
}

function renderTopHealth() {
  const badge = el("health-badge");
  const phases = Object.values(model.phases || {});
  const running = phases.some((p) => p !== "空闲");
  const planErrs = Object.keys(model.plan_errors || {}).length > 0;
  const latestLocal = model.history.find((r) => r.mode === "local");
  const latestRemote = model.history.find((r) => r.mode === "webdav");
  const anyFail =
    (latestLocal && !latestLocal.ok) || (latestRemote && !latestRemote.ok);
  const enabledCount = ["local", "webdav"].filter(
    (m) => model.settings[`${m}_enabled`],
  ).length;

  badge.className = "pill";
  if (running) {
    badge.classList.add("pill-info", "pill-pulse");
    badge.textContent = "备份运行中";
  } else if (planErrs || anyFail) {
    badge.classList.add("pill-err");
    badge.textContent = planErrs ? "计划配置异常" : "最近备份异常";
  } else if (enabledCount === 0) {
    badge.classList.add("pill-warn");
    badge.textContent = "定时计划未启用";
  } else {
    badge.classList.add("pill-ok");
    badge.textContent = "运行正常";
  }

  if (lastSyncAt) {
    const syncTime = lastSyncAt.toLocaleTimeString("zh-CN", { hour12: false });
    el("last-sync").textContent = sseConnected
      ? `实时连接 · ${syncTime}`
      : `更新于 ${syncTime}`;
  }
}

function renderKpis() {
  const history = model.history || [];
  const lastOk = history.find((r) => r.ok);
  const localLast = history.find((r) => r.mode === "local");
  const webdavLast = history.find((r) => r.mode === "webdav");

  // KPI 1: Latest successful backup
  const kpiLast = node("article", undefined, "kpi-card");
  kpiLast.append(node("span", "最近成功备份", "kpi-label"));
  const lastVal = node(
    "div",
    lastOk ? formatRelative(lastOk.time, false) : "尚未成功",
    `kpi-value ${lastOk ? "tone-ok" : "tone-warn"}`,
  );
  const statusOf = (rec) => (!rec ? "未运行" : rec.ok ? "正常" : "失败");
  kpiLast.append(
    lastVal,
    node(
      "span",
      `本地：${statusOf(localLast)} · WebDAV：${statusOf(webdavLast)}`,
      "kpi-sub",
    ),
  );

  // KPI 2: Next scheduled execution
  const enabledModes = ["local", "webdav"].filter(
    (m) => model.settings[`${m}_enabled`],
  );
  const upcoming = Object.entries(model.next || {})
    .map(([k, iso]) => ({ mode: k, iso, ts: new Date(iso).getTime() }))
    .sort((a, b) => a.ts - b.ts);
  const soonest = upcoming[0];
  const kpiNext = node("article", undefined, "kpi-card");
  kpiNext.append(node("span", "下次计划执行", "kpi-label"));
  const nextVal = node(
    "div",
    soonest ? formatRelative(soonest.iso, true) : "未启用",
    `kpi-value ${soonest ? "" : "tone-warn"}`,
  );
  kpiNext.append(
    nextVal,
    node(
      "span",
      soonest
        ? `${NAMES[soonest.mode]} · ${formatTime(soonest.iso)}（已启用 ${enabledModes.length}/2）`
        : `已启用 ${enabledModes.length}/2 项定时计划`,
      "kpi-sub",
    ),
  );

  // KPI 3: Success rate & mini dot strip
  const total = history.length;
  const okCount = history.filter((r) => r.ok).length;
  const rate = total
    ? ((okCount / total) * 100).toFixed(okCount === total || okCount === 0 ? 0 : 1)
    : null;
  const kpiRate = node("article", undefined, "kpi-card");
  kpiRate.append(node("span", `近期成功率（近 ${total} 次）`, "kpi-label"));
  const rateTone =
    rate === null ? "" : okCount === total ? "tone-ok" : okCount === 0 ? "tone-err" : "tone-warn";
  kpiRate.append(
    node("div", rate === null ? "—" : `${rate}%`, `kpi-value ${rateTone}`),
  );
  if (total > 0) {
    const dots = node("div", undefined, "spark-dots");
    const recent = history.slice(0, 15).reverse();
    for (const item of recent) {
      const dot = node("span", undefined, `spark-dot ${item.ok ? "ok" : "fail"}`);
      dot.title = `${NAMES[item.mode]} · ${item.ok ? "成功" : "失败"} · ${formatTime(item.time)}`;
      dots.append(dot);
    }
    kpiRate.append(dots);
  } else {
    kpiRate.append(node("span", "完成首次备份后展示运行趋势", "kpi-sub"));
  }

  // KPI 4: Local storage usage (click opens Archives tab)
  const storage = model.local_storage || {
    total_count: 0,
    total_size: 0,
    plugin_count: 0,
    plugin_size: 0,
  };
  el("archives-count").textContent = String(storage.total_count);
  const kpiStore = node("article", undefined, "kpi-card kpi-card-link");
  kpiStore.title = "点击查看并管理备份文件";
  kpiStore.onclick = () => {
    archiveSource = "local";
    switchTab("archives");
  };
  kpiStore.append(
    node("span", "本地备份占用", "kpi-label"),
    node("div", formatBytes(storage.total_size), "kpi-value"),
    node(
      "span",
      `data/backups 共 ${storage.total_count} 份 ZIP（本插件 ${storage.plugin_count} 份）`,
      "kpi-sub",
    ),
  );

  el("kpi-grid").replaceChildren(kpiLast, kpiNext, kpiRate, kpiStore);
}

function formatProgressDetail(progress) {
  if (!progress) return "";
  if (progress.stage === "upload" && Number.isFinite(progress.total)) {
    const parts = [
      `${formatBytes(progress.current || 0)} / ${formatBytes(progress.total)}`,
    ];
    if (Number.isFinite(progress.rate_bps) && progress.rate_bps > 0) {
      parts.push(`${formatBytes(progress.rate_bps)}/s`);
    }
    return parts.join(" · ");
  }
  if (
    progress.stage === "directories" &&
    Number.isFinite(progress.bytes) &&
    progress.bytes > 0
  ) {
    return `${progress.detail || "正在打包目录…"} · 已写入 ${formatBytes(progress.bytes)}`;
  }
  return progress.detail || "";
}

function fillProgressBox(box, phase, startedIso, progress) {
  const pct =
    progress?.determinate && Number.isFinite(progress.percent)
      ? Math.min(100, Math.max(0, Number(progress.percent)))
      : null;
  const label = pct !== null ? `${phase} · ${pct.toFixed(pct === 100 || pct === 0 ? 0 : 1)}%` : `阶段：${phase}`;
  const headLabel = box.querySelector(".progress-label");
  if (headLabel) headLabel.textContent = label;

  const elapsedEl = box.querySelector(".elapsed-timer");
  if (elapsedEl) {
    elapsedEl.dataset.started = startedIso || "";
    elapsedEl.textContent = startedIso
      ? `已运行 ${Math.max(0, Math.floor((Date.now() - new Date(startedIso).getTime()) / 1000))}s`
      : "";
  }

  const track = box.querySelector(".progress-track");
  if (track) {
    if (pct !== null) {
      let fill = track.querySelector(".progress-fill");
      if (!fill) {
        fill = node("div", undefined, "progress-fill");
        track.replaceChildren(fill);
      }
      fill.style.width = `${pct}%`;
    } else if (!track.querySelector(".progress-bar")) {
      track.replaceChildren(node("div", undefined, "progress-bar"));
    }
  }

  const detailEl = box.querySelector(".progress-detail");
  if (detailEl) {
    const detailText = formatProgressDetail(progress);
    detailEl.textContent = detailText;
    detailEl.hidden = !detailText;
  }
}

function updateCardProgressInPlace(targetMode) {
  if (!model) return false;
  const box = document.querySelector(`[data-progress-mode="${targetMode}"]`);
  const phase = model.phases?.[targetMode] || "空闲";
  if (!box || phase === "空闲") return false;
  const pill = document.querySelector(`[data-card-pill="${targetMode}"]`);
  if (pill) pill.textContent = phase;
  const headline = document.querySelector(`[data-card-headline="${targetMode}"]`);
  if (headline) headline.textContent = `${phase}…`;
  fillProgressBox(
    box,
    phase,
    model.phase_started?.[targetMode],
    model.progress?.[targetMode],
  );
  return true;
}

function renderCards() {
  const cards = [];
  for (const key of ["local", "webdav"]) {
    const label = NAMES[key];
    const phase = model.phases[key] || "空闲";
    const isRunning = phase !== "空闲";
    const last = model.history.find((x) => x.mode === key);
    const planErr = model.plan_errors[key];
    const enabled = Boolean(model.settings[`${key}_enabled`]);
    const keepCount = Number(model.settings[`${key}_keep_count`] || 0);
    const includeTemp = Boolean(model.settings[`${key}_items`]);
    const unconfigured = key === "webdav" && !model.settings.url;

    const card = node("article", undefined, "card");

    // Header: pill shows current operational state (not duplicating outcome text)
    const head = node("div", undefined, "card-head");
    const titleWrap = node("div", undefined, "card-title");
    titleWrap.append(node("h2", `${label}备份`));
    const pillClass = isRunning
      ? "pill pill-info pill-pulse"
      : planErr
        ? "pill pill-err"
        : unconfigured
          ? "pill pill-warn"
          : last && !last.ok
            ? "pill pill-err"
            : last && last.ok
              ? "pill pill-ok"
              : "pill pill-neutral";
    const pillText = isRunning
      ? phase
      : planErr
        ? "计划无效"
        : unconfigured
          ? "未配置"
          : last && !last.ok
            ? "最近异常"
            : "空闲";
    const pillEl = node("span", pillText, pillClass);
    pillEl.dataset.cardPill = key;
    head.append(titleWrap, pillEl);
    card.append(head);

    // Primary status summary (consistent semantic slot across all states)
    const metricRow = node("div", undefined, "card-metric-row");
    let headlineText = "尚未运行";
    let headlineCls = "metric is-muted";
    let subText = unconfigured
      ? "请先配置 WebDAV 服务地址与账号"
      : "点击下方「立即备份」或开启定时计划";
    let subCls = "metric-sub";

    if (isRunning) {
      headlineText = `${phase}…`;
      headlineCls = "metric";
      subText = "备份任务执行中，下方实时更新进度";
    } else if (last) {
      if (last.ok) {
        const rel = formatRelative(last.time, false);
        headlineText = rel === "刚刚" ? "刚刚已备份" : `${rel}已备份`;
        headlineCls = "metric";
        const parts = [`单次归档 ${formatBytes(last.size)}`];
        if (Number.isFinite(last.duration_ms)) {
          parts.push(`耗时 ${formatDuration(last.duration_ms)}`);
        }
        parts.push(formatTime(last.time));
        subText = parts.join(" · ");
      } else {
        headlineText = "最近备份失败";
        headlineCls = "metric is-err";
        subText = `${formatRelative(last.time, false)} · ${last.error || "请查看运行历史"}`;
        subCls = "metric-sub is-err";
      }
    }

    const headlineEl = node("div", headlineText, headlineCls);
    headlineEl.dataset.cardHeadline = key;
    metricRow.append(headlineEl, node("span", subText, subCls));
    card.append(metricRow);

    // Live progress indicator when active
    if (isRunning) {
      const box = node("div", undefined, "progress-box");
      box.dataset.progressMode = key;
      const pHead = node("div", undefined, "progress-head");
      pHead.append(
        node("span", "", "progress-label"),
        node("span", "", "elapsed-timer"),
      );
      const track = node("div", undefined, "progress-track");
      const detail = node("div", "", "progress-detail");
      box.append(pHead, track, detail);
      fillProgressBox(
        box,
        phase,
        model.phase_started?.[key],
        model.progress?.[key],
      );
      card.append(box);
    }

    // Config Summary Grid
    const meta = node("div", undefined, "meta-grid");
    const addMeta = (k, v, isErr = false) => {
      const item = node("div", undefined, "meta-item");
      item.append(
        node("span", k, "meta-key"),
        node("span", v, `meta-val${isErr ? " is-err" : ""}`),
      );
      item.lastChild.title = v;
      meta.append(item);
    };

    if (planErr) {
      addMeta("定时计划", planErr, true);
    } else if (enabled && model.next[key]) {
      addMeta(
        "定时计划",
        `${model.settings[`${key}_cron`]} · ${formatRelative(model.next[key], true)}`,
      );
    } else {
      addMeta("定时计划", "已关闭（仅手动）");
    }

    addMeta(
      "保留策略",
      keepCount > 0 ? `自动保留最新 ${keepCount} 份` : "不自动清理",
    );

    addMeta("备份范围", includeTemp ? "常规项目 + temp" : "常规项目（不含 temp）");

    if (key === "local") {
      addMeta("保存位置", "data/backups/");
    } else {
      const targetText = summarizeHost(
        model.settings.url,
        model.settings.remote_dir,
      );
      const pwdTag = model.password_set ? "已填密码" : "未填密码";
      addMeta(
        "远端目标",
        model.settings.url ? `${targetText} · ${pwdTag}` : "未配置 WebDAV",
        !model.settings.url,
      );
    }
    card.append(meta);

    // Card Actions
    const actions = node("div", undefined, "card-actions");
    const runBtn = node("button", "立即备份", "btn btn-tonal");
    runBtn.type = "button";
    runBtn.disabled = isRunning;
    runBtn.onclick = async () => {
      runBtn.disabled = true;
      try {
        await bridge.apiPost("run", { mode: key });
        showToast(`${label}备份任务已提交`, "info");
        await refresh();
        scheduleNextPoll();
      } catch (err) {
        showToast(err.message || "提交备份失败", "error");
        runBtn.disabled = false;
      }
    };
    actions.append(runBtn);

    if (key === "webdav") {
      const testBtn = node("button", "测试连接", "btn btn-sm");
      testBtn.type = "button";
      testBtn.onclick = async () => {
        if (!model.settings.url) {
          showToast("请先填写 WebDAV 服务地址", "error");
          openSettingsFor("webdav");
          return;
        }
        testBtn.disabled = true;
        testBtn.textContent = "测试中…";
        showToast("正在测试已保存的 WebDAV 连接…", "pending");
        try {
          await bridge.apiPost("test-connection", {
            url: model.settings.url,
            remote_dir: model.settings.remote_dir,
            username: model.settings.username,
            allow_http: Boolean(model.settings.allow_http),
          });
          showToast("WebDAV 连接测试通过（写入、校验并清理成功）", "success");
        } catch (err) {
          showToast(`连接测试失败：${err.message || "请检查配置"}`, "error");
        } finally {
          testBtn.disabled = false;
          testBtn.textContent = "测试连接";
        }
      };
      actions.append(testBtn);
    }

    const filesBtn = node("button", "文件", "btn btn-sm spacer");
    filesBtn.type = "button";
    filesBtn.onclick = () => {
      archiveSource = key;
      switchTab("archives");
    };

    const cfgBtn = node("button", "配置", "btn btn-sm");
    cfgBtn.type = "button";
    cfgBtn.onclick = () => openSettingsFor(key);
    actions.append(filesBtn, cfgBtn);

    card.append(actions);
    cards.push(card);
  }
  el("cards").replaceChildren(...cards);
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    showToast("已复制到剪贴板", "success");
  } catch {
    showToast("复制失败，请手动选中复制", "error");
  }
}

function renderHistory() {
  const all = model.history || [];
  el("history-count").textContent = String(all.length);

  const filtered = all.filter((r) => {
    if (filterMode !== "all" && r.mode !== filterMode) return false;
    if (filterStatus === "ok" && !r.ok) return false;
    if (filterStatus === "fail" && r.ok) return false;
    return true;
  });

  if (!filtered.length) {
    const msg = all.length
      ? "当前筛选条件下没有匹配的运行记录。"
      : "完成首次备份后，这里会显示时间、耗时、文件大小和运行结果。";
    el("history").replaceChildren(node("p", msg, "empty"));
    return;
  }

  const rows = filtered.map((r) => {
    const row = node("div", undefined, "run-row");
    const main = node("div", undefined, "run-main");

    // Head row with badges
    const head = node("div", undefined, "run-head");
    const statusText = !r.ok
      ? "失败"
      : r.mode === "webdav"
        ? r.remote
          ? "成功 · 已校验"
          : "成功 · 旧版未校验"
        : "成功";
    head.append(
      node("span", statusText, `pill ${r.ok ? "pill-ok" : "pill-err"}`),
      node("span", NAMES[r.mode] || r.mode, "tag tag-accent"),
    );
    if (r.trigger) {
      head.append(
        node("span", r.trigger === "schedule" ? "定时触发" : "手动触发", "tag"),
      );
    }
    if (r.include_temp) {
      head.append(node("span", "含 temp", "tag"));
    }
    if (r.pruned > 0) {
      head.append(node("span", `已清理 ${r.pruned} 份旧备份`, "tag"));
    }
    if (r.prune_error) {
      head.append(node("span", "清理警告", "tag tag-warn"));
    }
    main.append(head);

    // Time & duration
    const subParts = [formatTime(r.time)];
    if (Number.isFinite(r.duration_ms)) {
      subParts.push(`耗时 ${formatDuration(r.duration_ms)}`);
    }
    main.append(node("div", subParts.join(" · "), "run-sub"));

    // Path / Remote URL with Copy
    const targetText = r.remote || r.name;
    if (targetText) {
      const pathRow = node("div", undefined, "run-path-row");
      pathRow.append(node("code", targetText, "run-path"));
      const copyBtn = node("button", "复制", "copy-btn");
      copyBtn.type = "button";
      copyBtn.onclick = () => copyText(targetText);
      pathRow.append(copyBtn);
      main.append(pathRow);
    }

    if (!r.ok && r.error) {
      main.append(node("div", `失败原因：${r.error}`, "callout callout-err"));
    }
    if (r.prune_error) {
      main.append(
        node(
          "div",
          `备份已成功，但自动清理旧备份失败：${r.prune_error}`,
          "callout callout-warn",
        ),
      );
    }

    row.append(
      main,
      node("div", r.ok ? formatBytes(r.size) : "—", "run-size"),
    );
    return row;
  });

  el("history").replaceChildren(...rows);
}

// Phase 2: Archives Tab
function syncArchiveSourceButtons() {
  for (const btn of document.querySelectorAll("[data-archive-source]")) {
    btn.setAttribute(
      "aria-pressed",
      String(btn.dataset.archiveSource === archiveSource),
    );
  }
  el("archives-tip").textContent =
    archiveSource === "local"
      ? "本地 ZIP 保存在 data/backups/，可直接下载保存，或在 AstrBot 原生备份列表中导入恢复。"
      : "点击「拉取到本地」会将远端 ZIP 校验后保存到 data/backups/，随后可在 AstrBot 原生备份列表中一键恢复。";
}

function renderArchives() {
  syncArchiveSourceButtons();
  const container = el("archives-list");
  const summary = el("archives-summary");

  if (archiveLoading) {
    summary.textContent = "加载中…";
    container.replaceChildren(
      node(
        "p",
        archiveSource === "local"
          ? "正在扫描本地备份目录…"
          : "正在连接 WebDAV 读取远端目录…",
        "empty",
      ),
    );
    return;
  }

  const state = archivesCache[archiveSource];
  if (!state) {
    summary.textContent = "";
    container.replaceChildren(node("p", "暂无数据，点击刷新列表。", "empty"));
    return;
  }

  if (state.error) {
    summary.textContent = "读取失败";
    const box = node("div", undefined, "empty-state");
    box.append(node("div", state.error, "callout callout-err"));
    if (archiveSource === "webdav" && !model?.settings?.url) {
      const goBtn = node("button", "前往配置 WebDAV", "btn btn-primary btn-sm");
      goBtn.type = "button";
      goBtn.onclick = () => openSettingsFor("webdav");
      box.append(goBtn);
    }
    container.replaceChildren(box);
    return;
  }

  const items = state.items || [];
  summary.textContent = `共 ${items.length} 份 · ${formatBytes(state.total_size || 0)}`;

  if (!items.length) {
    container.replaceChildren(
      node(
        "p",
        archiveSource === "local"
          ? "data/backups/ 中暂无 ZIP 备份文件。"
          : "WebDAV 远端目录中暂无 ZIP 备份文件。",
        "empty",
      ),
    );
    return;
  }

  const rows = items.map((item) => {
    const row = node("div", undefined, "archive-row");
    const main = node("div", undefined, "archive-main");

    const head = node("div", undefined, "archive-head");
    head.append(node("code", item.name, "run-path"));
    head.append(
      node(
        "span",
        item.owned ? "本插件" : archiveSource === "local" ? "其他/原生" : "其他",
        item.owned ? "tag tag-accent" : "tag",
      ),
    );
    if (archiveSource === "webdav" && item.exists_local) {
      head.append(node("span", "本地已有", "tag tag-ok"));
    }
    main.append(head);

    const subParts = [formatBytes(item.size)];
    if (item.modified) {
      subParts.push(
        `${formatTime(item.modified)}（${formatRelative(item.modified, false)}）`,
      );
    }
    main.append(node("div", subParts.join(" · "), "archive-sub"));

    const actions = node("div", undefined, "archive-actions");
    const deleteKey = `${archiveSource}:${item.name}`;

    if (archiveSource === "local") {
      const dlBtn = node("button", "下载", "btn btn-tonal btn-sm");
      dlBtn.type = "button";
      dlBtn.onclick = async () => {
        dlBtn.disabled = true;
        dlBtn.textContent = "下载中…";
        try {
          await bridge.download(
            "archives/local/download",
            { name: item.name },
            item.name,
          );
          showToast(`已开始下载 ${item.name}`, "success");
        } catch (err) {
          showToast(`下载失败：${err.message || "请重试"}`, "error");
        } finally {
          dlBtn.disabled = false;
          dlBtn.textContent = "下载";
        }
      };
      actions.append(dlBtn);
    } else {
      const pullBtn = node(
        "button",
        item.exists_local ? "已在本地" : "拉取到本地",
        "btn btn-tonal btn-sm",
      );
      pullBtn.type = "button";
      pullBtn.disabled = Boolean(item.exists_local);
      pullBtn.onclick = async () => {
        pullBtn.disabled = true;
        pullBtn.textContent = "拉取中…";
        showToast(`正在从 WebDAV 拉取 ${item.name}…`, "pending");
        try {
          const res = await bridge.apiPost("archives/webdav/pull", {
            name: item.name,
          });
          showToast(
            `已拉取到本地（${formatBytes(res?.size)}），可在原生备份列表中恢复。`,
            "success",
          );
          archivesCache.local = null;
          await Promise.all([
            refresh().catch(() => {}),
            loadArchives("webdav", { silent: true }),
          ]);
        } catch (err) {
          showToast(`拉取失败：${err.message || "请稍后重试"}`, "error");
          pullBtn.disabled = false;
          pullBtn.textContent = "拉取到本地";
        }
      };
      actions.append(pullBtn);
    }

    // Inline two-step delete confirmation (sandbox iframe has no allow-modals)
    if (confirmDeleteKey === deleteKey) {
      const confirmBtn = node("button", "确认删除", "btn btn-danger-solid btn-sm");
      confirmBtn.type = "button";
      confirmBtn.onclick = async () => {
        confirmBtn.disabled = true;
        confirmBtn.textContent = "删除中…";
        const endpoint =
          archiveSource === "local"
            ? "archives/local/delete"
            : "archives/webdav/delete";
        try {
          await bridge.apiPost(endpoint, { name: item.name });
          confirmDeleteKey = null;
          showToast(`已删除 ${item.name}`, "success");
          if (archiveSource === "local") {
            archivesCache.webdav = null;
          }
          await Promise.all([
            refresh().catch(() => {}),
            loadArchives(archiveSource, { silent: true }),
          ]);
        } catch (err) {
          showToast(`删除失败：${err.message || "请稍后重试"}`, "error");
          confirmDeleteKey = null;
          renderArchives();
        }
      };
      const cancelBtn = node("button", "取消", "btn btn-sm");
      cancelBtn.type = "button";
      cancelBtn.onclick = () => {
        confirmDeleteKey = null;
        renderArchives();
      };
      actions.append(confirmBtn, cancelBtn);
    } else {
      const delBtn = node("button", "删除", "btn btn-danger btn-sm");
      delBtn.type = "button";
      delBtn.onclick = () => {
        confirmDeleteKey = deleteKey;
        renderArchives();
      };
      actions.append(delBtn);
    }

    row.append(main, actions);
    return row;
  });

  container.replaceChildren(...rows);
}

async function loadArchives(source = archiveSource, { silent = false } = {}) {
  confirmDeleteKey = null;
  archiveLoading = true;
  el("refresh-archives-btn").disabled = true;
  renderArchives();
  try {
    const endpoint =
      source === "local" ? "archives/local" : "archives/webdav";
    const data = await bridge.apiGet(endpoint);
    archivesCache[source] = {
      items: data?.items || [],
      total_size: data?.total_size || 0,
      collection: data?.collection || "",
      error: null,
    };
    if (source === "local") {
      el("archives-count").textContent = String(archivesCache.local.items.length);
    }
  } catch (err) {
    archivesCache[source] = {
      items: [],
      total_size: 0,
      error: err.message || "读取备份列表失败",
    };
    if (!silent) {
      showToast(archivesCache[source].error, "error");
    }
  } finally {
    archiveLoading = false;
    el("refresh-archives-btn").disabled = false;
    if (source === archiveSource) {
      renderArchives();
    }
  }
}

// Settings helpers
const UMO_RE = /^[^\s:]+:(?:GroupMessage|FriendMessage):[^\s:]+$/;

function getNotifyTargetsWorkingCopy() {
  const src = drafts.notify?.notify_targets ?? model?.settings?.notify_targets;
  return Array.isArray(src) ? [...src] : [];
}

function populateNotifyPlatforms() {
  const select = el("notify-platform");
  const customInput = el("notify-platform-custom");
  const prev = select.value;
  const platforms = model?.platforms || [];
  const options = [];
  for (const p of platforms) {
    const label = p.unsupported
      ? `${p.id}（${p.name} · 不支持主动推送）`
      : p.name && p.name !== p.id
        ? `${p.id}（${p.name}）`
        : p.id;
    const opt = node("option", label);
    opt.value = p.id;
    options.push(opt);
  }
  const customOpt = node("option", "自定义平台 ID…");
  customOpt.value = "__custom__";
  options.push(customOpt);
  select.replaceChildren(...options);

  if (prev && options.some((o) => o.value === prev)) {
    select.value = prev;
  } else if (platforms.length > 0) {
    const firstSupported = platforms.find((p) => !p.unsupported) || platforms[0];
    select.value = firstSupported.id;
  } else {
    select.value = "__custom__";
  }
  customInput.hidden = select.value !== "__custom__";
}

function renderNotifyTargetsList() {
  const container = el("notify-targets-list");
  const list = getNotifyTargetsWorkingCopy();
  if (!list.length) {
    container.replaceChildren(
      node(
        "div",
        "暂无通知目标。可在上方选择平台与群号/用户 ID 添加，或在群聊/私聊发送 /backup_notify add 绑定。",
        "notify-empty",
      ),
    );
    return;
  }
  const rows = list.map((umo) => {
    const [platformId, chatType, sessionId] = umo.split(":");
    const row = node("div", undefined, "notify-item");
    const main = node("div", undefined, "notify-item-main");
    main.append(
      node("span", platformId, "tag tag-accent"),
      node("span", chatType === "GroupMessage" ? "群聊" : "私聊", "tag"),
      node("strong", sessionId),
      node("code", umo, "run-path"),
    );
    const removeBtn = node("button", "移除", "btn btn-danger btn-sm");
    removeBtn.type = "button";
    removeBtn.onclick = () => {
      const nextTargets = getNotifyTargetsWorkingCopy().filter((x) => x !== umo);
      drafts.notify = {
        notify_mode: el("notify-mode").value,
        notify_targets: nextTargets,
      };
      renderNotifyTargetsList();
      updateDirtyIndicators();
    };
    row.append(main, removeBtn);
    return row;
  });
  container.replaceChildren(...rows);
}

function updateDirtyIndicators() {
  const anyDirty = Boolean(drafts.local || drafts.webdav || drafts.notify);
  el("settings-dirty-dot").hidden = !anyDirty;
  for (const m of ["local", "webdav", "notify"]) {
    const dot = document.querySelector(`[data-dirty="${m}"]`);
    if (dot) dot.hidden = !drafts[m];
  }
  el("discard-draft").hidden = !drafts[mode];
  el("save-feedback").textContent = drafts[mode] ? "当前页面有未保存的更改" : "";
}

function collect() {
  if (mode === "notify") {
    return {
      notify_mode: el("notify-mode").value,
      notify_targets: getNotifyTargetsWorkingCopy(),
    };
  }
  const values = {};
  for (const key of ["enabled", "cron", "timezone"]) {
    values[`${mode}_${key}`] =
      key === "enabled" ? el(key).checked : el(key).value.trim();
  }
  values[`${mode}_items`] = el("include-temp").checked;
  const rawKeep = el("keep-count").value.trim();
  values[`${mode}_keep_count`] = /^\d+$/.test(rawKeep) ? Number(rawKeep) : -1;
  if (mode === "webdav") {
    for (const key of ["url", "remote_dir", "username"]) {
      values[key] = el(key).value.trim();
    }
    values.allow_http = el("allow_http").checked;
    if (el("password").value) values.password = el("password").value;
  }
  return values;
}

async function fetchCronPreview() {
  const box = el("cron-preview");
  const cron = el("cron").value.trim();
  const tz = el("timezone").value.trim();
  if (!cron || !tz) {
    box.className = "preview-box is-err";
    box.textContent = "请填写 Cron 表达式和时区以预览执行时间。";
    return;
  }
  try {
    const res = await bridge.apiPost("cron-preview", {
      cron,
      timezone: tz,
      count: 3,
    });
    const runs = res?.next_runs || [];
    box.className = "preview-box";
    box.replaceChildren(node("div", "未来 3 次预计执行时间："));
    const list = node("div", undefined, "preview-list");
    for (const iso of runs) {
      list.append(node("span", formatTime(iso)));
    }
    box.append(list);
  } catch (err) {
    box.className = "preview-box is-err";
    box.textContent = err.message || "Cron 表达式或时区无效";
  }
}

function scheduleCronPreview() {
  clearTimeout(cronPreviewTimer);
  cronPreviewTimer = setTimeout(fetchCronPreview, 250);
}

function syncPresetChips() {
  const curCron = el("cron").value.trim();
  for (const btn of document.querySelectorAll("[data-cron]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.cron === curCron));
  }
  const curKeep = el("keep-count").value.trim();
  for (const btn of document.querySelectorAll("[data-keep]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.keep === curKeep));
  }
}

function drawForm() {
  if (!model) return;
  const isNotify = mode === "notify";
  const conf = { ...model.settings, ...drafts[mode] };
  for (const b of document.querySelectorAll("[data-mode]")) {
    b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  }

  el("plan-section").hidden = isNotify;
  el("plan-section").disabled = isNotify;
  el("retention-section").hidden = isNotify;
  el("retention-section").disabled = isNotify;
  el("scope-section").hidden = isNotify;
  el("scope-section").disabled = isNotify;
  el("remote").hidden = mode !== "webdav";
  el("remote").disabled = mode !== "webdav";
  el("notify-mode-section").hidden = !isNotify;
  el("notify-mode-section").disabled = !isNotify;
  el("notify-targets-section").hidden = !isNotify;
  el("notify-targets-section").disabled = !isNotify;
  el("test-connection").hidden = mode !== "webdav";
  el("test-notify").hidden = !isNotify;

  if (isNotify) {
    el("settings-tip").textContent =
      "配置备份结束后的主动消息通知，支持多平台群聊与私聊。";
    el("save").textContent = "保存通知设置";
    el("notify-mode").value = conf.notify_mode || "off";
    populateNotifyPlatforms();
    renderNotifyTargetsList();
    updateDirtyIndicators();
    return;
  }

  el("settings-tip").textContent =
    "两个目标独立配置定时计划、保留份数与备份范围。";
  el("plan-title").textContent = `${NAMES[mode]}定时计划`;
  el("save").textContent = `保存${NAMES[mode]}设置`;
  el("enabled").checked = Boolean(conf[`${mode}_enabled`]);
  el("cron").value = conf[`${mode}_cron`] || "";
  el("timezone").value = conf[`${mode}_timezone`] || "Asia/Shanghai";
  el("keep-count").value = String(conf[`${mode}_keep_count`] ?? 0);
  el("keep-help").textContent =
    mode === "local"
      ? "每次本地备份完成后，仅保留由本插件生成的最新 N 份 ZIP，不影响手动上传或原生备份。填 0 表示不自动清理。"
      : "每次 WebDAV 备份校验成功后，自动清理远程目录中超出 N 份的本插件历史 ZIP（需服务器支持 PROPFIND 与 DELETE）。填 0 表示不自动清理。";

  for (const key of ["url", "remote_dir", "username", "password"]) {
    el(key).value = conf[key] || "";
  }
  el("password").placeholder = model.password_set
    ? "已保存密码，留空则保持不变"
    : "直接输入 WebDAV 密码";
  el("allow_http").checked = Boolean(conf.allow_http);
  el("include-temp").checked = Boolean(conf[`${mode}_items`]);

  syncPresetChips();
  updateDirtyIndicators();
  fetchCronPreview();
}

function switchTab(nextTab) {
  activeTab = nextTab;
  confirmDeleteKey = null;
  for (const btn of document.querySelectorAll("[data-tab]")) {
    const selected = btn.dataset.tab === nextTab;
    btn.setAttribute("aria-selected", String(selected));
  }
  el("tab-history").hidden = nextTab !== "history";
  el("tab-archives").hidden = nextTab !== "archives";
  el("tab-settings").hidden = nextTab !== "settings";
  if (nextTab === "settings") {
    drawForm();
  } else if (nextTab === "archives") {
    syncArchiveSourceButtons();
    if (!archivesCache[archiveSource]) {
      loadArchives(archiveSource, { silent: true });
    } else {
      renderArchives();
    }
  }
}

function openSettingsFor(targetMode) {
  mode = targetMode;
  switchTab("settings");
  el("tab-settings").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderAll() {
  if (!model) return;
  renderTopHealth();
  renderKpis();
  renderCards();
  renderHistory();
  updateDirtyIndicators();
}

function scheduleNextPoll() {
  clearTimeout(pollTimer);
  // When SSE is connected, only run a low-frequency 60s safety refresh.
  const delay = sseConnected ? 60000 : hasAnyActivePhase() ? 2500 : 10000;
  pollTimer = setTimeout(async () => {
    if (!document.hidden && !busy) {
      await refresh().catch((err) => showToast(err.message, "error"));
    }
    scheduleNextPoll();
  }, delay);
}

function handleSSEEvent(event) {
  if (!model || !event || typeof event.parsed !== "object" || !event.parsed) {
    return;
  }
  const data = event.parsed;
  if (event.eventType === "snapshot") {
    model.phases = { ...model.phases, ...(data.phases || {}) };
    model.phase_started = {
      ...(model.phase_started || {}),
      ...(data.phase_started || {}),
    };
    model.progress = { ...(model.progress || {}), ...(data.progress || {}) };
    renderTopHealth();
    renderCards();
    return;
  }
  if (event.eventType === "progress" && data.mode) {
    const prevPhase = model.phases?.[data.mode] || "空闲";
    model.phases[data.mode] = data.phase || "空闲";
    model.phase_started = model.phase_started || {};
    model.phase_started[data.mode] = data.phase_started ?? null;
    model.progress = model.progress || {};
    model.progress[data.mode] = data.progress ?? null;
    renderTopHealth();
    if (prevPhase !== "空闲" && model.phases[data.mode] !== "空闲") {
      if (!updateCardProgressInPlace(data.mode)) {
        renderCards();
      }
    } else {
      renderCards();
    }
    return;
  }
  if (event.eventType === "completed" && data.record) {
    const rec = data.record;
    const label = NAMES[rec.mode] || rec.mode;
    archivesCache.local = null;
    archivesCache.webdav = null;
    if (rec.ok) {
      showToast(`${label}备份已完成（${formatBytes(rec.size)}）`, "success");
    } else {
      showToast(`${label}备份失败：${rec.error || "请查看运行历史"}`, "error");
    }
    refresh().catch(() => {});
  }
}

function scheduleSSEReconnect() {
  clearTimeout(sseRetryTimer);
  const delays = [3000, 6000, 15000, 30000];
  const wait = delays[Math.min(sseRetryAttempt, delays.length - 1)];
  sseRetryAttempt += 1;
  sseRetryTimer = setTimeout(() => {
    if (!document.hidden) {
      connectSSE();
    } else {
      scheduleSSEReconnect();
    }
  }, wait);
}

async function connectSSE() {
  if (!bridge?.subscribeSSE) return;
  if (sseSubId) {
    const oldId = sseSubId;
    sseSubId = null;
    bridge.unsubscribeSSE(oldId).catch(() => {});
  }
  try {
    sseSubId = await bridge.subscribeSSE("events", {
      onOpen() {
        sseConnected = true;
        sseRetryAttempt = 0;
        if (model) renderTopHealth();
        scheduleNextPoll();
      },
      onMessage(event) {
        handleSSEEvent(event);
      },
      onClose() {
        sseConnected = false;
        sseSubId = null;
        if (model) renderTopHealth();
        scheduleNextPoll();
        scheduleSSEReconnect();
      },
      onError() {
        sseConnected = false;
        sseSubId = null;
        if (model) renderTopHealth();
        scheduleNextPoll();
        scheduleSSEReconnect();
      },
    });
  } catch {
    sseConnected = false;
    sseSubId = null;
    scheduleNextPoll();
    scheduleSSEReconnect();
  }
}

async function refresh() {
  if (loading) return;
  loading = true;
  const hadModel = Boolean(model);
  const prevHistoryTop = model?.history?.[0]?.time;
  try {
    model = await bridge.apiGet("dashboard");
    lastSyncAt = new Date();
    renderAll();
    // Invalidate cached archive lists when a new backup completes
    if (hadModel && model.history?.[0]?.time !== prevHistoryTop) {
      archivesCache.local = null;
      archivesCache.webdav = null;
      if (activeTab === "archives") {
        loadArchives(archiveSource, { silent: true });
      }
    }
  } finally {
    loading = false;
  }
}

// Event bindings
el("refresh-btn").onclick = async () => {
  el("refresh-btn").disabled = true;
  try {
    await refresh();
    if (activeTab === "archives") {
      await loadArchives(archiveSource, { silent: true });
    }
    showToast("状态已刷新", "info");
  } catch (err) {
    showToast(err.message || "刷新失败", "error");
  } finally {
    el("refresh-btn").disabled = false;
  }
};

el("open-settings-btn").onclick = () => openSettingsFor(mode);

for (const btn of document.querySelectorAll("[data-tab]")) {
  btn.onclick = () => switchTab(btn.dataset.tab);
}

for (const btn of document.querySelectorAll("[data-archive-source]")) {
  btn.onclick = () => {
    const nextSource = btn.dataset.archiveSource;
    if (nextSource === archiveSource && archivesCache[nextSource]) return;
    archiveSource = nextSource;
    confirmDeleteKey = null;
    syncArchiveSourceButtons();
    if (!archivesCache[archiveSource]) {
      loadArchives(archiveSource, { silent: true });
    } else {
      renderArchives();
    }
  };
}

el("refresh-archives-btn").onclick = () => loadArchives(archiveSource);

for (const btn of document.querySelectorAll("[data-filter-mode]")) {
  btn.onclick = () => {
    filterMode = btn.dataset.filterMode;
    for (const b of document.querySelectorAll("[data-filter-mode]")) {
      b.setAttribute("aria-pressed", String(b.dataset.filterMode === filterMode));
    }
    if (model) renderHistory();
  };
}

for (const btn of document.querySelectorAll("[data-filter-status]")) {
  btn.onclick = () => {
    filterStatus = btn.dataset.filterStatus;
    for (const b of document.querySelectorAll("[data-filter-status]")) {
      b.setAttribute(
        "aria-pressed",
        String(b.dataset.filterStatus === filterStatus),
      );
    }
    if (model) renderHistory();
  };
}

for (const btn of document.querySelectorAll("[data-mode]")) {
  btn.onclick = () => {
    if (!busy) {
      mode = btn.dataset.mode;
      drawForm();
    }
  };
}

for (const btn of document.querySelectorAll("[data-cron]")) {
  btn.onclick = () => {
    el("cron").value = btn.dataset.cron;
    drafts[mode] = collect();
    syncPresetChips();
    updateDirtyIndicators();
    fetchCronPreview();
  };
}

for (const btn of document.querySelectorAll("[data-keep]")) {
  btn.onclick = () => {
    el("keep-count").value = btn.dataset.keep;
    drafts[mode] = collect();
    syncPresetChips();
    updateDirtyIndicators();
  };
}

el("form").oninput = (event) => {
  if (
    event.target === el("notify-platform") ||
    event.target === el("notify-platform-custom") ||
    event.target === el("notify-chat-type") ||
    event.target === el("notify-session-id")
  ) {
    return;
  }
  drafts[mode] = collect();
  if (mode !== "notify") {
    syncPresetChips();
  }
  updateDirtyIndicators();
  if (event.target === el("cron") || event.target === el("timezone")) {
    scheduleCronPreview();
  }
};

el("notify-platform").onchange = () => {
  el("notify-platform-custom").hidden =
    el("notify-platform").value !== "__custom__";
  if (!el("notify-platform-custom").hidden) {
    el("notify-platform-custom").focus();
  }
};

function addNotifyTargetFromInputs() {
  const rawSession = el("notify-session-id").value.trim();
  if (!rawSession) {
    showToast("请填写群号、用户 ID 或完整 UMO。", "error");
    el("notify-session-id").focus();
    return;
  }
  let umo = rawSession;
  if (!UMO_RE.test(rawSession)) {
    const selectedPlatform = el("notify-platform").value;
    const platformId =
      selectedPlatform === "__custom__"
        ? el("notify-platform-custom").value.trim()
        : selectedPlatform;
    if (!platformId || /[\s:]/.test(platformId)) {
      showToast("请填写有效的平台实例 ID（不能包含空格或冒号）。", "error");
      (selectedPlatform === "__custom__"
        ? el("notify-platform-custom")
        : el("notify-platform")
      ).focus();
      return;
    }
    if (/[\s:]/.test(rawSession)) {
      showToast("群号或用户 ID 不能包含空格或冒号。", "error");
      el("notify-session-id").focus();
      return;
    }
    const chatType = el("notify-chat-type").value;
    umo = `${platformId}:${chatType}:${rawSession}`;
  }

  const current = getNotifyTargetsWorkingCopy();
  if (current.includes(umo)) {
    showToast("该通知目标已在列表中。", "info");
    return;
  }
  if (current.length >= 50) {
    showToast("通知目标数量不能超过 50 个。", "error");
    return;
  }
  drafts.notify = {
    notify_mode: el("notify-mode").value,
    notify_targets: [...current, umo],
  };
  el("notify-session-id").value = "";
  renderNotifyTargetsList();
  updateDirtyIndicators();
  showToast(`已添加 ${umo}（记得点击保存通知设置）`, "info");
}

el("add-notify-target").onclick = addNotifyTargetFromInputs;
el("notify-session-id").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addNotifyTargetFromInputs();
  }
});

el("test-notify").onclick = async () => {
  if (busy) return;
  const targets = getNotifyTargetsWorkingCopy();
  if (!targets.length) {
    showToast("请先添加至少一个通知目标会话。", "error");
    el("notify-session-id").focus();
    return;
  }
  busy = true;
  el("test-notify").disabled = true;
  el("test-notify").textContent = "发送中…";
  showToast(`正在向 ${targets.length} 个目标发送测试通知…`, "pending");
  try {
    const res = await bridge.apiPost("notify-test", { targets });
    if (res?.failed?.length) {
      showToast(
        `已成功发送 ${res.sent} 个，失败 ${res.failed.length} 个：${res.failed.join("；")}`,
        "error",
      );
    } else {
      showToast(`测试通知已发送到 ${res?.sent ?? targets.length} 个目标会话。`, "success");
    }
  } catch (err) {
    showToast(err.message || "测试通知发送失败", "error");
  } finally {
    busy = false;
    el("test-notify").disabled = false;
    el("test-notify").textContent = "发送测试通知";
  }
};

el("discard-draft").onclick = () => {
  delete drafts[mode];
  drawForm();
  showToast("已恢复为上次保存的设置", "info");
};

el("check-temp-size").onclick = async () => {
  const btn = el("check-temp-size");
  const result = el("temp-size-result");
  btn.disabled = true;
  btn.textContent = "计算中…";
  try {
    const info = await bridge.apiGet("temp-size");
    result.hidden = false;
    if (!info?.exists) {
      result.textContent = "目录不存在（0 B）";
    } else if (!info.files) {
      result.textContent = "当前为空（0 B）";
    } else {
      result.textContent = `当前 ${formatBytes(info.size)}（${info.files} 个文件）`;
    }
    btn.textContent = "重新计算";
  } catch (err) {
    showToast(`查询 temp 大小失败：${err.message || "请稍后重试"}`, "error");
    btn.textContent = result.hidden ? "查看当前大小" : "重新计算";
  } finally {
    btn.disabled = false;
  }
};

el("form").addEventListener(
  "invalid",
  (event) => {
    if (event.target === el("form").querySelector("input:invalid")) {
      const label = event.target.labels?.[0]?.textContent?.trim() || "输入项";
      showToast(`请检查${label}：${event.target.validationMessage}`, "error");
    }
  },
  true,
);

el("test-connection").onclick = el("form").onsubmit = async (event) => {
  event.preventDefault();
  if (busy) return;
  const testing = event.currentTarget === el("test-connection");
  const values = collect();
  if (testing && (!values.url || !el("url").validity.valid)) {
    showToast("请填写有效的 WebDAV 服务地址。", "error");
    el("url").focus();
    return;
  }
  if (
    !testing &&
    mode !== "notify" &&
    (values[`${mode}_keep_count`] < 0 || values[`${mode}_keep_count`] > 1000)
  ) {
    showToast("保留份数需填写 0 到 1000 之间的整数。", "error");
    el("keep-count").focus();
    return;
  }

  const saveLabel = mode === "notify" ? "通知" : NAMES[mode];
  busy = true;
  el("save").disabled = true;
  el("test-connection").disabled = true;
  el("test-notify").disabled = true;
  el("discard-draft").disabled = true;
  if (testing) {
    el("test-connection").textContent = "测试中…";
    showToast("正在测试 WebDAV 写入和校验…", "pending");
  } else {
    el("save").textContent = "正在保存…";
    showToast(`正在保存${saveLabel}设置…`, "pending");
  }
  for (const control of document.querySelectorAll(
    "#form fieldset, [data-mode]",
  )) {
    control.disabled = true;
  }
  try {
    if (testing) {
      const { url, remote_dir, username, password, allow_http } = values;
      await bridge.apiPost("test-connection", {
        url,
        remote_dir,
        username,
        password,
        allow_http,
      });
      showToast("连接成功，测试文件已写入、校验并删除。", "success");
      return;
    }
    await bridge.apiPost("settings", values);
    Object.assign(
      model.settings,
      Object.fromEntries(
        Object.entries(values).filter(([k]) => k !== "password"),
      ),
    );
    if (values.password) model.password_set = true;
    delete drafts[mode];
    if (mode === "webdav") {
      archivesCache.webdav = null;
    }
    drawForm();
    showToast(`${saveLabel}设置已保存`, "success");
  } catch (err) {
    showToast(
      `${testing ? "连接测试" : "保存"}失败：${err.message || "请稍后重试。"}`,
      "error",
    );
    return;
  } finally {
    busy = false;
    for (const control of document.querySelectorAll(
      "#form fieldset, [data-mode]",
    )) {
      control.disabled = false;
    }
    el("plan-section").disabled = mode === "notify";
    el("retention-section").disabled = mode === "notify";
    el("scope-section").disabled = mode === "notify";
    el("remote").disabled = mode !== "webdav";
    el("notify-mode-section").disabled = mode !== "notify";
    el("notify-targets-section").disabled = mode !== "notify";
    el("test-connection").disabled = false;
    el("test-connection").textContent = "测试连接";
    el("test-notify").disabled = false;
    el("discard-draft").disabled = false;
    el("save").disabled = false;
    el("save").textContent = `保存${saveLabel}设置`;
  }
  refresh().catch((err) => showToast(`仪表盘刷新失败：${err.message}`, "error"));
};

// Tick live elapsed timers once per second without network traffic
const tickTimer = setInterval(() => {
  if (document.hidden) return;
  for (const nodeEl of document.querySelectorAll(".elapsed-timer")) {
    const iso = nodeEl.dataset.started;
    if (iso) {
      const sec = Math.max(
        0,
        Math.floor((Date.now() - new Date(iso).getTime()) / 1000),
      );
      nodeEl.textContent = `已运行 ${sec}s`;
    }
  }
}, 1000);

// Boot
applyHostTheme(null);
const onSystemThemeChange = () => {
  if (typeof bridge?.getContext?.()?.isDark !== "boolean") {
    applyHostTheme(null);
  }
};
systemDarkQuery.addEventListener("change", onSystemThemeChange);

try {
  if (!bridge) throw new Error("请从 AstrBot WebUI 的插件页面打开仪表盘。");
  const initialContext = await bridge.ready();
  applyHostTheme(initialContext);
  const offContext = bridge.onContext(applyHostTheme);
  await refresh();
  scheduleNextPoll();
  connectSSE();
  window.addEventListener(
    "pagehide",
    () => {
      offContext();
      systemDarkQuery.removeEventListener("change", onSystemThemeChange);
      clearTimeout(pollTimer);
      clearTimeout(sseRetryTimer);
      clearInterval(tickTimer);
      if (sseSubId) {
        bridge.unsubscribeSSE(sseSubId).catch(() => {});
        sseSubId = null;
      }
    },
    { once: true },
  );
} catch (err) {
  showToast(err.message || "仪表盘初始化失败", "error");
}
