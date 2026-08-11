const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const page = fs.readFileSync("app/page.js", "utf8");
const styles = fs.readFileSync("app/globals.css", "utf8");
const nextConfig = fs.readFileSync("next.config.mjs", "utf8");
const packageJson = JSON.parse(fs.readFileSync("package.json", "utf8"));

test("landing page contains an authored interactive scene", () => {
  assert.match(page, /Canvas/);
  assert.match(page, /useFrame/);
  assert.match(page, /<button\b/);
  assert.doesNotMatch(page, /raw\.githack\.com|jsdelivr\.net|Environment\s+.*preset/);
  if (process.env.CHITTI_MODEL_LOOP === "1") {
    assert.doesNotMatch(page, /Ideas with a little more dimension\./);
    assert.doesNotMatch(
      page,
      /A deterministic Next\.js landing page fixture with a live React Three\s+Fiber scene,\s+ready for a safe build\./,
    );
    assert.doesNotMatch(page, /CHITTI \/ MOTION LAB/);
  }
});

test("landing page declares responsive layout rules", () => {
  assert.match(styles, /@media\s*\(/);
  assert.match(styles, /\.hero/);
});

test("landing page is statically exportable", () => {
  assert.match(nextConfig, /output\s*:\s*["']export["']/);
  assert.equal(packageJson.scripts.export, "test -f out/index.html");
});
