(() => {
  let preference;
  try { preference = localStorage.getItem('vif-theme'); } catch { /* Storage may be disabled. */ }
  document.documentElement.dataset.theme = ['light', 'dark'].includes(preference) ? preference : (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
})();
