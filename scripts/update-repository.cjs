const { applyUpdate, checkUpdate } = require('../desktop/electron/source-update.cjs');
try {
  const result = process.argv.includes('--check') ? checkUpdate(process.env.AGENT_UPDATE_ROOT) : applyUpdate(process.env.AGENT_UPDATE_ROOT);
  if (process.argv.includes('--check')) { console.log(JSON.stringify(result)); process.exit(0); }
  console.log(result.available ? 'Source updated. Preparing the new version...' : 'Already up to date.');
} catch (error) { console.error(error.message); process.exitCode = 1; }
