const bridge = window.AstrBotPluginView;
const el = (id) => document.getElementById(id);
const names = { local: "本地", webdav: "WebDAV" };
const drafts = {};
let model,
  mode = "local",
  busy = false,
  loading = false;
const time = (iso) => new Date(iso).toLocaleString("zh-CN", { hour12: false });
const bytes = (size) => `${(size / 1048576).toFixed(1)} MiB`;
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
function drawStatus() {
  const cards = [];
  for (const [key, label] of Object.entries(names)) {
    const card = node("article", undefined, "card");
    const heading = node("div", undefined, "cardhead");
    heading.append(
      node("h3", `${label}备份`),
      node("span", model.phases[key], "state"),
    );
    const last = model.history.find((x) => x.mode === key);
    const result = last
      ? last.ok
        ? bytes(last.size)
        : "备份失败"
      : "尚未运行";
    const next =
      model.plan_errors[key] ||
      (model.next[key]
        ? `下次运行 ${time(model.next[key])}`
        : "定时备份已关闭");
    const run = node("button", `立即备份到${label}`);
    run.disabled = model.phases[key] !== "空闲";
    run.onclick = async () => {
      run.disabled = true;
      try {
        await bridge.apiPost("run", { mode: key });
        notice(`${label}任务已提交，使用已保存的设置。`);
        await refresh();
      } catch (err) {
        notice(err.message, true);
        run.disabled = false;
      }
    };
    card.append(
      heading,
      node("p", result, "metric"),
      node(
        "p",
        last
          ? `最近运行 ${time(last.time)}`
          : key === "local"
            ? "保存到 data/backups"
            : "上传到已配置的远程目录",
        "secondary",
      ),
      node("p", next, "secondary"),
      run,
    );
    cards.push(card);
  }
  el("cards").replaceChildren(...cards);
  const rows = model.history.map((r) => {
    const row = node("div", undefined, "run"),
      info = node("div");
    info.append(
      node("b", `${names[r.mode]} · ${r.ok ? "成功" : "失败"}`),
      node("small", `${time(r.time)} · ${r.ok ? r.name : r.error}`),
    );
    row.append(info, node("strong", r.ok ? bytes(r.size) : ""));
    return row;
  });
  el("history").replaceChildren(
    ...(rows.length
      ? rows
      : [
          node(
            "p",
            "完成首次备份后，这里会显示时间、文件大小和运行结果。",
            "empty",
          ),
        ]),
  );
}
function collect() {
  const values = {};
  for (const key of ["enabled", "cron", "timezone"])
    values[`${mode}_${key}`] =
      key === "enabled" ? el(key).checked : el(key).value.trim();
  values[`${mode}_items`] = [...el("items").querySelectorAll(":checked")].map(
    (x) => x.value,
  );
  if (mode === "webdav") {
    for (const key of ["url", "username"]) values[key] = el(key).value.trim();
    values.allow_http = el("allow_http").checked;
    if (el("password").value) values.password = el("password").value;
  }
  return values;
}
function drawForm() {
  const conf = { ...model.settings, ...drafts[mode] };
  el("settings").hidden = false;
  for (const b of document.querySelectorAll("[data-mode]"))
    b.setAttribute("aria-pressed", b.dataset.mode === mode);
  el("plan-title").textContent = `${names[mode]}计划`;
  el("save").textContent = `保存${names[mode]}设置`;
  el("enabled").checked = conf[`${mode}_enabled`];
  for (const key of ["cron", "timezone"])
    el(key).value = conf[`${mode}_${key}`];
  el("remote").hidden = mode !== "webdav";
  el("remote").disabled = mode !== "webdav";
  for (const key of ["url", "username", "password"])
    el(key).value = conf[key] || "";
  el("password").placeholder = model.password_set
    ? "已设置，留空保留"
    : "输入应用密码";
  el("allow_http").checked = conf.allow_http;
  el("items").replaceChildren(
    ...model.items.map((item) => {
      const label = node("label", undefined, "item"),
        input = node("input"),
        text = node("span");
      input.type = "checkbox";
      input.value = item.key;
      input.checked = conf[`${mode}_items`].includes(item.key);
      text.append(node("strong", item.name), node("small", item.hint));
      label.append(input, text);
      return label;
    }),
  );
  el("dirty").textContent = drafts[mode] ? "有未保存的更改" : "";
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    model = await bridge.apiGet("dashboard");
    drawStatus();
  } finally {
    loading = false;
  }
}
el("form").oninput = () => {
  drafts[mode] = collect();
  el("dirty").textContent = "有未保存的更改";
};
for (const b of document.querySelectorAll("[data-mode]"))
  b.onclick = () => {
    if (!busy) {
      mode = b.dataset.mode;
      drawForm();
    }
  };
el("form").onsubmit = async (event) => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  el("save").disabled = true;
  const values = collect();
  try {
    await bridge.apiPost("settings", values);
    Object.assign(
      model.settings,
      Object.fromEntries(
        Object.entries(values).filter(([k]) => k !== "password"),
      ),
    );
    if (values.password) model.password_set = true;
    delete drafts[mode];
    drawForm();
    notice(`${names[mode]}设置已保存。`);
    await refresh();
  } catch (err) {
    notice(err.message, true);
  } finally {
    busy = false;
    el("save").disabled = false;
  }
};
try {
  if (!bridge) throw new Error("请从 AstrBot WebUI 的插件页面打开仪表盘。");
  await bridge.ready();
  await refresh();
  drawForm();
  notice("");
  const timer = setInterval(() => {
    if (!document.hidden && !busy)
      refresh().catch((err) => notice(err.message, true));
  }, 5000);
  window.addEventListener("pagehide", () => clearInterval(timer), {
    once: true,
  });
} catch (err) {
  notice(err.message, true);
}
