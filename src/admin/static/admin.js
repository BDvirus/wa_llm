// wa_llm admin — the little glue htmx doesn't do on its own.
(() => {
  "use strict";

  const dialog = document.getElementById("dialog");
  const toast = document.getElementById("toast");
  let toastTimer;

  function showToast(text) {
    toast.textContent = text; // textContent, never innerHTML: server text may echo input
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 7000);
  }

  function messageFrom(xhr) {
    const body = (xhr && xhr.responseText) || "";
    try {
      const parsed = JSON.parse(body);
      if (parsed && parsed.detail) return String(parsed.detail);
    } catch (_) { /* plain-text error body */ }
    return body.trim().slice(0, 300) || "הפעולה נכשלה";
  }

  // An expired Authentik session makes the XHR follow a redirect to the login
  // page. Never swap that page into the table - ask for a reload instead.
  document.body.addEventListener("htmx:beforeSwap", (evt) => {
    const url = evt.detail.xhr.responseURL || "";
    if (url && !url.startsWith(`${location.origin}/admin`)) {
      evt.detail.shouldSwap = false;
      evt.detail.isError = false;
      showToast("החיבור פג — יש לרענן את הדף ולהתחבר מחדש");
    }
  });

  document.body.addEventListener("htmx:responseError", (evt) => showToast(messageFrom(evt.detail.xhr)));
  document.body.addEventListener("htmx:sendError", () => showToast("אין חיבור לשרת — יש לרענן את הדף"));

  // The enable confirmation is loaded into the dialog body; open it once it lands.
  document.body.addEventListener("htmx:afterSwap", (evt) => {
    if (evt.detail.target && evt.detail.target.id === "dialog-body" && !dialog.open) {
      dialog.showModal();
    }
  });

  // Close the dialog once its form succeeded.
  document.body.addEventListener("htmx:afterRequest", (evt) => {
    const elt = evt.detail.elt;
    if (dialog.open && evt.detail.successful && elt.tagName === "FORM" && dialog.contains(elt)) {
      dialog.close();
    }
  });

  document.addEventListener("click", (evt) => {
    if (evt.target.closest("[data-close-dialog]")) dialog.close();
  });

  // Client-side filter; re-applied after swaps so replaced rows keep the filter.
  const filter = document.getElementById("filter");
  const emptyFilter = document.querySelector(".empty--filter");

  function applyFilter() {
    if (!filter) return;
    const query = filter.value.trim().toLowerCase();
    let visible = 0;
    document.querySelectorAll("#rows > tr").forEach((row) => {
      const match = !query || (row.dataset.search || "").includes(query);
      row.hidden = !match;
      if (match) visible += 1;
    });
    if (emptyFilter) emptyFilter.hidden = visible > 0;
  }

  if (filter) {
    filter.addEventListener("input", applyFilter);
    document.body.addEventListener("htmx:afterSettle", applyFilter);
  }
})();
