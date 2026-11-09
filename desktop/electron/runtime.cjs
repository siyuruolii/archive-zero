const path = require('node:path');

function backendRuntime(app, resources, env, root) {
  if (app.isPackaged) return {
    executable: path.join(resources, 'backend', 'archive-zero-backend.exe'),
    args: [],
    cwd: app.getPath('userData'),
    env: { ...env,
      AGENT_DATA_DIR: env.AGENT_DATA_DIR || path.join(app.getPath('userData'), 'data'),
      AGENT_FRONTEND_DIR: path.join(resources, 'app', 'dist') }
  };
  return {
    executable: env.AGENT_PYTHON || path.join(root, '.venv', 'Scripts', 'python.exe'),
    args: ['-X', 'utf8', '-m', 'reading_agent.server'], cwd: root, env: { ...env, AGENT_DATA_DIR: env.AGENT_DATA_DIR || path.join(root, ".dev-data") }
  };
}
module.exports = { backendRuntime };
