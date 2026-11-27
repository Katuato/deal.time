/* Public Mini App configuration. Never put OAuth secrets or bot tokens here. */
window.DEALENDER_CONFIG = Object.freeze({
  apiBaseUrl: '/api/v1',
  demoMode: new URLSearchParams(window.location.search).get('demo') === '1'
});
