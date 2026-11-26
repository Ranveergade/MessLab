(function () {
  "use strict";

  function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(";").shift();
    return "";
  }

  const csrf = getCookie("csrftoken");

  function toast(message, type) {
    let wrap = document.querySelector(".flash-wrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "flash-wrap";
      document.body.appendChild(wrap);
    }
    const el = document.createElement("div");
    el.className = `flash ${type || "success"}`;
    el.setAttribute("role", "status");
    el.innerHTML = `<span aria-hidden="true">${type === "error" ? "⚠️" : "✅"}</span><span></span>`;
    el.lastElementChild.textContent = message;
    wrap.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateY(-6px)";
      el.style.transition = "all .25s ease";
      setTimeout(() => el.remove(), 300);
    }, 4200);
  }

  window.messlabToast = toast;

  document.querySelectorAll("[data-print]").forEach((button) =>
    button.addEventListener("click", () => window.print())
  );

  // Mobile manager drawer
  const drawer = document.querySelector("[data-drawer]");
  const backdrop = document.querySelector("[data-drawer-backdrop]");
  function setDrawer(open) {
    if (!drawer) return;
    drawer.classList.toggle("open", open);
    if (backdrop) backdrop.classList.toggle("open", open);
    document.body.style.overflow = open ? "hidden" : "";
  }
  document.querySelectorAll("[data-drawer-open]").forEach((btn) =>
    btn.addEventListener("click", () => setDrawer(true))
  );
  document.querySelectorAll("[data-drawer-close]").forEach((btn) =>
    btn.addEventListener("click", () => setDrawer(false))
  );
  if (backdrop) backdrop.addEventListener("click", () => setDrawer(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setDrawer(false);
  });

  // One-tap intent: optimistic UI, API call, graceful fallback to the form.
  document.querySelectorAll("form.js-intent-form").forEach((form) => {
    const note = form.querySelector("[data-intent-note]");
    form.querySelectorAll("button[data-status]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        if (!window.fetch) return;
        event.preventDefault();
        const status = button.dataset.status;
        const payload = {
          date: form.dataset.date,
          meal: form.dataset.meal,
          status: status,
        };
        const buttons = form.querySelectorAll("button[data-status]");
        buttons.forEach((b) => (b.disabled = true));
        try {
          const response = await fetch("/api/v1/intents/", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": csrf,
            },
            body: JSON.stringify(payload),
          });
          if (!response.ok) throw new Error("request failed");
          const data = await response.json();
          buttons.forEach((b) => {
            b.classList.remove("active-yes", "active-no", "active-maybe");
          });
          const cls = status === "YES" ? "active-yes" : status === "NO" ? "active-no" : "active-maybe";
          button.classList.add(cls);
          if (note) {
            note.textContent =
              status === "YES"
                ? "Saved. The kitchen now counts you in."
                : status === "NO"
                ? "Saved. The kitchen will cook less — and you earn green points."
                : "Saved as maybe. The kitchen plans a small buffer.";
          }
          toast(
            status === "YES"
              ? "You're counted in."
              : status === "NO"
              ? "Skip saved with notice."
              : "Marked as maybe."
          );
          const card = form.closest("[data-meal-card]");
          if (card) card.dataset.intent = status;
        } catch (err) {
          form.submit();
        } finally {
          buttons.forEach((b) => (b.disabled = false));
        }
      });
    });
  });

  // Confirmation for destructive / state-changing actions
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  // Loading state on submit
  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", () => {
      const btn = form.querySelector("button[type=submit]");
      if (btn && !btn.classList.contains("js-no-loading")) btn.classList.add("is-loading");
    });
  });

  // Auto-submit filters
  document.querySelectorAll("[data-auto-submit]").forEach((el) => {
    el.addEventListener("change", () => el.form && el.form.submit());
  });

  // PWA
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }
})();
