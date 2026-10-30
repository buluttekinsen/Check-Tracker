// Metric editor: add/remove rows and fill in the unit last used for a key.
// The form works without this script (blank rows are always rendered).
document.addEventListener("click", (event) => {
  const add = event.target.closest("[data-add-metric]");
  if (add) {
    const rows = document.getElementById("metric-rows");
    rows.append(document.getElementById("metric-row-template").content.cloneNode(true));
    rows.lastElementChild.querySelector("input").focus();
    return;
  }
  const remove = event.target.closest("[data-remove-row]");
  if (remove) remove.closest("tr").remove();
});

document.addEventListener("change", (event) => {
  if (!event.target.matches("input[name=metric_key]")) return;
  const rows = document.getElementById("metric-rows");
  const unitByKey = JSON.parse(rows.dataset.units || "{}");
  const unitInput = event.target.closest("tr").querySelector("input[name=metric_unit]");
  const unit = unitByKey[event.target.value.trim().toLowerCase()];
  if (unit && !unitInput.value) unitInput.value = unit;
});
