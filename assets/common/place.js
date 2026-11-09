/* Zolo share-link fallback (/place/). Loaded synchronously in <head> so the right language and
 * message variant are chosen before first paint.
 *
 * Contract: docs/share-link-contract.md in the zolo-flutter repo (owned by the app)
 *   https://zolo-deals.com/place/?id=<placeId>[&deal=<dealKey>]
 *   id   ^[A-Za-z0-9_-]{1,64}$, exactly once      deal ^[0-9a-f]{8}$, at most once
 *   unknown parameters ignored; malformed percent-encoding, a repeated id/deal or a URL longer than
 *   2048 characters make the whole link invalid; the fragment is ignored.
 *
 * Every URL component is untrusted. Nothing read from the URL is ever written into the page: this
 * script only sets data-share / data-lang / lang / dir on <html> and CSS shows the matching static
 * copy. The one derived value is the Android "Open in Zolo" intent href, built from the validated
 * id/deal only, with a fixed host, package and Play fallback.
 */
(function () {
  "use strict";

  var ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
  var DEAL_RE = /^[0-9a-f]{8}$/;
  var MAX_URL_LENGTH = 2048;
  var SHARE_PATH = "/place/";
  var PACKAGE = "com.zolo.deals";
  var PLAY_URL = "https://play.google.com/store/apps/details?id=com.zolo.deals";
  var TITLE = { en: "Shared with you on Zolo", he: "שיתפו אתכם בזולו" };

  var root = document.documentElement;

  /* ---------- Parse and validate the link ---------- */
  function decode(s) {
    return decodeURIComponent(s.replace(/\+/g, " "));   // throws URIError on malformed %-encoding
  }

  function parse() {
    if (location.pathname !== SHARE_PATH) return null;
    if (location.href.length > MAX_URL_LENGTH) return null;
    var query = location.search.replace(/^\?/, "");
    var ids = [];
    var deals = [];
    var pairs = query ? query.split("&") : [];
    for (var i = 0; i < pairs.length; i++) {
      if (!pairs[i]) continue;
      var eq = pairs[i].indexOf("=");
      var key, value;
      try {
        key = decode(eq < 0 ? pairs[i] : pairs[i].slice(0, eq));
        value = decode(eq < 0 ? "" : pairs[i].slice(eq + 1));
      } catch (e) {
        return null;
      }
      if (key === "id") ids.push(value);
      else if (key === "deal") deals.push(value);
    }
    if (ids.length !== 1 || !ID_RE.test(ids[0])) return null;
    if (deals.length > 1 || (deals.length === 1 && !DEAL_RE.test(deals[0]))) return null;
    return { id: ids[0], deal: deals.length ? deals[0] : null };
  }

  var link = null;
  try { link = parse(); } catch (e) { link = null; }
  root.setAttribute("data-share", link ? (link.deal ? "deal" : "place") : "invalid");

  /* ---------- Language: same preference order as the root redirect pages ---------- */
  var STORAGE_KEY = "preferredLang";

  function initialLang() {
    try {
      var saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "he" || saved === "en") return saved;
    } catch (e) {}
    var langs = navigator.languages || [navigator.language || "en"];
    for (var i = 0; i < langs.length; i++) {
      var code = String(langs[i] || "").toLowerCase();
      if (code === "he" || code.indexOf("he-") === 0) return "he";
    }
    return "en";
  }

  function applyLang(lang) {
    root.setAttribute("lang", lang);
    root.setAttribute("dir", lang === "he" ? "rtl" : "ltr");
    root.setAttribute("data-lang", lang);
    document.title = TITLE[lang];
  }

  applyLang(initialLang());

  /* ---------- After parsing: language toggle and the Android "Open in Zolo" button ---------- */
  function intentUrl(l) {
    // Chrome/Android: open com.zolo.deals with the canonical https URL, or go to Google Play.
    var query = "?id=" + l.id + (l.deal ? "&deal=" + l.deal : "");
    return "intent://zolo-deals.com" + SHARE_PATH + query +
      "#Intent;scheme=https;action=android.intent.action.VIEW;package=" + PACKAGE +
      ";S.browser_fallback_url=" + encodeURIComponent(PLAY_URL) + ";end";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var toggles = document.querySelectorAll("[data-share-lang]");
    for (var i = 0; i < toggles.length; i++) {
      toggles[i].addEventListener("click", function (ev) {
        var next = ev.currentTarget.getAttribute("data-share-lang") === "he" ? "he" : "en";
        try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
        applyLang(next);
        document.dispatchEvent(new Event("zolo:langchange"));
        // keep keyboard focus on the (now visible) toggle of the new language
        var other = document.querySelector('[data-l10n="' + next + '"] [data-share-lang], [data-l10n="' + next + '"][data-share-lang]');
        if (other) other.focus();
      });
    }

    if (!link || !/Android/i.test(navigator.userAgent || "")) return;
    var buttons = document.querySelectorAll("[data-open-app]");
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].setAttribute("href", intentUrl(link));
      buttons[j].hidden = false;
    }
  });
})();
