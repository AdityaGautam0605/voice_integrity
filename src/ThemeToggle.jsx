import { useEffect, useState } from 'react';
export default function ThemeToggle() {
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || 'light');
  useEffect(() => {
    const media = matchMedia('(prefers-color-scheme: dark)');
    const sync = () => {
      let saved;
      try { saved = localStorage.getItem('vif-theme'); } catch { /* Use system preference. */ }
      const value = ['light', 'dark'].includes(saved) ? saved : media.matches ? 'dark' : 'light';
      document.documentElement.dataset.theme = value;
      setTheme(value);
    };
    window.addEventListener('storage', sync);
    media.addEventListener('change', sync);
    return () => { window.removeEventListener('storage', sync); media.removeEventListener('change', sync); };
  }, []);
  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    setTheme(next);
    try { localStorage.setItem('vif-theme', next); } catch { /* Theme still works this visit. */ }
  };
  return <button className="button secondary theme-toggle" onClick={toggle} aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'} title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}>
    <span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span><span>{theme === 'dark' ? 'Light' : 'Dark'}</span>
  </button>;
}
