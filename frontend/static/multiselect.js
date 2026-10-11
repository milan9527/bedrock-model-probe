const widgets = new WeakMap();

export function updateMultiSelect(select) {
  const widget = widgets.get(select);
  if (!widget) return;
  const options = [...select.options];
  const selected = [...select.selectedOptions];
  const focusedValue = widget.options.contains(document.activeElement) ? document.activeElement.value : null;
  const all = selected.length === options.length && !select.dataset.required;
  widget.summary.textContent = !options.length && select.dataset.empty ? select.dataset.empty
    : !selected.length || all
    ? select.dataset.all || "All"
    : selected.length === 1 ? selected[0].text : `${selected.length} selected`;
  widget.summary.title = selected.map(option => option.text).join(", ");
  widget.options.replaceChildren();
  for (const option of options) {
    const label = document.createElement("label");
    label.className = "multi-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = option.selected;
    checkbox.value = option.value;
    checkbox.setAttribute("aria-label", option.text);
    checkbox.addEventListener("change", event => {
      event.stopPropagation();
      if (select.dataset.required && !checkbox.checked && select.selectedOptions.length === 1) {
        checkbox.checked = true;
        return;
      }
      option.selected = checkbox.checked;
      updateMultiSelect(select);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const text = document.createElement("span");
    text.textContent = option.text;
    label.append(checkbox, text);
    label.hidden = !option.text.toLowerCase().includes(widget.search.value.toLowerCase());
    widget.options.append(label);
  }
  if (focusedValue !== null) {
    [...widget.options.querySelectorAll("input")].find(input => input.value === focusedValue)?.focus();
  }
}

export function initMultiSelects() {
  for (const select of document.querySelectorAll("select[multiple]")) {
    select.hidden = true;
    const details = document.createElement("details");
    details.className = "multi-select";
    const summary = document.createElement("summary");
    summary.setAttribute("aria-label", select.getAttribute("aria-label") || select.name);
    const menu = document.createElement("div");
    menu.className = "multi-menu";
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = "Search options";
    search.setAttribute("aria-label", "Search " + (select.getAttribute("aria-label") || select.name));
    search.addEventListener("input", () => updateMultiSelect(select));
    search.addEventListener("change", event => event.stopPropagation());
    const actions = document.createElement("div");
    actions.className = "multi-actions";
    for (const action of ["All", select.dataset.required ? "Reset" : "Clear"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = action;
      button.addEventListener("click", () => {
        [...select.options].forEach((option, index) => {
          option.selected = action === "All" || (action === "Reset" && index === 0);
        });
        updateMultiSelect(select);
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      actions.append(button);
    }
    const options = document.createElement("div");
    options.className = "multi-options";
    menu.append(search, actions, options);
    details.append(summary, menu);
    details.addEventListener("toggle", () => {
      if (details.open) {
        document.querySelectorAll(".multi-select[open]").forEach(other => {
          if (other !== details) other.open = false;
        });
      }
    });
    details.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        details.open = false;
        summary.focus();
      }
    });
    select.after(details);
    widgets.set(select, { summary, search, options });
    updateMultiSelect(select);
  }
  document.addEventListener("click", event => {
    document.querySelectorAll(".multi-select[open]").forEach(details => {
      if (!details.contains(event.target)) details.open = false;
    });
  });
}
