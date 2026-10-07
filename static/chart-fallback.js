// Load the vendored Chart.js if the CDN copy didn't load (offline demo).
// Kept as a file (not inline) so the Content-Security-Policy can forbid inline scripts.
if (!window.Chart) {
  document.write('<script src="/static/vendor/chart.umd.min.js"><\/script>');
}
