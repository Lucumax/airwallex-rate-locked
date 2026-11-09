"use strict";
/* Light/dark theme for the console and the approver page. Light is the default look; the dark token set in app.css applies
   when the system asks for it (prefers-color-scheme) or when the person has used the toggle. The choice is kept in
   localStorage only (this browser, this origin), and the page works if storage is blocked. ?theme=dark or ?theme=light
   forces a theme for one page view and is never stored (used for screenshots). */
(function (win, doc) {
  var KEY = "rl_theme";
  var root = doc.documentElement;

  function valid(v) { return v === "dark" || v === "light" ? v : null; }
  function stored() { try { return valid(win.localStorage.getItem(KEY)); } catch (e) { return null; } }
  function remember(v) { try { win.localStorage.setItem(KEY, v); } catch (e) { /* private window or blocked storage */ } }
  function system() {
    return win.matchMedia && win.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function current() { return valid(root.getAttribute("data-theme")) || system(); }
  function paint() {
    var b = doc.getElementById("theme-toggle");
    if (!b) return;
    var dark = current() === "dark";
    b.setAttribute("aria-pressed", dark ? "true" : "false");
    b.setAttribute("title", dark ? "Switch to the light look" : "Switch to the dark look");
  }
  function apply(v) { root.setAttribute("data-theme", v); paint(); }
  function toggle() {
    var next = current() === "dark" ? "light" : "dark";
    apply(next);
    remember(next);
    return next;
  }

  var forced = null;
  try { forced = valid(new win.URLSearchParams(win.location.search).get("theme")); } catch (e) { forced = null; }
  var first = forced || stored();
  if (first) root.setAttribute("data-theme", first);

  function wire() {
    var b = doc.getElementById("theme-toggle");
    if (b) b.addEventListener("click", toggle);
    paint();
    if (win.matchMedia) {
      var m = win.matchMedia("(prefers-color-scheme: dark)");
      if (m && m.addEventListener) m.addEventListener("change", paint);
    }
  }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", wire); else wire();

  win.RateLockedTheme = { toggle: toggle, current: current, key: KEY };
})(window, document);
