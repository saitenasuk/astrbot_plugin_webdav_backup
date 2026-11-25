const bridge = window.AstrBotPluginView;
const el = (id) => document.getElementById(id);
const names = { local: "本地", webdav: "WebDAV" };
const drafts = {};
let model,
  mode = "local",
  showAll = false,
  busy = false,
  loading = false;
const clock = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium",
  timeStyle: "short",
  hour12: false,
});
const time = (iso) => clock.format(new Date(iso));
const bytes = (size) =>
  size < 1024
    ? `${size} B`
    : size < 1048576
      ? `${(size / 1024).toFixed(1)} KiB`
      : `${(size / 1048576).toFixed(1)} MiB`;
const submitting = new Set();
let rechecking = null;
const checkText = (check) =>
  `上次复查 ${time(check.time)} · ${check.ok ? "路径与字节数一致" : `未通过：${check.error}`}`;
const seconds = (value) =>
  value < 0.1 ? "< 0.1 秒" : `${value.toFixed(1)} 秒`;
el("clock-zone").textContent = `记录时区：${clock.resolvedOptions().timeZone}`;
function notice(message, error = false) {
  el("notice").textContent = message;
  el("notice").classList.toggle("error", error);
}
function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
function openSettings(key) {
  if (busy) return;
  mode = key;
  drawForm();
  el("settings").open = true;
  el("settings").scrollIntoView({ block: "start" });
  el(key === "webdav" ? "url" : "enabled").focus({ preventScroll: true });
}
function drawOverview() {
  const { history, settings, phases, plan_errors: errors } = model;
  const verified = history.find((r) => r.mode === "webdav" && r.ok && r.remote);
  const latestRemote = history.find((r) => r.mode === "webdav");
  const latestLocal = history.find((r) => r.mode === "local");
  // Inspect the last run of each target, retaining the backend's newest-first order.
  const failure = history.find(
    (r) => !r.ok && (r === latestRemote || r === latestLocal),
  );
  const invalid = Object.keys(errors)[0];
  const active =
    Object.keys(names).find(
      (key) => !["空闲", "排队中"].includes(phases[key]),
    ) || Object.keys(names).find((key) => phases[key] === "排队中");
  const action = el("overview-action");
  const needsSettings = failure?.mode || invalid || (!settings.url && "webdav");
  action.hidden = !needsSettings;
  if (needsSettings) {
    action.textContent =
      failure?.mode === "webdav"
        ? "检查连接设置"
        : failure
          ? "检查本地设置"
          : invalid
            ? "检查备份计划"
            : `配置 ${names[needsSettings]}`;
    action.onclick = () => openSettings(needsSettings);
  }
  let title, description;
  if (failure) {
    title = `${names[failure.mode]} 最近一次${failure.failed_phase || "备份"}失败`;
    description = `${time(failure.time)} · ${failure.error || "未记录失败原因"}`;
  } else if (invalid) {
    title = `${names[invalid]} 计划未生效`;
    description = errors[invalid];
  } else if (active) {
    title = `${names[active]} · ${phases[active]}`;
    description = "任务运行期间，阶段会自动更新";
  } else if (!settings.url) {
    title = "尚未配置 WebDAV";
    description =
      "填写服务地址与目录，即可把备份保存到远端；本地备份可独立使用。";
  } else if (verified?.recheck?.ok === false) {
    title = "最近已校验的 ZIP 复查未通过";
    description = checkText(verified.recheck);
  } else if (latestRemote?.ok && latestRemote.remote) {
    title = "WebDAV 备份已校验";
    description = "上传时已核对远端路径和字节数";
  } else {
    title = latestRemote?.ok ? "旧版备份无校验信息" : "等待 WebDAV 备份";
    description = "手动运行一次，或等待下次定时任务";
  }
  el("overview").dataset.state =
    failure || invalid || verified?.recheck?.ok === false
      ? "error"
      : verified
        ? "verified"
        : "neutral";
  el("overview-title").textContent = title;
  el("overview-description").textContent = description;
  el("receipt-time").textContent = verified
    ? time(verified.time)
    : "暂无校验回执";
  el("receipt-dir").textContent = verified
    ? verified.remote.slice(0, verified.remote.lastIndexOf("/") + 1)
    : "";
  el("receipt-note").textContent = verified
    ? `${bytes(verified.size)} · 当时路径与大小一致`
    : "最近 30 条记录中无已校验的 WebDAV 备份";
  const check = verified?.recheck;
  el("receipt-recheck").hidden = !check;
  el("receipt-recheck").textContent = check ? checkText(check) : "";
  el("receipt-recheck").classList.toggle("error", check?.ok === false);
}
function drawDestinations() {
  const { history, settings, phases, plan_errors: errors } = model;
  for (const [key, label] of Object.entries(names)) {
    let row = el(`target-${key}`);
    if (!row) {
      row = el("destination").content.firstElementChild.cloneNode(true);
      row.id = `target-${key}`;
      row.querySelector("h3").textContent = label;
      const edit = row.querySelector(".edit");
      edit.onclick = () => openSettings(key);
      const run = row.querySelector(".run-now");
      run.setAttribute("aria-label", `立即备份到${label}`);
      run.onclick = async () => {
        submitting.add(key);
        run.disabled = true;
        try {
          await bridge.apiPost("run", { mode: key });
          notice(`${label}任务已提交，使用已保存的设置。`);
          await refresh();
        } catch (err) {
          notice(err.message, true);
        } finally {
          submitting.delete(key);
          run.disabled = model.phases[key] !== "空闲";
        }
      };
      el("destinations").append(row);
    }
    const success = history.find((r) => r.mode === key && r.ok);
    const configured = key === "local" || Boolean(settings.url);
    row.querySelector(".phase").textContent =
      `当前${phases[key] === "空闲" ? "无任务" : `：${phases[key]}`}`;
    row.querySelector(".last").textContent = success
      ? `${time(success.time)}${key === "webdav" && !success.remote ? " · 旧版未校验" : ""}`
      : "近期无成功记录";
    const schedule = row.querySelector(".next");
    // APScheduler returns the next trigger's wall time with its configured offset.
    schedule.textContent =
      errors[key] ||
      (model.next[key]
        ? `${model.next[key].slice(0, 16).replace("T", " ")} · ${settings[`${key}_timezone`]}`
        : settings[`${key}_enabled`]
          ? "暂无执行时间"
          : "定时未启用");
    schedule.classList.toggle("error", Boolean(errors[key]));
    row.querySelector(".run-now").hidden = !configured;
    row.querySelector(".run-now").disabled =
      submitting.has(key) || phases[key] !== "空闲";
    row
      .querySelector(".edit")
      .setAttribute(
        "aria-label",
        configured ? `设置${label}备份` : "配置 WebDAV",
      );
    row.querySelector(".edit").textContent = configured
      ? "设置"
      : "配置 WebDAV";
  }
}
function drawHistory() {
  const history = el("history");
  const filtered = model.history.filter(
    (r) =>
      (el("history-filter").value === "all" ||
        r.mode === el("history-filter").value) &&
      (!el("failures-only").checked || !r.ok),
  );
  const visible = showAll ? filtered : filtered.slice(0, 5);
  el("history-more").hidden = filtered.length <= 5;
  el("history-more").textContent = showAll
    ? "收起记录"
    : `查看全部 ${filtered.length} 条`;
  el("history-more").setAttribute("aria-expanded", String(showAll));
  // Reuse completed records so polling never closes an expanded receipt or steals focus.
  const existing = new Map(
    Array.from(history.children, (row) => [row.dataset.key, row]),
  );
  for (const [i, r] of visible.entries()) {
    const key = `${r.mode}:${r.time}`;
    let row = existing.get(key);
    if (!row) {
      row = node("details", undefined, "run");
      row.dataset.key = key;
      const summary = node("summary");
      const result = !r.ok
        ? "失败"
        : r.mode === "local"
          ? "成功"
          : r.remote
            ? "成功 · 已校验"
            : "成功 · 旧版未校验";
      summary.append(
        node("span", time(r.time), "stamp"),
        node("span", names[r.mode]),
        node("span", result, !r.ok ? "error" : "result"),
        node("span", r.ok ? bytes(r.size) : "—", "run-size"),
      );
      const detail = node("div", undefined, "run-details");
      if (!r.ok)
        detail.append(node("p", `失败原因：${r.error || "未记录"}`, "error"));
      const path =
        r.remote ||
        (r.ok && r.mode === "local" && r.name ? `data/backups/${r.name}` : "");
      if (path) {
        detail.append(
          node("p", r.mode === "webdav" ? "远端目标" : "本地文件", "caption"),
        );
        const text = node("code", path);
        const copy = node("button", "复制路径", "quiet");
        copy.type = "button";
        copy.onclick = async () => {
          try {
            await navigator.clipboard.writeText(path);
            showSaveToast("路径已复制", "success");
          } catch {
            // Clipboard access can be denied inside the host's sandboxed iframe.
            const range = document.createRange();
            range.selectNodeContents(text);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            let copied = false;
            try {
              copied = document.execCommand("copy");
            } catch {
              /* The selection remains available for manual copying. */
            }
            showSaveToast(
              copied ? "路径已复制" : "路径已选中，请使用系统复制操作",
              copied ? "success" : "info",
            );
          }
        };
        detail.append(text, copy);
      } else if (r.name) detail.append(node("code", r.name));
      if (r.ok && r.mode === "webdav")
        detail.append(
          node(
            "p",
            r.remote
              ? "校验回执：上传时远端路径和字节数与本地归档一致"
              : "旧版记录未保存校验信息",
            "secondary",
          ),
        );
      if (Number.isFinite(r.duration)) {
        const stages = Object.entries(r.stages || {}).map(
          ([stage, value]) => `${stage} ${seconds(value)}`,
        );
        detail.append(
          node(
            "p",
            [`总耗时 ${seconds(r.duration)}`, ...stages].join(" · "),
            "secondary",
          ),
        );
      }
      if (!r.ok && r.failed_phase)
        detail.append(node("p", `失败阶段：${r.failed_phase}`));
      if (
        r.mode === "webdav" &&
        r.ok &&
        r.remote &&
        r.name &&
        Number.isSafeInteger(r.size)
      ) {
        const button = node("button", "复查远端文件", "recheck");
        button.type = "button";
        button.onclick = async () => {
          if (rechecking !== null) return;
          rechecking = key;
          drawHistory();
          try {
            const check = await bridge.apiPost("verify-remote", {
              time: r.time,
            });
            const current = model.history.find(
              (record) => record.time === r.time && record.mode === r.mode,
            );
            if (current) current.recheck = check;
            drawOverview();
            showSaveToast(
              check.ok
                ? "复查通过：远端路径和字节数一致"
                : `远端复查未通过：${check.error}`,
              check.ok ? "success" : "error",
            );
          } catch (err) {
            showSaveToast(`无法完成复查：${err.message}`, "error");
          } finally {
            rechecking = null;
            drawHistory();
          }
        };
        detail.append(button, node("p", undefined, "recheck-result"));
      }
      row.append(summary, detail);
    }
    const recheck = row.querySelector(".recheck");
    if (recheck) {
      recheck.disabled = rechecking !== null;
      recheck.textContent = rechecking === key ? "正在复查…" : "复查远端文件";
      const result = row.querySelector(".recheck-result");
      result.textContent = r.recheck ? checkText(r.recheck) : "";
      result.classList.toggle("error", r.recheck?.ok === false);
    }
    if (history.children[i] !== row)
      history.insertBefore(row, history.children[i] || null);
    existing.delete(key);
  }
  for (const row of existing.values()) row.remove();
  if (!visible.length)
    history.append(
      node(
        "p",
        model.history.length
          ? "没有符合筛选条件的记录"
          : "暂无运行记录。完成首次备份后，可在这里查看结果。",
        "empty",
      ),
    );
}
function collect() {
  const values = {};
  for (const key of ["enabled", "cron", "timezone"])
    values[`${mode}_${key}`] =
      key === "enabled" ? el(key).checked : el(key).value.trim();
  values[`${mode}_items`] = el("include-temp").checked;
  if (mode === "webdav") {
    for (const key of ["url", "remote_dir", "username"])
      values[key] = el(key).value.trim();
    values.allow_http = el("allow_http").checked;
    if (el("password").value) values.password = el("password").value;
  }
  return values;
}
let saveToastTimer;
function showSaveToast(message, state) {
  const toast = el("save-toast");
  clearTimeout(saveToastTimer);
  toast.dataset.state = state;
  toast.textContent = message;
  if (state !== "pending") {
    saveToastTimer = setTimeout(
      () => {
        toast.textContent = "";
      },
      state === "error" ? 8000 : 5000,
    );
  }
}
function drawForm() {
  const conf = { ...model.settings, ...drafts[mode] };
  for (const b of document.querySelectorAll("[data-mode]"))
    b.setAttribute("aria-pressed", b.dataset.mode === mode);
  el("plan-title").textContent = `${names[mode]}计划`;
  el("save").textContent = `保存${names[mode]}设置`;
  el("enabled").checked = conf[`${mode}_enabled`];
  for (const key of ["cron", "timezone"])
    el(key).value = conf[`${mode}_${key}`];
  el("test-connection").hidden = mode !== "webdav";
  el("remote").hidden = mode !== "webdav";
  el("remote").disabled = mode !== "webdav";
  for (const key of ["url", "remote_dir", "username", "password"])
    el(key).value = conf[key] || "";
  el("password").placeholder = model.password_set
    ? "已设置，留空保留"
    : "输入应用密码";
  el("allow_http").checked = conf.allow_http;
  el("include-temp").checked = conf[`${mode}_items`];
  el("save-feedback").textContent = drafts[mode] ? "有未保存的更改" : "";
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const next = await bridge.apiGet("dashboard");
    const changed =
      !model || JSON.stringify(model.history) !== JSON.stringify(next.history);
    model = next;
    drawOverview();
    drawDestinations();
    if (changed) drawHistory();
  } finally {
    loading = false;
  }
}
el("form").oninput = () => {
  drafts[mode] = collect();
  el("save-feedback").textContent = "有未保存的更改";
};
el("form").addEventListener(
  "invalid",
  (event) => {
    if (event.target === el("form").querySelector("input:invalid")) {
      const label = event.target.labels[0].textContent.trim();
      showSaveToast(
        `请检查${label}。${event.target.validationMessage}`,
        "error",
      );
    }
  },
  true,
);
for (const b of document.querySelectorAll("[data-mode]"))
  b.onclick = () => {
    if (!busy) {
      mode = b.dataset.mode;
      drawForm();
    }
  };
el("history-filter").onchange = el("failures-only").onchange = () => {
  showAll = false;
  drawHistory();
};
el("history-more").onclick = () => {
  showAll = !showAll;
  drawHistory();
};
el("test-connection").onclick = el("form").onsubmit = async (event) => {
  event.preventDefault();
  if (busy) return;
  const testing = event.currentTarget === el("test-connection");
  const values = collect();
  if (testing && (!values.url || !el("url").validity.valid)) {
    showSaveToast("请填写有效的 WebDAV 服务地址。", "error");
    el("url").focus();
    return;
  }
  busy = true;
  el("save").disabled = true;
  el("test-connection").disabled = true;
  if (testing) {
    el("test-connection").textContent = "测试中…";
    showSaveToast("正在测试 WebDAV 写入和校验…", "pending");
  } else {
    el("save").textContent = "正在保存…";
    showSaveToast(`正在保存${names[mode]}设置…`, "pending");
  }
  for (const control of document.querySelectorAll(
    "#form fieldset, [data-mode]",
  ))
    control.disabled = true;
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
      showSaveToast("连接成功，测试文件已写入、校验并删除。", "success");
      return;
    }
    await bridge.apiPost("settings", values);
    Object.assign(
      model.settings,
      Object.fromEntries(
        Object.entries(values).filter(([key]) => key !== "password"),
      ),
    );
    if (values.password) model.password_set = true;
    delete drafts[mode];
    drawForm();
    showSaveToast(`${names[mode]}设置已保存`, "success");
  } catch (err) {
    showSaveToast(
      `${testing ? "连接测试" : "保存"}失败。${err.message || "请稍后重试。"}`,
      "error",
    );
    return;
  } finally {
    busy = false;
    for (const control of document.querySelectorAll(
      "#form fieldset, [data-mode]",
    ))
      control.disabled = false;
    el("remote").disabled = mode !== "webdav";
    el("test-connection").disabled = false;
    el("test-connection").textContent = "测试连接";
    el("save").disabled = false;
    el("save").textContent = `保存${names[mode]}设置`;
  }
  refresh().catch((err) => notice(`仪表盘刷新失败。${err.message}`, true));
};
try {
  if (!bridge) throw new Error("请从 AstrBot WebUI 的插件页面打开仪表盘。");
  await bridge.ready();
  await refresh();
  drawForm();
  el("dashboard").hidden = false;
  notice("");
  const timer = setInterval(() => {
    if (!document.hidden && !busy && rechecking === null)
      refresh().catch((err) => notice(err.message, true));
  }, 5000);
  window.addEventListener("pagehide", () => clearInterval(timer), {
    once: true,
  });
} catch (err) {
  notice(err.message, true);
}
