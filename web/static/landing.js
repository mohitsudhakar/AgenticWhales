// Landing page (/) — sign-in gate. The user must accept the Disclaimer,
// Privacy Policy, and Terms of Use, then complete Google sign-in. We POST
// the attestation to /api/audit/compliance-ack BEFORE redirecting to /fund
// so the server has a non-revoked attestation row matching the active
// disclaimer version. Without that row, /fund's actions fail with a 412.

const $ = (sel) => document.querySelector(sel);
const REDIRECT_TARGET = "/fund";

let _ACTIVE_DISCLAIMER_VERSION = "v1.0";  // overwritten by GET /api/compliance/docs

window.addEventListener("agenticwhales-auth-ready", initLanding);
if (window.AgenticWhalesAuth) initLanding();

async function initLanding() {
  const auth = window.AgenticWhalesAuth;
  if (!auth) return;

  // Pull the live active disclaimer version + summaries so the version
  // label on the consent line is always truthful.
  try {
    const r = await fetch("/api/compliance/docs");
    if (r.ok) {
      const data = await r.json();
      _ACTIVE_DISCLAIMER_VERSION = data.version || _ACTIVE_DISCLAIMER_VERSION;
      const vlabel = document.getElementById("welcome-disclaimer-version");
      if (vlabel) vlabel.textContent = `Version ${_ACTIVE_DISCLAIMER_VERSION}`;
      const dvlabel = document.getElementById("disclaimer-version-label");
      if (dvlabel) dvlabel.textContent = _ACTIVE_DISCLAIMER_VERSION;
    }
  } catch (e) { /* offline OK — fall through to default v1.0 */ }

  // Already-signed-in users: post the attestation (idempotent — server
  // creates a fresh row only if none exists for the active version) and
  // then redirect.
  auth.onChange(async (user) => {
    if (!user) return;
    if (sessionStorage.getItem("aw_landing_consent_clicked") === "1") {
      // The user got here by checking the consent box + clicking Google;
      // record the attestation before redirecting.
      try { await postAttestation(auth); } catch (e) { console.warn("attestation post failed:", e); }
      sessionStorage.removeItem("aw_landing_consent_clicked");
    }
    // Loop guard: a session-restore race can have fund.js bounce a
    // half-restored signed-out state back to / right as we're trying to
    // forward here, and then onChange fires again with the real user. If
    // we've already redirected here twice in 10s, stop — let the user
    // click through manually instead of getting stuck reloading.
    if (_allowLandingRedirect()) {
      window.location.replace(REDIRECT_TARGET);
    }
  });

  initWelcomeControls(auth);
  initLegalModalControls();
  initHowItWorksCarousel();
}

function _allowLandingRedirect() {
  const key = "aw_redirect_root_to_fund";
  const now = Date.now();
  let hist = [];
  try { hist = JSON.parse(sessionStorage.getItem(key) || "[]"); } catch (_) {}
  hist = hist.filter((t) => now - t < 10_000);
  if (hist.length >= 2) {
    console.warn("landing redirect guard tripped — bounced /↔/fund 2× in 10s; staying on /.");
    return false;
  }
  hist.push(now);
  try { sessionStorage.setItem(key, JSON.stringify(hist)); } catch (_) {}
  return true;
}

async function postAttestation(auth) {
  const token = auth?.getAccessToken?.();
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch("/api/audit/compliance-ack", {
    method: "POST",
    headers,
    body: JSON.stringify({
      version: _ACTIVE_DISCLAIMER_VERSION,
      ack_paper_only: true,
      ack_not_advice: true,
      ack_jurisdiction: true,
    }),
  });
  if (!res.ok) {
    const j = await res.json().catch(() => ({}));
    throw new Error(j.detail?.message || j.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function initWelcomeControls(auth) {
  const agree = $("#welcome-agree");
  const googleBtn = $("#welcome-google");
  const hint = $("#welcome-firebase-hint");

  if (!auth.isConfigured) {
    if (hint) hint.hidden = false;
    googleBtn.disabled = true;
    return;
  }

  const update = () => { googleBtn.disabled = !agree.checked; };
  agree.addEventListener("change", update);
  update();

  googleBtn.addEventListener("click", async () => {
    googleBtn.disabled = true;
    // Persist that this navigation flow started from the consent screen, so
    // when the OAuth redirect returns to / we know to POST the attestation
    // before the onChange handler forwards to /fund.
    try { sessionStorage.setItem("aw_landing_consent_clicked", "1"); } catch (_) {}
    try {
      await auth.signInWithGoogle();
      // Supabase performs a full-page OAuth redirect; the post-OAuth load
      // hits /, the onChange listener sees the new session, posts the
      // attestation, and then forwards to /fund. No explicit redirect here.
    } catch (err) {
      console.error("Google sign-in failed:", err);
      alert(`Sign-in failed: ${err.message || err}`);
      googleBtn.disabled = false;
      try { sessionStorage.removeItem("aw_landing_consent_clicked"); } catch (_) {}
    }
  });

  setTimeout(() => agree?.focus(), 50);
}

function initLegalModalControls() {
  const which2id = {
    disclaimer: "disclaimer-modal",
    privacy: "privacy-modal",
    terms: "terms-modal",
  };
  document.addEventListener("click", (e) => {
    const open = e.target.closest?.("[data-open]");
    if (open) {
      const id = which2id[open.dataset.open];
      if (id) document.getElementById(id)?.classList.remove("hidden");
      return;
    }
    const close = e.target.closest?.("[data-close-modal]");
    if (close) {
      document.getElementById(close.dataset.closeModal)?.classList.add("hidden");
      return;
    }
    // Backdrop dismiss for legal modals only — the welcome modal is the
    // gate itself and is intentionally non-dismissable.
    for (const id of Object.values(which2id)) {
      if (e.target.id === id) document.getElementById(id)?.classList.add("hidden");
    }
  });
}

// ---------- "How it works" rotating cards ----------

let howAutoTimer = null;

function initHowItWorksCarousel() {
  const track = $("#how-track");
  const dotsWrap = $("#how-dots");
  if (!track || !dotsWrap) return;
  const cards = Array.from(track.querySelectorAll(".how-card"));
  dotsWrap.innerHTML = "";
  cards.forEach((_, i) => {
    const dot = document.createElement("button");
    dot.type = "button";
    dot.className = "how-dot" + (i === 0 ? " active" : "");
    dot.dataset.idx = String(i);
    dot.addEventListener("click", () => goToCard(i, true));
    dotsWrap.appendChild(dot);
  });
  $("#how-prev").onclick = () => goToCard(currentCardIdx() - 1, true);
  $("#how-next").onclick = () => goToCard(currentCardIdx() + 1, true);
  goToCard(0, false);
  scheduleHowAutoplay();
}

function currentCardIdx() {
  const active = $("#how-track .how-card.active");
  if (!active) return 0;
  return Number(active.dataset.step || 1) - 1;
}

function goToCard(idx, manual) {
  const track = $("#how-track");
  if (!track) return;
  const cards = Array.from(track.querySelectorAll(".how-card"));
  const dots = Array.from($("#how-dots").querySelectorAll(".how-dot"));
  const n = cards.length;
  const i = ((idx % n) + n) % n;
  cards.forEach((c, k) => c.classList.toggle("active", k === i));
  dots.forEach((d, k) => d.classList.toggle("active", k === i));
  if (manual) scheduleHowAutoplay();
}

function scheduleHowAutoplay() {
  if (howAutoTimer) clearInterval(howAutoTimer);
  howAutoTimer = setInterval(() => goToCard(currentCardIdx() + 1, false), 5500);
}
