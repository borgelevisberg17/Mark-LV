const token = document.getElementById("token");
const status = document.getElementById("status");
chrome.storage.local.get("token", data => { token.value = data.token || ""; });
document.getElementById("save").addEventListener("click", async () => {
  const value = token.value.trim();
  if (value.length < 32) { status.textContent = "Token must contain at least 32 characters."; return; }
  await chrome.storage.local.set({token:value});
  status.textContent = "Token saved. The extension will reconnect automatically.";
});