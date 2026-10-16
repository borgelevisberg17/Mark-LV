const BASE = "http://127.0.0.1:18763";
let running = false;

async function getToken() {
  const { token = "" } = await chrome.storage.local.get("token");
  return token.trim();
}

async function request(path, options = {}) {
  const token = await getToken();
  if (!token) throw new Error("Configure the Mark-LV bridge token in extension options.");
  const response = await fetch(BASE + path, {
    ...options,
    headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json", ...(options.headers || {}) },
    cache: "no-store"
  });
  if (!response.ok) throw new Error("Mark-LV bridge returned HTTP " + response.status);
  return response.status === 204 ? {} : response.json();
}

async function activeTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const tab = tabs[0];
  if (!tab?.id || !/^https?:\/\//i.test(tab.url || "")) {
    throw new Error("Select a normal website tab in Edge. Browser-internal pages cannot be automated.");
  }
  return tab;
}

function executeOnPage(action, p) {
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const findByText = (selector, text) => [...document.querySelectorAll(selector)]
    .find(el => visible(el) && (el.innerText || el.textContent || "").trim().toLowerCase().includes(String(text).toLowerCase()));
  const describe = el => el ? ({tag: el.tagName.toLowerCase(), text: (el.innerText || el.textContent || el.value || "").trim().slice(0, 300), selector: el.id ? "#" + el.id : el.getAttribute("aria-label") ? '[aria-label="' + el.getAttribute("aria-label") + '"]' : el.tagName.toLowerCase()}) : null;
  try {
    switch (action) {
      case "get_url": return location.href;
      case "get_text": return (document.body?.innerText || "").slice(0, 18000);
      case "click": {
        const el = (p.selector && document.querySelector(p.selector)) || (p.text && findByText("button,a,[role=button],input[type=submit]", p.text));
        if (!el) return "No matching clickable element found.";
        el.click(); return "Clicked " + JSON.stringify(describe(el));
      }
      case "smart_click": {
        const q = String(p.description || "").toLowerCase();
        const el = findByText("button,a,[role=button],input[type=submit]", q) ||
          [...document.querySelectorAll("button,a,[role=button]")].find(e => visible(e) && q && (e.getAttribute("aria-label") || e.title || "").toLowerCase().includes(q));
        if (!el) return "Could not find a visible button or link matching: " + q;
        el.click(); return "Clicked " + JSON.stringify(describe(el));
      }
      case "type":
      case "smart_type": {
        let el = p.selector ? document.querySelector(p.selector) : null;
        if (!el && p.description) el = findByText("label", p.description)?.control || [...document.querySelectorAll("input,textarea,[contenteditable=true]")].find(e => visible(e) && (e.getAttribute("placeholder") || e.getAttribute("aria-label") || e.name || "").toLowerCase().includes(String(p.description).toLowerCase()));
        if (!el) return "No matching input field found.";
        el.focus();
        if (p.clear_first !== false) {
          if ("value" in el) el.value = "";
          else el.textContent = "";
        }
        if ("value" in el) {
          const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set;
          if (setter) setter.call(el, String(p.text || "")); else el.value = String(p.text || "");
        } else el.textContent = String(p.text || "");
        el.dispatchEvent(new Event("input", {bubbles:true}));
        el.dispatchEvent(new Event("change", {bubbles:true}));
        return "Entered text in " + JSON.stringify(describe(el));
      }
      case "fill_form": {
        const fields = p.fields || {}; const done = [];
        for (const [selector, value] of Object.entries(fields)) {
          const el = document.querySelector(selector);
          if (!el) { done.push(selector + ": not found"); continue; }
          el.focus();
          if ("value" in el) {
            const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set;
            if (setter) setter.call(el, String(value)); else el.value = String(value);
          } else el.textContent = String(value);
          el.dispatchEvent(new Event("input", {bubbles:true}));
          el.dispatchEvent(new Event("change", {bubbles:true}));
          done.push(selector + ": filled");
        }
        return done.join("\n");
      }
      case "scroll": window.scrollBy({top: (p.direction === "up" ? -1 : 1) * Math.min(3000, Math.max(0, Number(p.amount) || 500)), behavior:"smooth"}); return "Scrolled " + (p.direction || "down");
      case "press": {
        const el = document.activeElement || document.body;
        el.dispatchEvent(new KeyboardEvent("keydown", {key:p.key || "Enter", bubbles:true}));
        el.dispatchEvent(new KeyboardEvent("keyup", {key:p.key || "Enter", bubbles:true}));
        if ((p.key || "Enter") === "Enter" && el.form) el.form.requestSubmit();
        return "Pressed " + (p.key || "Enter");
      }
      default: return "__UNSUPPORTED__";
    }
  } catch (e) { return "Browser page error: " + (e?.message || String(e)); }
}

async function perform(command) {
  const p = command.parameters || {};
  const action = command.action;
  if (action === "go_to" || action === "new_tab" || action === "search") {
    let url = p.url || "";
    if (action === "search") url = ({google:"https://www.google.com/search?q=",bing:"https://www.bing.com/search?q=",duckduckgo:"https://duckduckgo.com/?q="}[p.engine] || "https://www.google.com/search?q=") + encodeURIComponent(p.query || "");
    if (!/^https?:\/\//i.test(url)) url = "https://" + url;
    if (action === "new_tab") {
      const tab = await chrome.tabs.create({url, active:true});
      return "Opened new tab: " + (tab.url || url);
    }
    const tab = await activeTab();
    await chrome.tabs.update(tab.id, {url});
    return "Navigated existing Edge tab to " + url;
  }
  if (action === "reload" || action === "close_tab") {
    const tab = await activeTab();
    if (action === "reload") await chrome.tabs.reload(tab.id);
    else await chrome.tabs.remove(tab.id);
    return action === "close_tab" ? "Closed active tab." : "Reload completed.";
  }
  if (action === "back" || action === "forward") {
    const tab = await activeTab();
    const [{result}] = await chrome.scripting.executeScript({
      target: {tabId: tab.id},
      func: direction => { if (direction === "back") history.back(); else history.forward(); return direction + " requested."; },
      args: [action]
    });
    return String(result || action + " requested.");
  }
  const tab = await activeTab();
  const [{result}] = await chrome.scripting.executeScript({
    target: {tabId: tab.id},
    func: executeOnPage,
    args: [action, p]
  });
  if (result === "__UNSUPPORTED__") throw new Error("Unsupported action: " + action);
  return String(result ?? "");
}

async function loop() {
  if (running) return;
  running = true;
  while (true) {
    try {
      await request("/heartbeat", {method:"POST", body:"{}"});
      const {command} = await request("/poll");
      if (command) {
        let result = "";
        let ok = true;
        try { result = await perform(command); }
        catch (e) { result = e?.message || String(e); ok = false; }
        await request("/result", {method:"POST", body:JSON.stringify({id:command.id, result, ok})});
      }
    } catch (_) {
      await new Promise(resolve => setTimeout(resolve, 1800));
    }
  }
}

chrome.runtime.onInstalled.addListener(() => { loop(); });
chrome.runtime.onStartup.addListener(() => { loop(); });
loop();
