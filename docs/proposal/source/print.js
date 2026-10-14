// Print proposal.html to PDF with headless Chrome (puppeteer from the mermaid-cli install).
const path = require('path');
const puppeteer = require('puppeteer')  // npm i puppeteer@23.11.1;
(async () => {
  const [, , input, output] = process.argv;
  const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.goto('file://' + path.resolve(input), { waitUntil: 'networkidle0' });
  await page.pdf({
    path: output, format: 'A4', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true,
    headerTemplate: '<div></div>',
    // Footer on every page; the cover gets it too, which is acceptable for a proposal.
    footerTemplate: `<div style="width:100%;font-family:'Noto Sans',sans-serif;font-size:7.5pt;color:#7a8696;padding:0 18mm;display:flex;justify-content:space-between;">
      <span>Secure AI Document Extraction Platform · Proposal · Confidential</span>
      <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>`,
  });
  await browser.close();
})();
