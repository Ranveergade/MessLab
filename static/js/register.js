(function () {
  "use strict";

  var role = document.querySelector("[data-reg-role]");
  var hostelField = document.querySelector("[data-hostel-field]");
  var hostel = document.querySelector("[data-reg-hostel]");

  function syncHostel() {
    if (!role || !hostelField || !hostel) return;
    var needed = role.value === "hostel_student";
    hostelField.hidden = !needed;
    hostel.required = needed;
  }

  if (role) {
    role.addEventListener("change", syncHostel);
    syncHostel();
  }

  // Resend cooldown shared with the login screen.
  var resend = document.getElementById("otp-resend");
  var hint = document.getElementById("otp-resend-hint");
  var email = document.querySelector('input[name="email"]');
  if (!resend || !hint || !email) return;

  var cooldown = parseInt(resend.dataset.cooldown || "60", 10);
  var key = "messlab:otp-resend:" + (email.value || "").toLowerCase();
  var timer = null;

  function until() {
    try {
      return parseInt(window.sessionStorage.getItem(key) || "0", 10) || 0;
    } catch (err) {
      return 0;
    }
  }

  function tick() {
    var remaining = Math.ceil((until() - Date.now()) / 1000);
    if (remaining > 0) {
      resend.disabled = true;
      hint.textContent = "You can resend in " + remaining + "s";
      return;
    }
    resend.disabled = false;
    hint.textContent = "";
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  var form = document.getElementById("otp-resend-form");
  if (form) {
    form.addEventListener("submit", function () {
      try {
        window.sessionStorage.setItem(key, String(Date.now() + cooldown * 1000));
      } catch (err) {
        /* storage can be unavailable in private mode */
      }
    });
  }

  tick();
  if (until() > Date.now()) timer = window.setInterval(tick, 1000);
})();
