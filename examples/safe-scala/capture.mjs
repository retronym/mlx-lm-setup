// Screenshot the real sources the film cites: the TACIT README, the paper's arXiv page, the Scala 3 safe-mode reference.
//   node examples/safe-scala/capture.mjs [only,...]
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const OUT = `${ROOT}/data/safe-scala/captures`;
mkdirSync(OUT, { recursive: true });
const only = process.argv[2]?.split(",");

const shots = {
  tacit: { url: "https://github.com/lampepfl/tacit", scroll: "article h1" },
  arxiv: { url: "https://arxiv.org/abs/2603.00991" },
  safe: { url: "https://nightly.scala-lang.org/docs/reference/experimental/capture-checking/safe.html", clipTo: "text=It makes sense for agentic tooling" },
};

const browser = await chromium.launch();
for (const [name, { url, scroll, clipTo }] of Object.entries(shots)) {
  if (only && !only.includes(name)) continue;
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, colorScheme: "dark" });
  try {
    await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
    // scrollIntoView also works where the page scrolls an inner container (the Scala docs), then back off for sticky headers
    if (scroll) await page.locator(scroll).first().evaluate(e => {
      e.scrollIntoView({ block: "start" });
      let p = e.parentElement;
      while (p && p.scrollHeight <= p.clientHeight) p = p.parentElement;
      (p ?? document.scrollingElement).scrollBy(0, -90);
    });
    await page.waitForTimeout(1200);
    if (clipTo) {
      // the Scala docs scroll an inner container: render a tall page and cut a 1440x900 window starting just above the element
      await page.setViewportSize({ width: 1440, height: 2600 });
      await page.waitForTimeout(800);
      const box = await page.locator(clipTo).first().boundingBox();
      await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: 0, y: Math.max(0, box.y - 140), width: 1440, height: 900 } });
    } else await page.screenshot({ path: `${OUT}/${name}.png` });
    console.log("ok", name);
  } catch (e) {
    console.log("FAILED", name, e.message.split("\n")[0]);
  }
  await page.close();
}
await browser.close();
