(function () {
  "use strict";

  // Demo role buttons: request a code for the seeded account and flag the
  // verify step so it can sign in without typing the demo code.
  var requestForm = document.getElementById("otp-request-form");
  if (requestForm) {
    document.querySelectorAll("[data-demo-email]").forEach(function (button) {
      button.addEventListener("click", function () {
        var email = requestForm.querySelector("#id_email");
        var autologin = requestForm.querySelector("#id_autologin");
        if (!email) return;
        email.value = button.dataset.demoEmail || "";
        if (autologin) autologin.value = "1";
        requestForm.submit();
      });
    });
  }

  var card = document.querySelector("[data-otp-card]");
  if (!card) return;

  var codeInput = document.getElementById("id_code");
  var verifyForm = document.getElementById("otp-verify-form");

  // Demo auto sign-in: the server only sends data-dev-hint in DEBUG demo mode.
  var hint = card.dataset.devHint || "";
  if (card.dataset.autologin === "1" && hint && codeInput && verifyForm) {
    codeInput.value = hint;
    window.setTimeout(function () {
      verifyForm.submit();
    }, 120);
  }

  // Keep the resend button honest across page reloads.
  var resend = document.getElementById("otp-resend");
  var hintEl = document.getElementById("otp-resend-hint");
  var emailInput = verifyForm && verifyForm.querySelector('input[name="email"]');
  if (!resend || !hintEl || !emailInput) return;

  var cooldown = parseInt(resend.dataset.cooldown || "60", 10);
  var storageKey = "messlab:otp-resend:" + (emailInput.value || "").toLowerCase();
  var timer = null;

  function until() {
    try {
      return parseInt(window.sessionStorage.getItem(storageKey) || "0", 10) || 0;
    } catch (err) {
      return 0;
    }
  }

  function tick() {
    var remaining = Math.ceil((until() - Date.now()) / 1000);
    if (remaining > 0) {
      resend.disabled = true;
      hintEl.textContent = "You can resend in " + remaining + "s";
      return;
    }
    resend.disabled = false;
    hintEl.textContent = "";
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  var resendForm = document.getElementById("otp-resend-form");
  if (resendForm) {
    resendForm.addEventListener("submit", function () {
      try {
        window.sessionStorage.setItem(storageKey, String(Date.now() + cooldown * 1000));
      } catch (err) {
        /* storage can be unavailable in private mode */
      }
    });
  }

  tick();
  if (until() > Date.now()) {
    timer = window.setInterval(tick, 1000);
  }
})();
