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

async function navigateAndVerify(tab, url) {
  const completed = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Navigation timed out before Edge finished loading the page."));
    }, 20000);
    const listener = (tabId, changeInfo, updatedTab) => {
      if (tabId !== tab.id || changeInfo.status !== "complete") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(updatedTab);
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
  await chrome.tabs.update(tab.id, {url});
  const updated = await completed;
  if (!updated.url || new URL(updated.url).origin !== new URL(url).origin)
    throw new Error("Edge loaded a different destination than requested.");
  return updated.url;
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
        const q = String(p.description || "").trim();
        if (!q) return "Could not find a visible button or link: empty description.";
        const normalize = value => String(value || "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
        const describeTarget = el => normalize(el.getAttribute("aria-label") || el.title || el.innerText || el.textContent || el.value);
        const all = [...document.querySelectorAll("button,a,[role=button],input[type=submit]")].filter(visible);
        const exact = [...new Set(all.filter(el => describeTarget(el) === normalize(q)))];
        const matches = exact.length ? exact : [...new Set(all.filter(el => describeTarget(el).includes(normalize(q))))];
        if (!matches.length) return "Could not find a visible button or link matching: " + q;
        if (matches.length !== 1) return "Ambiguous click target for '" + q + "': " + matches.length + " matches. No click was performed.";
        const el = matches[0];
        try { el.click(); } catch (e) { return "Click outcome uncertain for '" + q + "': " + (e?.message || String(e)) + ". No retry was attempted."; }
        return "Clicked unique target " + JSON.stringify(describe(el));
      }
      case "type":
      case "smart_type": {
        const q = String(p.description || p.selector || "").trim();
        if (!q) return "No matching input field: empty description.";
        let candidates = [];
        if (p.selector) {
          try { candidates = [...document.querySelectorAll(p.selector)].filter(visible); }
          catch (_) { return "Invalid input selector; no text was entered."; }
        } else {
          const fields = [...document.querySelectorAll("input,textarea,[contenteditable=true],[role=textbox],[role=searchbox],[role=combobox]")].filter(visible);
          const describeField = el => [el.getAttribute("placeholder"), el.getAttribute("aria-label"), el.getAttribute("name"), el.getAttribute("title"), el.getAttribute("role"), el.labels ? [...el.labels].map(label => label.innerText).join(" ") : ""].filter(Boolean).join(" ");
          const exact = fields.filter(el => describeField(el).trim().toLocaleLowerCase() === q.toLocaleLowerCase());
          candidates = exact.length ? exact : fields.filter(el => describeField(el).toLocaleLowerCase().includes(q.toLocaleLowerCase()));
        }
        candidates = [...new Set(candidates)];
        if (!candidates.length) return "No matching input field found for: " + q;
        if (candidates.length !== 1) return "Ambiguous input target for '" + q + "': " + candidates.length + " matches. No text was entered.";
        const el = candidates[0];
        try {
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
        } catch (e) {
          return "Typing outcome uncertain for '" + q + "': " + (e?.message || String(e)) + ". No alternate input was attempted.";
        }
        return "Entered text in unique input " + JSON.stringify(describe(el));
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

async function executeWhatsApp(p) {
  if (String(p.operation || "") === "open") return "Open WhatsApp Web in the active Edge tab first.";
  if (location.protocol !== "https:" || location.hostname !== "web.whatsapp.com")
    throw new Error("Open WhatsApp Web in the active Edge tab first.");
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const text = el => (el?.innerText || el?.textContent || "").trim();
  const chatRows = () => {
    const cells = [...document.querySelectorAll("#pane-side [data-testid=cell-frame-container]")];
    return (cells.length ? cells : [...document.querySelectorAll("#pane-side [role=row]")]).filter(visible);
  };
  const operation = String(p.operation || "status");
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  if (operation === "status") return JSON.stringify({connected: !!document.querySelector("#pane-side"), title: document.title});
  if (operation === "list_chats") {
    const rows = chatRows().map(text).filter(Boolean);
    return JSON.stringify({chats: [...new Set(rows)].slice(0, Math.min(100, Math.max(1, Number(p.limit) || 30)))});
  }
  if (operation === "peek_chat") {
    const name = String(p.contact || "").trim();
    if (!name) throw new Error("A contact or group name is required.");
    const rows = chatRows().filter(el => text(el).toLocaleLowerCase().includes(name.toLocaleLowerCase()));
    if (rows.length !== 1) throw new Error(rows.length ? "More than one chat matches that name; use a more specific name." : "The chat is not currently visible in the WhatsApp chat list.");
    const row = rows[0];
    const unread = row.querySelector('[data-testid="icon-unread-count"], [aria-label*="unread" i], [aria-label*="não lida" i], [aria-label*="no leída" i]');
    const unreadLabel = (unread?.innerText || unread?.getAttribute("aria-label") || "").match(/\d+/)?.[0];
    return JSON.stringify({contact:name, preview:text(row).slice(0, 1500), unread:!!unread, unreadCount:unread ? Math.max(1, Number(unreadLabel) || 0) : 0});
  }
  const openChat = async name => {
    const search = [...document.querySelectorAll("#side input, #side [contenteditable=true], [data-testid=chat-list-search] input")]
      .find(el => visible(el) && !el.closest("#main"));
    if (!search) throw new Error("WhatsApp chat search is unavailable; ensure the chat list is loaded.");
    search.focus();
    if ("value" in search) {
      const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(search), "value")?.set;
      if (setter) setter.call(search, name); else search.value = name;
    } else search.textContent = name;
    search.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"insertText", data:name}));
    await sleep(900);
    const candidates = chatRows();
    const matches = candidates.filter(el => text(el).toLocaleLowerCase().includes(name.toLocaleLowerCase()));
    if (matches.length !== 1) throw new Error(matches.length ? "More than one chat matches that name; use a more specific contact or group name." : "No matching WhatsApp chat was found.");
    const match = matches[0];
    match.click(); await sleep(500);
  };
  if (operation === "read_messages" || operation === "send_message") {
    const name = String(p.contact || "").trim();
    if (!name || name.length > 200) throw new Error("A valid contact or group name is required.");
    await openChat(name);
  }
  if (operation === "read_messages") {
    const messages = [...document.querySelectorAll("#main [data-testid=msg-container]")].filter(visible)
      .map(el => ({text:text(el).slice(0, 2000), direction:el.querySelector(".message-out") ? "outgoing" : "incoming"}))
      .filter(m => m.text).slice(-Math.min(100, Math.max(1, Number(p.limit) || 30)));
    return JSON.stringify({contact:p.contact, messages});
  }
  if (operation === "send_message") {
    const message = String(p.message || "");
    if (!message.trim() || message.length > 4000) throw new Error("Message must contain 1 to 4000 characters.");
    const box = [...document.querySelectorAll("#main [contenteditable=true]")].find(visible);
    if (!box) throw new Error("The message composer is unavailable in this chat.");
    box.focus(); document.execCommand("insertText", false, message);
    await sleep(150);
    const button = [...document.querySelectorAll("#main button")].find(el => visible(el) && /send/i.test(el.getAttribute("aria-label") || el.title || ""));
    if (!button) throw new Error("The send button was not found; message was not sent.");
    button.click(); await sleep(700);
    const sent = [...document.querySelectorAll("#main [data-testid=msg-container]")]
      .some(el => el.querySelector(".message-out") && text(el).includes(message));
    if (!sent) throw new Error("WhatsApp did not show the outgoing message; delivery is unconfirmed.");
    return JSON.stringify({sent:true, contact:p.contact, verified:true});
  }
  throw new Error("Supported operations: status, list_chats, read_messages, send_message.");
}

async function perform(command) {
  const p = command.parameters || {};
  const action = command.action;
  if (action === "go_to" || action === "new_tab" || action === "search") {
    let url = p.url || "";
    if (action === "search") url = ({google:"https://www.google.com/search?q=",bing:"https://www.bing.com/search?q=",duckduckgo:"https://duckduckgo.com/?q="}[p.engine] || "https://www.google.com/search?q=") + encodeURIComponent(p.query || "");
    if (!/^https?:\/\//i.test(url)) url = "https://" + url;
    if (action === "new_tab") {
      const tab = await chrome.tabs.create({url:"about:blank", active:true});
      if (!tab.id) throw new Error("Edge could not create the requested tab.");
      const destination = await navigateAndVerify(tab, url);
      return "Opened new tab: " + destination;
    }
    const tab = await activeTab();
    const destination = await navigateAndVerify(tab, url);
    return "Navigated existing Edge tab to " + destination;
  }
  if (action === "whatsapp" && String(p.operation || "") === "open") {
    const tab = await chrome.tabs.create({url:"about:blank", active:true});
    const destination = await navigateAndVerify(tab, "https://web.whatsapp.com/");
    return "Opened WhatsApp Web in Edge: " + destination + ". Sign in there if needed, then ask Mark-LV to check status.";
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
  if (action === "whatsapp") {
    const tabs = await chrome.tabs.query({url:"https://web.whatsapp.com/*"});
    const tab = tabs.find(t => t.id);
    if (!tab) throw new Error("WhatsApp Web is not open in an Edge tab.");
    const [{result}] = await chrome.scripting.executeScript({target:{tabId:tab.id}, func:executeWhatsApp, args:[p]});
    return String(result ?? "");
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
