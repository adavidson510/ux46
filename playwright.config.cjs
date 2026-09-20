const {defineConfig} = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/ui', timeout: 20000, workers: 1,
  use: {browserName: 'chromium', headless: true},
});
