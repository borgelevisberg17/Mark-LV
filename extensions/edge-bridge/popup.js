const status = document.getElementById("status");
(async () => {
  const {token=""} = await chrome.storage.local.get("token");
  if (!token) { status.textContent = "Token not configured. Open settings."; return; }
  try {
    const r = await fetch("http://127.0.0.1:18763/health", {headers:{Authorization:"Bearer "+token},cache:"no-store"});
    status.textContent = r.ok ? "Connected to Mark-LV local service." : "Local service rejected the token.";
  } catch (_) { status.textContent = "Mark-LV is not running or the bridge is unavailable."; }
})();
document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());