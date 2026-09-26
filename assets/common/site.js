/* Zolo shared page script: language toggle, theme toggle, CTA tracking. */
(function () {
  "use strict";

  var root = document.documentElement;
  // Read at use time: the share page (/place/) switches language in place and fires "zolo:langchange".
  function isHebrew() { return (root.getAttribute("lang") || "").toLowerCase().indexOf("he") === 0; }

  /* ---------- Language toggle ---------- */
  (function () {
    var btn = document.querySelector("[data-lang-btn]");
    if (!btn) return;

    var STORAGE_KEY = "preferredLang";

    function savePreferredLang(lang) {
      try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) {}
    }

    function currentLang() {
      var path = (location.pathname || "/").toLowerCase();
      return (path.indexOf("/he/") === 0 || path === "/he") ? "he" : "en";
    }

    function buildTargetUrl(lang) {
      // "/en/privacy/" -> "/he/privacy/", "/en" -> "/he/", anything else -> "/<lang>/"
      var m = (location.pathname || "/").toLowerCase().match(/^\/(en|he)(\/.*)?$/);
      var rest = (m && m[2]) || "/";
      if (rest.charAt(rest.length - 1) !== "/") rest += "/";
      return "/" + lang + rest;
    }

    var current = currentLang();
    btn.textContent = current === "he" ? "EN" : "עב";
    savePreferredLang(current);

    btn.addEventListener("click", function () {
      var next = currentLang() === "he" ? "en" : "he";
      savePreferredLang(next);
      location.href = buildTargetUrl(next) + (location.search || "") + (location.hash || "");
    });
  })();

  /* ---------- Theme toggle (system | dark | light) ---------- */
  (function () {
    var STORAGE_KEY = "zolo_theme";
    var btn = document.querySelector("[data-theme-btn]");
    var mq = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");

    var ICON = { system: "🖥️", dark: "🌙", light: "☀️" };
    var LABELS = {
      he: { system: "ערכת נושא: מערכת", dark: "ערכת נושא: כהה", light: "ערכת נושא: בהירה" },
      en: { system: "Theme: system", dark: "Theme: dark", light: "Theme: light" }
    };

    function systemPrefersDark() { return !!(mq && mq.matches); }

    function effectiveTheme(mode) {
      if (mode === "dark" || mode === "light") return mode;
      return systemPrefersDark() ? "dark" : "light";
    }

    function setBtn(mode) {
      if (!btn) return;
      var label = LABELS[isHebrew() ? "he" : "en"][mode];
      btn.innerHTML = '<span aria-hidden="true">' + ICON[mode] + "</span>";
      btn.setAttribute("aria-label", label);
      btn.title = label;
    }

    function apply(mode) {
      root.setAttribute("data-theme", mode);
      setBtn(mode);

      var theme = effectiveTheme(mode);

      // <picture data-theme-pic><source media=... srcset=dark><img src=light></picture>
      // In "system" mode the browser follows the OS via the media query itself; a manual
      // choice forces the dark <source> on ("all") or off ("not all").
      var sourceMedia = mode === "system" ? "(prefers-color-scheme: dark)" : (theme === "dark" ? "all" : "not all");
      var sources = document.querySelectorAll("picture[data-theme-pic] > source");
      for (var i = 0; i < sources.length; i++) {
        if (sources[i].getAttribute("media") !== sourceMedia) sources[i].setAttribute("media", sourceMedia);
      }

      // Legacy: <img data-theme-img data-light=... data-dark=...>
      var imgs = document.querySelectorAll("img[data-theme-img]");
      for (var j = 0; j < imgs.length; j++) {
        var next = imgs[j].getAttribute(theme === "dark" ? "data-dark" : "data-light");
        if (next && imgs[j].getAttribute("src") !== next) imgs[j].setAttribute("src", next);
      }

      // Browser UI color (<meta name="theme-color" media=...>): follow the OS in system mode,
      // otherwise force both entries to the chosen theme.
      var metas = document.querySelectorAll('meta[name="theme-color"]');
      for (var m = 0; m < metas.length; m++) {
        if (!metas[m].hasAttribute("data-default")) metas[m].setAttribute("data-default", metas[m].getAttribute("content") || "");
        var wanted = mode === "system" ? metas[m].getAttribute("data-default") : (theme === "dark" ? "#000000" : "#FFFFFF");
        if (metas[m].getAttribute("content") !== wanted) metas[m].setAttribute("content", wanted);
      }

      // Decorative CSS background chosen per theme (not fetched while display:none on mobile)
      var bgs = document.querySelectorAll("[data-hero-bg]");
      for (var k = 0; k < bgs.length; k++) {
        var bg = bgs[k].getAttribute(theme === "dark" ? "data-dark" : "data-light");
        if (bg) bgs[k].style.backgroundImage = 'url("' + bg + '")';
      }
    }

    function getMode() {
      try { return localStorage.getItem(STORAGE_KEY) || "system"; } catch (e) { return "system"; }
    }

    function setMode(mode) {
      try { localStorage.setItem(STORAGE_KEY, mode); } catch (e) {}
      apply(mode);
    }

    function nextMode(mode) {
      if (mode === "system") return "dark";
      if (mode === "dark") return "light";
      return "system";
    }

    apply(getMode());

    if (btn) {
      btn.addEventListener("click", function () { setMode(nextMode(getMode())); });
    }

    function onSystemChange() { if (getMode() === "system") apply("system"); }
    if (mq && mq.addEventListener) mq.addEventListener("change", onSystemChange);
    else if (mq && mq.addListener) mq.addListener(onSystemChange);

    document.addEventListener("zolo:langchange", function () { setBtn(getMode()); });
  })();

  /* ---------- Conversion tracking: <a data-cta="..."> ---------- */
  (function () {
    var ctas = document.querySelectorAll("a[data-cta]");
    if (!ctas.length) return;

    function store(href) {
      if (/play\.google\.com/.test(href)) return "google_play";
      if (/apps\.apple\.com/.test(href)) return "app_store";
      return "other";
    }

    for (var i = 0; i < ctas.length; i++) {
      ctas[i].addEventListener("click", function (ev) {
        var a = ev.currentTarget;
        var href = a.getAttribute("href") || "";
        if (typeof window.gtag !== "function") return;
        window.gtag("event", "install_click", {
          store: store(href),
          placement: a.getAttribute("data-cta") || "unknown",
          language: isHebrew() ? "he" : "en",
          link_url: href,
          transport_type: "beacon"
        });
      });
    }
  })();
})();
