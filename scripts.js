document.addEventListener("DOMContentLoaded", function () {
  const toggleBtn = document.getElementById('toggle-btn');
  const ohlcTable = document.getElementById('ohlc-table');
  toggleBtn.addEventListener('click', () => {
      if (ohlcTable.style.display === 'none') {
          ohlcTable.style.display = 'block';
          toggleBtn.textContent = 'Hide Table';
      } else {
          ohlcTable.style.display = 'none';
          toggleBtn.textContent = 'Show Table';
      }
  });

  // Theme toggle
  function setTheme(theme) {
      document.body.setAttribute('data-theme', theme);
      localStorage.setItem('theme_mode', theme);
      document.getElementById('theme-icon').innerHTML = theme === "light" ? "☀" : "☾";
  }
  document.getElementById('theme-btn').onclick = function () {
      let nextTheme = document.body.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      setTheme(nextTheme);
  };
  // Load theme from localStorage
  const saved = localStorage.getItem('theme_mode');
  if(saved === "light" || saved === "dark") setTheme(saved);
});
