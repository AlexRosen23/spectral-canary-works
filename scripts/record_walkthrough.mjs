// Record the control-room walkthrough (docs/media/walkthrough.webm) and screenshots with Playwright.
//
//   1. serve a recorded run:  SCW_DB=var/runs/llm-latest/registry.sqlite SCW_LLM_DB=var/runs/llm-latest/llm-registry.sqlite \
//                             uv run uvicorn spectral_canary.api:app --port 5142      (after `make web-build`)
//   2. record:                NODE_PATH=<dir with playwright@1.63.0> node scripts/record_walkthrough.mjs http://127.0.0.1:5142
//
// Playwright is not a project dependency (one-off tool); any playwright 1.6x works.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const base = process.argv[2] || "http://127.0.0.1:5142";
const out = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..", "docs", "media");
fs.mkdirSync(out, { recursive: true });
const tmp = fs.mkdtempSync(path.join(out, ".video-"));

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  recordVideo: { dir: tmp, size: { width: 1280, height: 800 } },
  colorScheme: "dark",
});
const page = await ctx.newPage();
const pause = (ms) => page.waitForTimeout(ms);

await page.goto(base, { waitUntil: "networkidle" });
await pause(2500);
await page.screenshot({ path: path.join(out, "control-room-ml-lane.png") });
await page.mouse.wheel(0, 700);
await pause(2500);

// LLM lane
await page.locator("#llm-lane").scrollIntoViewIfNeeded();
await pause(1500);
await page.locator("#llm-lane").screenshot({ path: path.join(out, "llm-lane-releases.png") });
const rows = page.locator(".llm-releases tbody tr");
const n = await rows.count();
for (let i = 0; i < n; i++) {
  await rows.nth(i).click();
  await pause(2200);
}
// the blocked regression (p3) and the rolled-back release, with its generated postmortem
const blocked = page.locator(".llm-releases tbody tr", { hasText: "triage-p3" });
if (await blocked.count()) {
  await blocked.first().click();
  await pause(3000);
}
const rolled = page.locator(".llm-releases tbody tr", { hasText: "rolled back" });
if (await rolled.count()) {
  await rolled.first().click();
  await pause(2000);
  const pm = page.locator("#llm-lane details", { hasText: "Generated postmortem" });
  if (await pm.count()) {
    await pm.locator("summary").click();
    await pause(1500);
  }
  await page.locator("#llm-lane").screenshot({ path: path.join(out, "llm-lane-rollback.png") });
  await page.mouse.wheel(0, 500);
  await pause(3000);
}
const reg = page.locator("#llm-lane details", { hasText: "Bundle registry" });
if (await reg.count()) {
  await reg.locator("summary").click();
  await reg.scrollIntoViewIfNeeded();
  await pause(3000);
}
await ctx.close();
await browser.close();
const vid = fs.readdirSync(tmp).find((f) => f.endsWith(".webm"));
fs.renameSync(path.join(tmp, vid), path.join(out, "walkthrough.webm"));
fs.rmSync(tmp, { recursive: true, force: true });
console.log("wrote", path.join(out, "walkthrough.webm"), fs.statSync(path.join(out, "walkthrough.webm")).size, "bytes");
