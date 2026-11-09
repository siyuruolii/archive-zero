function shouldHideOnClose(quitting) { return !quitting; }
function restoreWindow(window) {
  if (!window || window.isDestroyed()) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}
function bindTrayRestore(tray, getWindow) {
  const restore = () => restoreWindow(getWindow());
  tray.on('click', restore);
  tray.on('double-click', restore);
}
function isApiRequest(path, method) {
  return typeof path === 'string' && path.startsWith('/api/') && !path.includes('..')
    && !path.includes('\\') && ['GET', 'POST', 'PATCH', 'PUT', 'DELETE'].includes(method);
}
module.exports = { shouldHideOnClose, isApiRequest, restoreWindow, bindTrayRestore };
