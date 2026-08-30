/* VolunteerDB manual: the audience switch and the live search box.
 *
 * Audience: "vdb-audience" in localStorage ("dev" or "user"). The technical
 * sidebar groups carry .vdb-dev-only (set at build time in conf.py) and CSS
 * hides them unless <html data-audience="dev">; page.html sets that attribute
 * before first paint, this file keeps the switch, the buttons and ?dev=1 in
 * step with it.
 *
 * Search: the sidebar form is Furo's own and still submits to Sphinx's
 * search.html. While the app answers /manual/_search with JSON, typing shows
 * live results under the box instead; any failure (no app behind the files,
 * an expired session that lands on the login page) leaves the form alone.
 *
 * That fallback is not everyone's to take: the user guide is public and
 * Sphinx's search page is not, so a signed-out reader submitting the form
 * would land on /login. The endpoint says which reader this is ("fallback":
 * false), and the offer is withdrawn -- the box stops promising a search it
 * cannot deliver, and the form stops submitting.
 */
(function () {
  "use strict";
  var KEY = "vdb-audience";
  var html = document.documentElement;
  var root = html.dataset.content_root || "./";

  function isDev() { return html.dataset.audience === "dev"; }
  function setAudience(dev) {
    if (dev) { html.dataset.audience = "dev"; } else { delete html.dataset.audience; }
    try { localStorage.setItem(KEY, dev ? "dev" : "user"); } catch (e) { /* private mode */ }
    syncToggle();
  }
  var toggle = document.getElementById("vdb-audience-toggle");
  function syncToggle() { if (toggle) { toggle.checked = isDev(); } }
  syncToggle();
  if (toggle) { toggle.addEventListener("change", function () { setAudience(toggle.checked); clear(); }); }
  var buttons = document.querySelectorAll("[data-vdb-audience]");
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].addEventListener("click", function (event) {
      setAudience(event.currentTarget.getAttribute("data-vdb-audience") === "dev");
    });
  }
  if (/[?&]dev=1(&|$)/.test(location.search)) { setAudience(true); }

  var form = document.getElementById("vdb-search-form");
  var input = form && form.querySelector("input[name=q]");
  var list = document.getElementById("vdb-search-results");
  if (!form || !input || !list) { return; }

  var timer = 0, seq = 0, active = -1, results = [], unavailable = false, open = false, dismissed = false;
  /* true until the endpoint says otherwise: with no app behind the files the
     form is all there is, and Sphinx's search.html sits right beside them. */
  var fallback = true;

  function span(cls, text) {
    var el = document.createElement("span");
    el.className = cls;
    el.textContent = text;
    return el;
  }
  function render() {
    list.textContent = "";
    open = !unavailable && !dismissed && input.value.trim().length >= 2;
    if (!open) {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      return;
    }
    if (!results.length) {
      var none = document.createElement("li");
      none.className = "vdb-hit-empty";
      none.textContent = fallback
        ? "No page matches. Press Enter to search every word."
        : "No page matches.";
      list.appendChild(none);
    }
    for (var i = 0; i < results.length; i++) {
      var hit = results[i];
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      li.id = "vdb-hit-" + i;
      li.setAttribute("aria-selected", i === active ? "true" : "false");
      var a = document.createElement("a");
      a.href = hit.url;
      a.appendChild(span("vdb-hit-title", hit.title));
      if (hit.section) { a.appendChild(span("vdb-hit-section", hit.section)); }
      if (hit.snippet) { a.appendChild(span("vdb-hit-snippet", hit.snippet)); }
      li.appendChild(a);
      list.appendChild(li);
    }
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (active >= 0) { input.setAttribute("aria-activedescendant", "vdb-hit-" + active); }
    else { input.removeAttribute("aria-activedescendant"); }
  }
  function clear() { results = []; active = -1; render(); }

  function query(q) {
    var mine = ++seq;
    var url = root + "_search?q=" + encodeURIComponent(q) + "&audience=" + (isDev() ? "all" : "user");
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        var type = response.headers.get("content-type") || "";
        if (!response.ok || type.indexOf("application/json") < 0) { throw new Error(String(response.status)); }
        return response.json();
      })
      .then(function (data) {
        if (mine !== seq) { return; }
        unavailable = false;
        fallback = data.fallback !== false;
        results = data.results || [];
        active = -1;
        render();
      })
      .catch(function () {
        if (mine !== seq) { return; }
        unavailable = true;
        results = [];
        render();
      });
  }

  form.addEventListener("submit", function (event) {
    /* Enter with no hit to jump to, on a page whose reader may not open
       search.html: swallow it rather than bounce them to the sign-in. */
    if (!fallback) { event.preventDefault(); }
  });
  input.addEventListener("input", function () {
    clearTimeout(timer);
    dismissed = false;
    var q = input.value.trim();
    if (q.length < 2) { clear(); return; }
    timer = setTimeout(function () { query(q); }, 250);
  });
  input.addEventListener("keydown", function (event) {
    if (!open) { return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!results.length) { return; }
      event.preventDefault();
      var step = event.key === "ArrowDown" ? 1 : -1;
      active = (active + step + results.length) % results.length;
      render();
    } else if (event.key === "Escape") {
      dismissed = true;
      clear();
    } else if (event.key === "Enter" && results.length) {
      event.preventDefault();
      location.href = results[active < 0 ? 0 : active].url;
    }
    /* Enter otherwise submits the form: Sphinx's own search page. */
  });
  input.addEventListener("blur", function () { setTimeout(function () { dismissed = true; clear(); }, 150); });
})();
