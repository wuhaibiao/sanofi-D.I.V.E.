const fs = require('fs');
const path = require('path');
const {pathToFileURL} = require('url');
const modules = '/Users/OleniusBlanc227/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const {chromium} = require(path.join(modules, 'playwright'));
const sharp = require(path.join(modules, 'sharp'));
const folder = __dirname;

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const context = await browser.newContext({acceptDownloads: true, deviceScaleFactor: 3,
      viewport: {width: 1600, height: 1000}});
    const page = await context.newPage();
    await page.goto(pathToFileURL(path.join(folder, 'memory-workflow.html')).href);
    await page.evaluate(() => document.fonts.ready);
    if (await page.locator('html').getAttribute('data-theme') !== 'light') {
      await page.locator('#btn-theme').click();
    }
    for (const [format, name] of [['svg-light', 'memory-workflow.figure.svg'], ['png', 'memory-workflow.figure.png']]) {
      await page.locator('#btn-export').click();
      const pending = page.waitForEvent('download', {timeout: 45000});
      await page.locator(`[data-format="${format}"]`).click();
      const download = await pending;
      await download.saveAs(path.join(folder, name));
    }
    await page.locator('.diagram-container').screenshot({path: path.join(folder, 'diagram-preview.png')});
    const svgPage = await context.newPage();
    await svgPage.goto(pathToFileURL(path.join(folder, 'memory-workflow.figure.svg')).href);
    await svgPage.evaluate(() => document.fonts.ready);
    await svgPage.locator('svg').first().screenshot({path: path.join(folder, 'memory-workflow.body.png'),
      animations: 'disabled'});
  } finally {
    await browser.close();
  }
  const symbols = {
    memory: '<path d="M5 5h14v14H5zM8 9h8M8 12h8M8 15h5"/>',
    storage: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
    search: '<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6M7 10h6M10 7v6"/>',
    rank: '<path d="M8 5h12M8 12h8M8 19h4M3 3v18M1 19l2 2 2-2"/>',
    agent: '<rect x="4" y="6" width="16" height="13" rx="3"/><path d="M12 2v4M8 11v2M16 11v2M8 16h8M1 10v6M23 10v6"/>',
    references: '<path d="M12 5v16M3 4c4-1 6-1 9 1 3-2 5-2 9-1v15c-4-1-6-1-9 1-3-2-5-2-9-1z"/>'
  };
  fs.mkdirSync(path.join(folder, 'icons'), {recursive: true});
  for (const [name, body] of Object.entries(symbols)) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" stroke="#156A73" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${body}</g></svg>`;
    fs.writeFileSync(path.join(folder, 'icons', name + '.svg'), svg);
    await sharp(Buffer.from(svg)).png().toFile(path.join(folder, 'icons', name + '.png'));
  }
  console.log('Canonical SVG and PNG exported; six section icons created.');
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
