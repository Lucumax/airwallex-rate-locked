// Run by tests/test_theme.py with Node. Loads web/theme.js into a stub window and document and checks the toggle.
const fs = require("fs"), vm = require("vm"), path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "src", "rate_locked", "web", "theme.js"), "utf8");

function page({ stored = null, systemDark = false, search = "", blocked = false } = {}) {
  const attrs = {}, battrs = {}, store = stored ? { rl_theme: stored } : {}, listeners = {};
  const doc = {
    readyState: "complete",
    documentElement: { getAttribute: (k) => (k in attrs ? attrs[k] : null), setAttribute: (k, v) => { attrs[k] = v; } },
    getElementById: (id) => (id === "theme-toggle" ? { setAttribute: (k, v) => { battrs[k] = v; }, addEventListener: (ev, f) => { listeners[ev] = f; } } : null),
    addEventListener() {},
  };
  const win = {
    URLSearchParams, location: { search },
    matchMedia: () => ({ matches: systemDark, addEventListener() {} }),
    localStorage: blocked ? { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } }
      : { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } },
  };
  vm.runInNewContext(src, { window: win, document: doc, URLSearchParams });
  return { win, attrs, battrs, store, click: () => listeners.click() };
}

const out = {};
let p = page();
out.default_has_no_override = p.attrs["data-theme"] === undefined && p.win.RateLockedTheme.current() === "light" && p.battrs["aria-pressed"] === "false";
p.click();
out.click_goes_dark_and_is_stored = p.attrs["data-theme"] === "dark" && p.store.rl_theme === "dark" && p.battrs["aria-pressed"] === "true";
p.click();
out.click_again_goes_light = p.attrs["data-theme"] === "light" && p.store.rl_theme === "light" && p.battrs["aria-pressed"] === "false";
p = page({ stored: "dark" });
out.stored_choice_is_applied_on_load = p.attrs["data-theme"] === "dark" && p.battrs["aria-pressed"] === "true";
p = page({ systemDark: true });
out.system_dark_reads_dark_and_toggle_goes_light = p.win.RateLockedTheme.current() === "dark" && (p.click(), p.attrs["data-theme"] === "light" && p.store.rl_theme === "light");
p = page({ stored: "dark", search: "?theme=light" });
out.query_parameter_wins_and_is_not_stored = p.attrs["data-theme"] === "light" && p.store.rl_theme === "dark";
p = page({ stored: "banana" });
out.garbage_in_storage_is_ignored = p.attrs["data-theme"] === undefined;
p = page({ blocked: true });
out.blocked_storage_still_toggles = (p.click(), p.attrs["data-theme"] === "dark");
console.log(JSON.stringify(out));
