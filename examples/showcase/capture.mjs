// Screenshot the real sites for the film: chat, decide, admin, speech (gateway :8090) and the emoji book and triage dashboard
// (dashboard_server.py :8766). Drives them with real requests, so the models start on demand.
//   node examples/showcase/capture.mjs [only,...]
import { chromium } from "playwright";
import { readFileSync, mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const OUT = `${ROOT}/data/showcase/captures`;
const GW = "http://127.0.0.1:8090", DASH = "http://127.0.0.1:8766";
const token = readFileSync(`${ROOT}/.gateway/token`, "utf8").trim();
mkdirSync(OUT, { recursive: true });
const only = process.argv[2]?.split(",");

const shots = {
  async chat(page) {
    await page.goto(`${GW}/`);
    await page.fill("#input", "In two sentences: why run language models locally on a Mac?");
    await page.click("#send");
    await page.waitForTimeout(1500);
    await page.waitForFunction(() => /send/i.test(document.querySelector("#send").innerText), null, { timeout: 180000 });
    await page.waitForTimeout(800);
  },
  async jev(page) {
    await page.goto(`${GW}/jev`);
    await page.click("#samples button >> nth=0");
    await page.click("#run");
    await page.waitForFunction(() => document.querySelector("#out").innerText.includes("%"), null, { timeout: 120000 });
    await page.waitForTimeout(500);
  },
  async admin(page) {
    await page.goto(`${GW}/admin#token=${token}`);
    await page.waitForTimeout(3000);
  },
  async speech(page) {
    await page.goto(`${GW}/speech`);
    await page.waitForTimeout(1500);
  },
  async book(page) {
    // the page follows the newest scored phrase; serve only the phrases of page 1 so it stays there
    await page.route("**/book_events*", async route => {
      const body = await (await route.fetch()).text();
      await route.fulfill({ body: body.split("\n").filter(l => l && JSON.parse(l).w0 < 180).join("\n") });
    });
    await page.goto(`${DASH}/book`);
    await page.waitForTimeout(3000);
    await page.hover('ruby[data-i="17"]');
    await page.waitForTimeout(500);
  },
  async triage(page) {
    await page.goto(`${DASH}/`);
    await page.waitForTimeout(2500);
  },
};

const browser = await chromium.launch();
for (const [name, run] of Object.entries(shots)) {
  if (only && !only.includes(name)) continue;
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, colorScheme: "dark" });
  try {
    await run(page);
    await page.screenshot({ path: `${OUT}/${name}.png` });
    console.log("ok", name);
  } catch (e) {
    console.log("FAILED", name, e.message.split("\n")[0]);
    await page.screenshot({ path: `${OUT}/${name}.png` });
  }
  await page.close();
}
await browser.close();
