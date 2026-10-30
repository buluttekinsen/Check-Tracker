// Date range picker for the check form's data period.
// Progressive enhancement: the page renders two native date inputs
// (period_start / period_end). This script hides them, turns them into hidden
// inputs with the same names and YYYY-MM-DD values, and shows one range field
// instead. If anything goes wrong before the swap, the date inputs stay.
(() => {
  const startInput = document.querySelector("[data-daterange-start]");
  const tpl = document.getElementById("daterange-template");
  if (!startInput || !tpl) return;
  const endInput = document.getElementById(startInput.dataset.daterangeEnd);
  const checkDateInput = document.getElementById(startInput.dataset.daterangeCheckDate);
  if (!endInput) return;

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
  const CHEVRON = { "-1": "15 18 9 12 15 6", "1": "9 18 15 12 9 6" };

  const pad = (n) => String(n).padStart(2, "0");
  const toIso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const fromIso = (s) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
    if (!m) return null;
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    return toIso(d) === s ? d : null;  // rejects 2026-02-30 etc.
  };
  const display = (d) => (d ? `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}` : "…");
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
  const monthStart = (d) => new Date(d.getFullYear(), d.getMonth(), 1);
  const addMonths = (d, n) => new Date(d.getFullYear(), d.getMonth() + n, 1);
  const today = () => { const n = new Date(); return new Date(n.getFullYear(), n.getMonth(), n.getDate()); };

  // A saved value the picker can't read: keep the plain inputs.
  for (const input of [startInput, endInput]) {
    if (input.value && !fromIso(input.value)) return;
  }

  // ---------------------------------------------------------------- state
  let range = { start: fromIso(startInput.value), end: fromIso(endInput.value) };  // what gets posted
  let anchor = null;  // first click of a selection in progress
  let hover = null;   // day under the pointer while picking the end
  let views = [];     // first day of the month shown in each panel (left before right)

  // ------------------------------------------------------------------ DOM
  const root = tpl.content.firstElementChild.cloneNode(true);
  const field = root.querySelector(".daterange__open");
  const text = root.querySelector(".daterange__text");
  const clearBtn = root.querySelector(".daterange__clear");
  const icon = root.querySelector(".daterange__icon");
  const popover = root.querySelector(".daterange__popover");
  const months = root.querySelector(".daterange__months");

  // Presets count back from the check date (today if none, never later than today).
  function referenceDay() {
    const cd = fromIso(checkDateInput && checkDateInput.value);
    return cd && cd < today() ? cd : today();
  }

  function presetRange(name) {
    const ref = referenceDay();
    switch (name) {
      case "yesterday": return [addDays(ref, -1), addDays(ref, -1)];
      case "last7": return [addDays(ref, -6), ref];
      case "last30": return [addDays(ref, -29), ref];
      case "last-month": return [addMonths(ref, -1), addDays(monthStart(ref), -1)];
    }
    return null;
  }

  function commit(start, end) {
    if (start && end && end < start) [start, end] = [end, start];
    range = { start, end };
    startInput.value = start ? toIso(start) : "";
    endInput.value = end ? toIso(end) : "";
    anchor = hover = null;
    renderField();
  }

  function renderField() {
    const { start, end } = anchor ? { start: anchor, end: null } : range;
    const empty = !start && !end;
    text.textContent = empty ? "Select a date range" : `${display(start)} – ${display(end)}`;
    text.classList.toggle("is-placeholder", empty);
    clearBtn.hidden = empty;
    icon.hidden = !empty;
  }

  // Selection shown in the calendar: the saved range, or the one being picked.
  function shownRange() {
    let start = anchor || range.start;
    let end = anchor ? hover || anchor : range.end;
    if (start && end && end < start) [start, end] = [end, start];
    return { start, end };
  }

  function dayClasses(day, sel) {
    const iso = toIso(day);
    const isStart = sel.start && iso === toIso(sel.start);
    const isEnd = sel.end && iso === toIso(sel.end);
    const cls = [];
    if (isStart) cls.push("is-start");
    if (isEnd) cls.push("is-end");
    if (sel.start && sel.end && day > sel.start && day < sel.end) cls.push("is-in-range");
    if (day > today()) cls.push("is-future");
    return cls;
  }

  function renderMonths() {
    const sel = shownRange();
    months.replaceChildren(...views.map((first, panel) => {
      const month = document.createElement("div");
      month.className = "daterange__month";
      const head = document.createElement("div");
      head.className = "daterange__head";
      const title = document.createElement("span");
      title.className = "daterange__title";
      title.textContent = `${MONTHS[first.getMonth()]} ${first.getFullYear()}`;
      const nav = (step) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "daterange__nav";
        b.dataset.panel = panel;
        b.dataset.step = step;
        b.setAttribute("aria-label", step < 0 ? "Previous month" : "Next month");
        b.innerHTML = `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><polyline points="${CHEVRON[step]}"/></svg>`;
        return b;
      };
      head.append(nav(-1), title, nav(1));

      const grid = document.createElement("div");
      grid.className = "daterange__grid";
      for (const w of WEEKDAYS) {
        const h = document.createElement("span");
        h.className = "daterange__weekday";
        h.textContent = w;
        grid.append(h);
      }
      // Six full weeks, Monday first; days of the neighbouring months in grey.
      const gridStart = addDays(first, -((first.getDay() + 6) % 7));
      for (let i = 0; i < 42; i++) {
        const d = addDays(gridStart, i);
        if (d.getMonth() !== first.getMonth()) {
          const s = document.createElement("span");
          s.className = "daterange__day is-outside" + (d > today() ? " is-future" : "");
          s.innerHTML = `<span>${d.getDate()}</span>`;
          s.setAttribute("aria-hidden", "true");
          grid.append(s);
          continue;
        }
        const b = document.createElement("button");
        b.type = "button";
        b.className = ["daterange__day", ...dayClasses(d, sel)].join(" ");
        b.innerHTML = `<span>${d.getDate()}</span>`;
        b.dataset.date = toIso(d);
        b.setAttribute("aria-label", display(d));
        b.disabled = d > today();
        b.tabIndex = -1;
        grid.append(b);
      }
      month.append(head, grid);
      return month;
    }));
    // One day is tabbable (roving focus): the range start, else the last enabled day shown.
    const days = months.querySelectorAll("button.daterange__day:not(:disabled)");
    const focusable = months.querySelector("button.is-start") || days[days.length - 1];
    if (focusable) focusable.tabIndex = 0;
  }

  // Only restyle existing day buttons (keeps focus; used for the hover preview).
  function paintDays() {
    const sel = shownRange();
    months.querySelectorAll("button.daterange__day").forEach((b) => {
      b.className = ["daterange__day", ...dayClasses(fromIso(b.dataset.date), sel)].join(" ");
    });
  }

  // Show a range: its end month on the right, its start month on the left
  // (or simply the month before, when it fits in one month).
  function showRange(start, end) {
    const right = monthStart(end || start || referenceDay());
    const left = start && monthStart(start) < right ? monthStart(start) : addMonths(right, -1);
    views = [left, right];
  }

  function open() {
    showRange(range.start, range.end);
    renderMonths();
    popover.hidden = false;
    field.setAttribute("aria-expanded", "true");
    // Keep the popover inside the window: shift it left if it sticks out.
    popover.style.left = "0px";
    const margin = 8;
    const r = popover.getBoundingClientRect();
    const overflow = r.right - (document.documentElement.clientWidth - margin);
    if (overflow > 0) popover.style.left = `${-Math.min(overflow, r.left - margin)}px`;
  }

  function close(focusField) {
    if (popover.hidden) return;
    // A half-finished selection is dropped; the saved range stays as it was.
    anchor = hover = null;
    renderField();
    popover.hidden = true;
    field.setAttribute("aria-expanded", "false");
    if (focusField) field.focus();
  }

  function focusDay(day) {
    if (day > today()) return;
    let b = months.querySelector(`button[data-date="${toIso(day)}"]`);
    if (!b) {
      // Bring the day's month into view: in the right panel if it lies after
      // it, otherwise in the left panel.
      const m = monthStart(day);
      if (m > views[1]) views[1] = m;
      else views[0] = m;
      renderMonths();
      b = months.querySelector(`button[data-date="${toIso(day)}"]`);
    }
    if (!b) return;
    months.querySelectorAll("button.daterange__day").forEach((x) => { x.tabIndex = -1; });
    b.tabIndex = 0;
    b.focus();
  }

  // ------------------------------------------------------------- events
  field.addEventListener("click", () => (popover.hidden ? open() : close(false)));

  clearBtn.addEventListener("click", () => {
    commit(null, null);
    close(false);
    field.focus();
  });

  popover.addEventListener("click", (event) => {
    const day = event.target.closest("button.daterange__day");
    if (day) {
      const d = fromIso(day.dataset.date);
      if (!anchor) {
        anchor = d;
        hover = null;
        renderField();
        paintDays();
      } else {
        commit(anchor, d);  // swaps if the second day is before the first
        paintDays();
      }
      return;
    }
    const nav = event.target.closest(".daterange__nav");
    if (nav) {
      const panel = +nav.dataset.panel;
      views[panel] = addMonths(views[panel], +nav.dataset.step);
      // Panels never meet or cross: pushing one into the other moves that one along.
      if (views[0] >= views[1]) {
        if (panel === 0) views[1] = addMonths(views[0], 1);
        else views[0] = addMonths(views[1], -1);
      }
      renderMonths();
      months.querySelectorAll(".daterange__nav")[panel * 2 + (+nav.dataset.step > 0 ? 1 : 0)].focus();
      return;
    }
    const preset = event.target.closest("[data-preset]");
    if (preset) {
      const [start, end] = presetRange(preset.dataset.preset);
      commit(start, end);
      showRange(start, end);
      renderMonths();
      return;
    }
    if (event.target.closest("[data-ok]")) {
      if (anchor) commit(anchor, anchor);  // only one day picked: a one-day range
      close(true);
    }
  });

  months.addEventListener("mouseover", (event) => {
    const day = event.target.closest("button.daterange__day");
    if (!anchor || !day || day.disabled) return;
    hover = fromIso(day.dataset.date);
    paintDays();
  });

  popover.addEventListener("keydown", (event) => {
    const day = event.target.closest("button.daterange__day");
    const steps = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    if (day && event.key in steps) {
      event.preventDefault();
      const d = addDays(fromIso(day.dataset.date), steps[event.key]);
      if (d > today()) return;
      if (anchor) hover = d;
      focusDay(d);
      paintDays();
    }
  });

  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !popover.hidden) {
      event.preventDefault();
      close(true);
    }
  });

  document.addEventListener("mousedown", (event) => {
    if (!root.contains(event.target)) close(false);
  });
  root.addEventListener("focusout", (event) => {
    if (event.relatedTarget && !root.contains(event.relatedTarget)) close(false);
  });

  // --------------------------------------------------------------- swap in
  const fields = document.querySelectorAll("[data-period-field]");
  renderField();
  fields[0].before(root);
  for (const input of [startInput, endInput]) input.type = "hidden";
  fields.forEach((f) => { f.hidden = true; });
})();
