/*
 * Automation support script for BrowserController.
 *
 * This is injected into an ISOLATED JavaScript world (ApplicationWorld) at
 * document creation, on every frame. Two consequences matter:
 *
 *   1. The page cannot see, call, or tamper with anything defined here - our
 *      globals are not the page's globals. A hostile page cannot forge a
 *      snapshot or redirect a click.
 *   2. We still share the DOM, so we can read structure and act on elements.
 *
 * We hold direct element references in a registry rather than stamping
 * data-* attributes onto the page. Nothing we do is observable in the page's
 * own DOM, and a reference always resolves to the exact node we captured -
 * never to whatever currently matches a re-derived selector.
 *
 * This file is an implementation detail of BrowserController. Callers get
 * semantic operations; they never see or supply JavaScript.
 */
(function () {
  "use strict";
  if (window.__pb) { return; }

  var state = {
    // Regenerated every time this script runs, i.e. once per document. A
    // snapshot carrying a different docId belongs to a page that is gone.
    docId: "d" + Math.random().toString(36).slice(2) + Date.now().toString(36),
    snapshots: new Map(),
    nextSnapshot: 0,
    domRevision: 0,
    maxSnapshots: 8
  };

  // Any DOM change bumps a revision counter. The controller reads it before
  // and after an action to tell "the click changed the page" from "the click
  // did nothing", without needing a screenshot or a diff.
  try {
    new MutationObserver(function () { state.domRevision++; }).observe(
      document.documentElement || document,
      { childList: true, subtree: true, attributes: true, characterData: true }
    );
  } catch (e) { /* observer unavailable: revision simply stays 0 */ }

  var INTERACTIVE = 'a[href], area[href], button, input, select, textarea,' +
    ' summary, [contenteditable=""], [contenteditable="true"], [tabindex]:not([tabindex="-1"]),' +
    ' [role="button"], [role="link"], [role="textbox"], [role="searchbox"],' +
    ' [role="checkbox"], [role="radio"], [role="combobox"], [role="switch"],' +
    ' [role="menuitem"], [role="tab"], [role="option"], [role="slider"]';

  function text(value, limit) {
    return (value == null ? "" : String(value)).replace(/\s+/g, " ").trim().slice(0, limit || 300);
  }

  /* ---------------------------------------------------------------- roles */
  function roleOf(el) {
    var explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) { return explicit.trim().toLowerCase(); }
    var tag = el.tagName.toLowerCase();
    if (tag === "a" || tag === "area") { return el.hasAttribute("href") ? "link" : "generic"; }
    if (tag === "button") { return "button"; }
    if (tag === "select") { return el.multiple ? "listbox" : "combobox"; }
    if (tag === "textarea") { return "textarea"; }
    if (tag === "summary") { return "button"; }
    if (tag === "img") { return "image"; }
    if (/^h[1-6]$/.test(tag)) { return "heading"; }
    if (tag === "form") { return "form"; }
    if (tag === "input") {
      var type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "checkbox") { return "checkbox"; }
      if (type === "radio") { return "radio"; }
      if (type === "submit" || type === "button" || type === "reset" || type === "image") { return "button"; }
      if (type === "search") { return "searchbox"; }
      if (type === "range") { return "slider"; }
      if (type === "hidden") { return "hidden"; }
      if (type === "file") { return "filepicker"; }
      // password / email / tel / url / number / date all behave as text entry;
      // the specific type is reported separately in `input_type`.
      return "textbox";
    }
    if (el.isContentEditable) { return "textbox"; }
    return "generic";
  }

  /* Accessible name, following the practical part of accname: aria-labelledby,
     aria-label, associated <label>, then content or a sensible attribute. */
  function accessibleName(el) {
    var byId = el.getAttribute && el.getAttribute("aria-labelledby");
    if (byId) {
      var parts = byId.split(/\s+/).map(function (id) {
        var node = document.getElementById(id);
        return node ? node.textContent : "";
      }).join(" ");
      if (text(parts)) { return text(parts); }
    }
    var label = el.getAttribute && el.getAttribute("aria-label");
    if (text(label)) { return text(label); }

    if (el.labels && el.labels.length) {
      var fromLabel = Array.prototype.map.call(el.labels, function (l) { return l.textContent; }).join(" ");
      if (text(fromLabel)) { return text(fromLabel); }
    }
    var tag = el.tagName.toLowerCase();
    if (tag === "input") {
      var type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "submit" || type === "button" || type === "reset") {
        if (text(el.value)) { return text(el.value); }
      }
      if (text(el.getAttribute("placeholder"))) { return text(el.getAttribute("placeholder")); }
      if (text(el.getAttribute("name"))) { return text(el.getAttribute("name")); }
    }
    if (tag === "img") {
      if (text(el.getAttribute("alt"))) { return text(el.getAttribute("alt")); }
    }
    if (text(el.innerText || el.textContent)) { return text(el.innerText || el.textContent); }
    if (text(el.getAttribute && el.getAttribute("title"))) { return text(el.getAttribute("title")); }
    if (text(el.getAttribute && el.getAttribute("placeholder"))) { return text(el.getAttribute("placeholder")); }
    if (text(el.getAttribute && el.getAttribute("name"))) { return text(el.getAttribute("name")); }
    return "";
  }

  function isVisible(el) {
    if (!el.isConnected) { return false; }
    var style = window.getComputedStyle(el);
    if (!style || style.visibility === "hidden" || style.display === "none") { return false; }
    if (style.opacity !== "" && parseFloat(style.opacity) === 0) { return false; }
    if (el.hasAttribute && el.hasAttribute("hidden")) { return false; }
    if (el.getAttribute && el.getAttribute("aria-hidden") === "true") { return false; }
    var rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function inViewport(el) {
    var r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 &&
           r.top < (window.innerHeight || 0) && r.left < (window.innerWidth || 0);
  }

  function isDisabled(el) {
    if (el.disabled === true) { return true; }
    if (el.getAttribute && el.getAttribute("aria-disabled") === "true") { return true; }
    return !!(el.closest && el.closest("fieldset[disabled]"));
  }

  /* A stable-enough identity check. Deliberately excludes the *value* of an
     input (typing must not invalidate a reference) but includes tag, role and
     accessible name, so a recycled node in a virtualised list - the classic
     way automation clicks the wrong thing - is caught. */
  function fingerprint(el) {
    return [el.tagName.toLowerCase(), roleOf(el), accessibleName(el).slice(0, 80),
            (el.getAttribute && el.getAttribute("type")) || ""].join("");
  }

  function describe(el, ref, formIndex) {
    var tag = el.tagName.toLowerCase();
    var role = roleOf(el);
    var item = {
      ref: ref,
      role: role,
      name: accessibleName(el),
      tag: tag,
      visible: isVisible(el),
      in_viewport: false,
      disabled: isDisabled(el)
    };
    if (item.visible) { item.in_viewport = inViewport(el); }
    if (formIndex !== undefined && formIndex !== null) { item.form = formIndex; }

    if (tag === "a" || tag === "area") {
      if (el.href) { item.href = String(el.href).slice(0, 2048); }
      if (el.target) { item.target = el.target; }
      if (el.hasAttribute("download")) { item.download = true; }
    }
    if (tag === "input") {
      item.input_type = (el.getAttribute("type") || "text").toLowerCase();
      if (el.autocomplete) { item.autocomplete = el.autocomplete; }
    }
    if (tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable) {
      if (el.placeholder) { item.placeholder = text(el.placeholder, 120); }
      if (el.required) { item.required = true; }
      if (el.readOnly) { item.readonly = true; }
      if (el.name) { item.field_name = text(el.name, 80); }
      if (el.maxLength !== undefined && el.maxLength > 0) { item.max_length = el.maxLength; }
      // A password value is never reported, not even truncated.
      if (item.input_type === "password") {
        item.value = el.value ? new Array(Math.min(el.value.length, 12) + 1).join("*") : "";
        item.secret = true;
      } else if (role === "checkbox" || role === "radio" || role === "switch") {
        item.checked = !!el.checked;
      } else if (tag === "select") {
        item.value = text(el.value, 120);
        item.options = Array.prototype.slice.call(el.options, 0, 50).map(function (o) {
          return { label: text(o.label || o.text, 80), value: text(o.value, 80), selected: !!o.selected };
        });
      } else if (el.isContentEditable) {
        item.value = text(el.textContent, 200);
      } else {
        item.value = text(el.value, 200);
      }
    }
    if (role === "heading") {
      item.level = parseInt(el.getAttribute("aria-level") || tag.slice(1), 10) || 2;
    }
    if (el.getAttribute && el.getAttribute("aria-expanded")) {
      item.expanded = el.getAttribute("aria-expanded") === "true";
    }
    return item;
  }

  /* -------------------------------------------------------------- capture */
  function capture(options) {
    options = options || {};
    var maxElements = options.max_elements || 300;
    var maxText = options.max_text || 20000;
    var includeInvisible = !!options.include_invisible;

    var snapshotId = "s" + (++state.nextSnapshot);
    var formNodes = Array.prototype.slice.call(document.forms, 0, 50);
    var elements = formNodes.slice();
    var fingerprints = formNodes.map(fingerprint);
    var formCount = formNodes.length;
    var reported = [];

    var forms = formNodes.map(function (form, i) {
      return {
        ref: snapshotId + ":f" + i,
        name: text(form.getAttribute("name") || form.getAttribute("id") || form.getAttribute("aria-label"), 80),
        action: form.action ? String(form.action).slice(0, 2048) : "",
        method: (form.method || "get").toLowerCase(),
        field_count: form.elements ? form.elements.length : 0
      };
    });
    var formIndexOf = function (el) {
      var owner = el.form || (el.closest ? el.closest("form") : null);
      if (!owner) { return null; }
      var i = formNodes.indexOf(owner);
      return i === -1 ? null : i;
    };

    var candidates = document.querySelectorAll(INTERACTIVE);
    var truncated = false;
    for (var i = 0; i < candidates.length; i++) {
      if (reported.length >= maxElements) { truncated = true; break; }
      var el = candidates[i];
      var role = roleOf(el);
      if (role === "hidden") { continue; }
      if (!includeInvisible && !isVisible(el)) { continue; }
      var ref = snapshotId + ":e" + reported.length;
      elements.push(el);
      fingerprints.push(fingerprint(el));
      reported.push(describe(el, ref, formIndexOf(el)));
    }

    var headings = Array.prototype.slice.call(
      document.querySelectorAll("h1, h2, h3, h4, h5, h6, [role='heading']"), 0, 60
    ).filter(isVisible).map(function (h) {
      return {
        level: parseInt(h.getAttribute("aria-level") || h.tagName.slice(1), 10) || 2,
        text: text(h.innerText || h.textContent, 200)
      };
    });

    var body = document.body;
    var pageText = text(body ? body.innerText : "", maxText + 1);
    var textTruncated = pageText.length > maxText;

    state.snapshots.set(snapshotId, {
      docId: state.docId, elements: elements, fingerprints: fingerprints, formCount: formCount
    });
    // Bound memory: a long-lived page could otherwise accumulate snapshots and
    // pin detached DOM nodes alive.
    while (state.snapshots.size > state.maxSnapshots) {
      state.snapshots.delete(state.snapshots.keys().next().value);
    }

    return {
      snapshot_id: snapshotId,
      doc_id: state.docId,
      dom_revision: state.domRevision,
      url: location.href,
      title: document.title || "",
      lang: document.documentElement.getAttribute("lang") || "",
      headings: headings,
      forms: forms,
      elements: reported,
      element_count: reported.length,
      elements_truncated: truncated,
      text: pageText.slice(0, maxText),
      text_truncated: textTruncated,
      scroll_y: Math.round(window.scrollY),
      scroll_height: Math.round(document.documentElement.scrollHeight),
      viewport_height: Math.round(window.innerHeight),
      viewport_width: Math.round(window.innerWidth),
      at_bottom: (window.scrollY + window.innerHeight) >= (document.documentElement.scrollHeight - 2)
    };
  }

  /* -------------------------------------------------------------- resolve */
  /* Every failure mode is a distinct status so the controller can turn it into
     a specific, actionable error rather than a generic "click failed". */
  function resolve(ref) {
    if (typeof ref !== "string" || ref.indexOf(":") === -1) {
      return { status: "invalid_ref" };
    }
    var parts = ref.split(":");
    var snapshotId = parts[0];
    var index = parts[1];
    var snapshot = state.snapshots.get(snapshotId);
    if (!snapshot) { return { status: "unknown_snapshot" }; }
    if (snapshot.docId !== state.docId) { return { status: "document_changed" }; }

    var offset;
    var n = parseInt(index.slice(1), 10);
    if (isNaN(n) || n < 0) { return { status: "invalid_ref" }; }
    if (index.charAt(0) === "f") {
      if (n >= snapshot.formCount) { return { status: "unknown_ref" }; }
      offset = n;
    } else if (index.charAt(0) === "e") {
      offset = snapshot.formCount + n;
    } else {
      return { status: "invalid_ref" };
    }
    if (!(offset >= 0 && offset < snapshot.elements.length)) {
      return { status: "unknown_ref" };
    }
    var el = snapshot.elements[offset];
    if (!el || !el.isConnected) { return { status: "detached" }; }
    if (fingerprint(el) !== snapshot.fingerprints[offset]) { return { status: "mutated" }; }
    return { status: "ok", el: el, offset: offset };
  }

  function targetInfo(el) {
    return { role: roleOf(el), name: accessibleName(el), tag: el.tagName.toLowerCase() };
  }

  function guard(el, need) {
    if (need.visible && !isVisible(el)) { return "not_visible"; }
    if (need.enabled && isDisabled(el)) { return "disabled"; }
    return null;
  }

  /* --------------------------------------------------------------- action */
  function act(request) {
    var op = request.op;

    if (op === "scroll") {
      var before = window.scrollY;
      if (request.direction === "top") { window.scrollTo(0, 0); }
      else if (request.direction === "bottom") { window.scrollTo(0, document.documentElement.scrollHeight); }
      else {
        var step = request.amount || Math.round(window.innerHeight * 0.85);
        window.scrollBy(0, request.direction === "up" ? -step : step);
      }
      return { status: "ok", scroll_before: Math.round(before), scroll_after: Math.round(window.scrollY),
               scroll_height: Math.round(document.documentElement.scrollHeight),
               dom_revision: state.domRevision };
    }

    var r = resolve(request.ref);
    if (r.status !== "ok") { return r; }
    var el = r.el;
    var info = targetInfo(el);

    if (op === "scroll_to") {
      el.scrollIntoView({ block: "center", inline: "nearest" });
      return { status: "ok", target: info, dom_revision: state.domRevision };
    }

    if (op === "inspect") {
      return { status: "ok", target: info, element: describe(el, request.ref, null),
               dom_revision: state.domRevision };
    }

    if (op === "click") {
      var problem = guard(el, { visible: true, enabled: true });
      if (problem) { return { status: problem, target: info }; }
      el.scrollIntoView({ block: "center", inline: "nearest" });
      el.click();
      return { status: "ok", target: info, dom_revision: state.domRevision };
    }

    if (op === "type") {
      var bad = guard(el, { visible: true, enabled: true });
      if (bad) { return { status: bad, target: info }; }
      var editable = el.isContentEditable ||
        ["input", "textarea"].indexOf(el.tagName.toLowerCase()) !== -1;
      if (!editable) { return { status: "not_editable", target: info }; }
      if (el.readOnly) { return { status: "readonly", target: info }; }
      el.focus();
      var value = request.text == null ? "" : String(request.text);
      if (request.append) {
        value = (el.isContentEditable ? el.textContent : el.value) + value;
      }
      if (el.isContentEditable) { el.textContent = value; }
      else {
        // Bypass React's cached value setter so frameworks see the change.
        var proto = el.tagName.toLowerCase() === "textarea"
          ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        var setter = Object.getOwnPropertyDescriptor(proto, "value");
        if (setter && setter.set) { setter.set.call(el, value); } else { el.value = value; }
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { status: "ok", target: info, dom_revision: state.domRevision };
    }

    if (op === "set_checked") {
      var blocked = guard(el, { visible: true, enabled: true });
      if (blocked) { return { status: blocked, target: info }; }
      var kind = (el.getAttribute("type") || "").toLowerCase();
      var aria = el.getAttribute("role");
      if (kind !== "checkbox" && kind !== "radio" && aria !== "checkbox" && aria !== "switch") {
        return { status: "not_checkable", target: info };
      }
      if (!!el.checked !== !!request.checked) { el.click(); }
      return { status: "ok", target: info, checked: !!el.checked, dom_revision: state.domRevision };
    }

    if (op === "select_option") {
      var stop = guard(el, { visible: true, enabled: true });
      if (stop) { return { status: stop, target: info }; }
      if (el.tagName.toLowerCase() !== "select") { return { status: "not_selectable", target: info }; }
      var wanted = String(request.value == null ? "" : request.value);
      var matched = false;
      for (var j = 0; j < el.options.length; j++) {
        var opt = el.options[j];
        if (opt.value === wanted || text(opt.label || opt.text) === wanted) {
          el.selectedIndex = j; matched = true; break;
        }
      }
      if (!matched) { return { status: "option_not_found", target: info }; }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { status: "ok", target: info, value: el.value, dom_revision: state.domRevision };
    }

    if (op === "submit") {
      var form = el.tagName.toLowerCase() === "form" ? el : (el.form || (el.closest && el.closest("form")));
      if (!form) { return { status: "no_form", target: info }; }
      if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
      return { status: "ok", target: info, dom_revision: state.domRevision };
    }

    if (op === "focus") { el.focus(); return { status: "ok", target: info }; }

    return { status: "unknown_op" };
  }

  /* Cheap poll used by wait_for_*: no snapshot is created, nothing is stored. */
  function probe(query) {
    query = query || {};
    var matches = 0, sample = null;
    if (query.text_contains) {
      var body = (document.body && document.body.innerText) || "";
      if (body.toLowerCase().indexOf(String(query.text_contains).toLowerCase()) !== -1) {
        matches = 1;
      }
    } else {
      var nodes = document.querySelectorAll(INTERACTIVE);
      for (var i = 0; i < nodes.length && matches < 200; i++) {
        var el = nodes[i];
        if (query.role && roleOf(el) !== query.role) { continue; }
        if (query.visible !== false && !isVisible(el)) { continue; }
        if (query.name_contains) {
          var name = accessibleName(el).toLowerCase();
          if (name.indexOf(String(query.name_contains).toLowerCase()) === -1) { continue; }
        }
        matches++;
        if (!sample) { sample = { role: roleOf(el), name: accessibleName(el) }; }
      }
    }
    return {
      matches: matches, sample: sample, dom_revision: state.domRevision,
      doc_id: state.docId, url: location.href, title: document.title || "",
      ready_state: document.readyState
    };
  }

  function status() {
    return {
      doc_id: state.docId, dom_revision: state.domRevision, url: location.href,
      title: document.title || "", ready_state: document.readyState,
      scroll_y: Math.round(window.scrollY),
      scroll_height: Math.round(document.documentElement.scrollHeight),
      snapshots: state.snapshots.size
    };
  }

  window.__pb = { capture: capture, act: act, probe: probe, status: status };
})();
