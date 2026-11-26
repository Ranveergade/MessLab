/* Camera QR scanner for the canteen desk.
 *
 * Uses the built-in BarcodeDetector where the browser supports it, and falls
 * back to the vendored jsQR decoder in ``static/vendor/jsQR.js``. Both run
 * locally in the browser; frames are never uploaded. Manual entry is always
 * available if the camera or browser cannot scan.
 */
(() => {
  const form = document.querySelector("[data-scan-form]");
  if (!form) return;

  const video = document.getElementById("scan-video");
  const status = document.getElementById("scan-status");
  const startBtn = document.querySelector("[data-scan-start]");
  const stopBtn = document.querySelector("[data-scan-stop]");
  const tokenInput = form.querySelector("[data-scan-token]");

  let stream = null;
  let detector = null;
  let timer = null;
  let busy = false;
  let canvas = null;
  let context = null;

  const say = (text, tone) => {
    if (!status) return;
    status.textContent = text;
    status.dataset.tone = tone || "";
  };

  const stop = () => {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
    if (video) {
      video.srcObject = null;
    }
    detector = null;
    busy = false;
    if (startBtn) startBtn.hidden = false;
    if (stopBtn) stopBtn.hidden = true;
    say("Camera is off.");
  };

  const submit = (raw) => {
    if (busy || !raw) return;
    busy = true;
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    tokenInput.value = raw;
    say("Pass read. Checking with the server…");
    form.submit();
  };

  const decodeWithJsQr = () => {
    if (!window.jsQR || !video || !video.videoWidth) return null;
    if (!canvas) {
      canvas = document.createElement("canvas");
      context = canvas.getContext("2d", { willReadFrequently: true });
    }
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const frame = context.getImageData(0, 0, canvas.width, canvas.height);
    const result = window.jsQR(frame.data, frame.width, frame.height, {
      inversionAttempts: "dontInvert",
    });
    return result && result.data ? result.data : null;
  };

  const tick = async () => {
    if (busy || !video || !video.videoWidth) return;
    try {
      if (detector) {
        const codes = await detector.detect(video);
        if (codes && codes.length > 0) {
          submit(codes[0].rawValue);
          return;
        }
      }
      const raw = decodeWithJsQr();
      if (raw) submit(raw);
    } catch (error) {
      say("Could not read the frame. Hold the pass steady.", "warn");
    }
  };

  const canScan = () => "BarcodeDetector" in window || Boolean(window.jsQR);

  if (startBtn) {
    startBtn.addEventListener("click", async () => {
      if (!canScan()) {
        say("This browser has no QR reader. Use manual entry below.", "warn");
        return;
      }
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        say("Camera access is unavailable. Use manual entry below.", "warn");
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
          audio: false,
        });
        video.srcObject = stream;
        await video.play();
        detector =
          "BarcodeDetector" in window
            ? new window.BarcodeDetector({ formats: ["qr_code"] })
            : null;
        timer = window.setInterval(tick, 400);
        startBtn.hidden = true;
        stopBtn.hidden = false;
        say("Camera on. Point it at the student pass.");
      } catch (error) {
        stop();
        say(
          "Camera unavailable: " +
            (error.message || "permission denied") +
            ". Use manual entry.",
          "warn"
        );
      }
    });
  }

  if (stopBtn) stopBtn.addEventListener("click", stop);
  window.addEventListener("pagehide", stop);
})();
