function shouldHideOnClose(quitting) { return !quitting; }
function isApiRequest(path, method) {
  return typeof path === 'string' && path.startsWith('/api/') && !path.includes('..')
    && !path.includes('\\') && ['GET', 'POST', 'PATCH', 'PUT', 'DELETE'].includes(method);
}
module.exports = { shouldHideOnClose, isApiRequest };
