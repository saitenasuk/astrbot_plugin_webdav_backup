// Run with Node.js and Playwright (Chromium): node --test tests/dashboard.cjs
const { after, before, test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const schema = require("../_conf_schema.json");
const settings = Object.fromEntries(
  Object.entries(schema).map(([key, spec]) => [key, spec.default]),
);
const remote = "https://dav.example.com/dav/nightly/one.zip";
const runs = [
  {
    mode: "local",
    ok: true,
    time: "2026-11-25T04:00:00Z",
    name: "local.zip",
    size: 2048,
  },
  {
    mode: "webdav",
    ok: false,
    time: "2026-11-25T03:00:00Z",
    error: "WebDAV PUT 返回 HTTP 403",
  },
  {
    mode: "webdav",
    ok: true,
    time: "2026-11-24T03:00:00Z",
    remote,
    name: "one.zip",
    size: 1024,
  },
  ...[23, 22, 21].map((day) => ({
    mode: "webdav",
    ok: true,
    time: `2026-11-${day}T03:00:00Z`,
    name: `old-${day}.zip`,
    size: 512,
  })),
];
let browser;
before(async () => {
  browser = await chromium.launch();
});
after(async () => {
  await browser?.close();
});

// Both screens use the packaged HTML, CSS and JS; only the host bridge is replaced.
async function openPage(t, history, url, width = 1280) {
  const page = await browser.newPage({
    viewport: { width, height: 800 },
    timezoneId: "UTC",
  });
  t.after(() => page.close());
  await page.clock.install();
  await page.addInitScript(
    (state) => {
      window.snapshot = state;
      window.posts = [];
      window.AstrBotPluginView = {
        ready: async () => {},
        apiGet: async () => structuredClone(state),
        apiPost: async (endpoint, payload) => {
          window.posts.push({ endpoint, payload });
          if (endpoint === "settings") Object.assign(state.settings, payload);
          if (endpoint === "run") state.phases[payload.mode] = "排队中";
        },
      };
    },
    {
      settings: { ...settings, url },
      phases: { local: "空闲", webdav: "空闲" },
      next: {},
      plan_errors: {},
      history,
    },
  );
  await page.route("http://dashboard.test/**", (route) =>
    route.fulfill({
      path: path.join(
        root,
        "views/dashboard",
        new URL(route.request().url()).pathname.slice(1) || "index.html",
      ),
    }),
  );
  await page.goto("http://dashboard.test/");
  await page.locator("#dashboard").waitFor();
  return page;
}

test("local success does not mask remote failure; receipts remain usable while polling", async (t) => {
  const page = await openPage(t, runs, "https://dav.example.com/dav/", 375);
  assert.match(await page.locator("#overview").innerText(), /403/);
  assert.match(await page.locator(".receipt").innerText(), /11月24日/);
  assert.equal(
    await page.locator("#receipt-dir").innerText(),
    "https://dav.example.com/dav/nightly/",
  );
  assert.equal(await page.locator("#settings").evaluate((e) => e.open), false);
  assert.equal(await page.locator(".run").count(), 5);
  await page.locator("#history-more").click();
  assert.equal(await page.locator(".run").count(), 6);
  await page.locator("#history-filter").selectOption("webdav");
  await page.locator("#failures-only").check();
  assert.equal(await page.locator(".run").count(), 1);
  await page.locator(".run summary").click();
  assert.match(await page.locator(".run-details").innerText(), /403/);
  await page.evaluate(() =>
    snapshot.history.unshift({
      mode: "local",
      ok: true,
      time: "2026-11-26T04:00:00Z",
      name: "two.zip",
      size: 1024,
    }),
  );
  await page.clock.fastForward(5000);
  assert.equal(await page.locator(".run").evaluate((e) => e.open), true);
  await page.locator("#failures-only").uncheck();
  await page.locator(".run summary").nth(1).click();
  await page
    .locator(".run[open]")
    .filter({ hasText: remote })
    .getByRole("button", { name: "复制路径" })
    .click();
  assert.match(await page.locator("#save-toast").innerText(), /路径已/);
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  await page.locator("#overview-action").click();
  assert(await page.locator("#url").isVisible());
});

test("first-run setup, per-target drafts, probe, save, manual run and schedule errors", async (t) => {
  const page = await openPage(t, [], "");
  assert.match(
    await page.locator(".receipt").innerText(),
    /最近 30 条记录中无已校验/,
  );
  assert(await page.locator("#target-webdav .run-now").isHidden());
  await page.locator("#target-webdav .edit").click();
  await page.locator("#url").fill("https://dav.example.com/dav/");
  await page.locator("#remote_dir").fill("/nightly");
  await page.locator("[data-mode=local]").click();
  await page.locator("[data-mode=webdav]").click();
  assert.equal(await page.locator("#remote_dir").inputValue(), "/nightly");
  await page.locator("#test-connection").click();
  assert.equal(
    await page.evaluate(() => posts.at(-1).endpoint),
    "test-connection",
  );
  assert(await page.locator("#target-webdav .run-now").isHidden());
  await page.locator("#save").click();
  await page.locator("#target-webdav .run-now").click();
  assert.deepEqual(await page.evaluate(() => posts.at(-1)), {
    endpoint: "run",
    payload: { mode: "webdav" },
  });
  assert(await page.locator("#target-webdav .run-now").isDisabled());
  await page.evaluate(() => {
    snapshot.plan_errors.local = "计划无效，请检查 Cron 和时区。";
  });
  await page.clock.fastForward(5000);
  assert.match(await page.locator("#overview-title").innerText(), /本地.*计划/);
});

test("legacy success is not a receipt and next run stays in the plan time zone", async (t) => {
  const page = await openPage(t, runs.slice(-1), "https://dav.example.com/");
  assert.match(await page.locator("#overview-title").innerText(), /旧版/);
  assert.match(await page.locator(".receipt").innerText(), /无已校验/);
  await page.evaluate(() => {
    snapshot.next.webdav = "2026-11-27T03:00:00+08:00";
    snapshot.settings.webdav_timezone = "Asia/Shanghai";
    snapshot.phases.webdav = "正在上传并校验";
  });
  await page.clock.fastForward(5000);
  assert.match(
    await page.locator("#target-webdav .next").innerText(),
    /03:00.*Asia\/Shanghai/,
  );
  assert.match(
    await page.locator("#overview-title").innerText(),
    /WebDAV.*正在上传并校验/,
  );
  assert(await page.locator("#target-webdav .run-now").isDisabled());
});
