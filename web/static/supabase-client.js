// AgenticWhales — Supabase glue (Auth + Postgres for usage tracking).
//
// Configuration is supplied at runtime by the server, which reads
//   AGENTICWHALES_SUPABASE_URL
//   AGENTICWHALES_SUPABASE_ANON_KEY
// from the environment (or .env) and injects an inline <script> setting
// `window.__AGENTICWHALES_SUPABASE_CONFIG` into the served HTML before this
// module evaluates. See web/server.py::_supabase_runtime_config_tag.
//
// If those env vars are unset, the code below falls through to placeholder
// sentinels and the welcome modal degrades to a localStorage guest mode that
// still enforces the per-day cap. The repo never ships real credentials.
//
// One-time setup checklist for a new Supabase project:
//   1. Run the SQL in /docs/supabase-schema.sql to create the `profiles` and
//      `usage_daily` tables, the RLS policies, and the `increment_usage` RPC.
//   2. Authentication → Providers → Google → enable; paste your Google OAuth
//      Client ID + Client Secret (created at console.cloud.google.com).
//   3. Authentication → URL Configuration → set Site URL to your origin
//      (e.g. http://localhost:8765) and add prod origins to Additional
//      Redirect URLs as needed.
//   4. Project settings → API → copy the Project URL and `anon` public key.
//   5. Add them to .env (dev) or your prod environment as
//      AGENTICWHALES_SUPABASE_URL / AGENTICWHALES_SUPABASE_ANON_KEY.

// The anon key is intentionally shipped to the browser — Supabase's auth flow
// requires it client-side. Security comes from Row Level Security on every
// public table (see docs/supabase-schema.sql). NEVER put the service_role key
// here; that bypasses RLS.
//
// In production the server injects `window.__AGENTICWHALES_SUPABASE_CONFIG`
// from env vars; the literal sentinels below are only used when env vars are
// unset, which keeps the welcome modal in guest mode.
const SUPABASE_CONFIG = window.__AGENTICWHALES_SUPABASE_CONFIG || {
  url: "https://YOUR-PROJECT-REF.supabase.co",
  anonKey: "REPLACE_WITH_YOUR_SUPABASE_ANON_KEY",
};

const DAILY_LIMIT_BY_TIER = {
  novice: 3,
  intermediate: 50,
  master: Infinity,
};

// "Configured" = both fields are present and don't still contain the
// placeholder sentinel strings the file ships with.
const isConfigured = !!(
  SUPABASE_CONFIG.url &&
  !SUPABASE_CONFIG.url.includes("YOUR-PROJECT-REF") &&
  SUPABASE_CONFIG.anonKey &&
  !SUPABASE_CONFIG.anonKey.startsWith("REPLACE_")
);

// ---------- Pub/sub for auth state changes ----------
const listeners = new Set();
let currentUser = null; // { uid, displayName, email, photoURL, tier, isGuest }

function emit() {
  for (const fn of listeners) {
    try { fn(currentUser); } catch (e) { console.error("auth listener error:", e); }
  }
}

function todayKey() {
  // UTC date so the per-day cap rolls at 00:00 UTC.
  const d = new Date();
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
}

// ===================================================================
// Path A: Supabase IS configured
// ===================================================================
async function bootSupabase() {
  // ESM build of supabase-js v2 from the official CDN.
  const { createClient } = await import("https://esm.sh/@supabase/supabase-js@2");
  const sb = createClient(SUPABASE_CONFIG.url, SUPABASE_CONFIG.anonKey);

  async function loadProfile(authUser) {
    // 1) Try to fetch existing profile
    const { data: existing, error: selErr } = await sb
      .from("profiles")
      .select("id, username, tier")
      .eq("id", authUser.id)
      .maybeSingle();
    if (selErr) console.warn("profile select failed:", selErr.message);
    if (existing) return existing;

    // 2) None — create one (RLS allows insert where id = auth.uid()).
    const username =
      authUser.user_metadata?.full_name ||
      authUser.user_metadata?.name ||
      authUser.email ||
      "trader";
    const { data: created, error: insErr } = await sb
      .from("profiles")
      .insert({ id: authUser.id, username, tier: "novice" })
      .select("id, username, tier")
      .single();
    if (insErr) {
      console.warn("profile insert failed:", insErr.message);
      return { id: authUser.id, username, tier: "novice" };
    }
    return created;
  }

  async function syncFromSession(session) {
    if (!session?.user) {
      currentUser = null;
      emit();
      return;
    }
    const profile = await loadProfile(session.user);
    currentUser = {
      uid: session.user.id,
      displayName: profile.username || session.user.user_metadata?.full_name || "trader",
      email: session.user.email,
      photoURL: session.user.user_metadata?.avatar_url || null,
      tier: profile.tier || "novice",
      isGuest: false,
    };
    emit();
  }

  // Initial session check + subscribe to changes.
  const { data: initial } = await sb.auth.getSession();
  let _session = initial.session;
  await syncFromSession(_session);
  sb.auth.onAuthStateChange((_evt, session) => {
    _session = session;
    syncFromSession(session);
  });

  return {
    isConfigured: true,
    getAccessToken: () => _session?.access_token || null,
    signInWithGoogle: async () => {
      const { error } = await sb.auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: window.location.origin + window.location.pathname },
      });
      if (error) throw error;
    },
    signOut: async () => {
      const { error } = await sb.auth.signOut();
      if (error) throw error;
    },
    setDisplayName: async (name) => {
      if (!currentUser || currentUser.isGuest) return;
      const { error } = await sb
        .from("profiles")
        .update({ username: name })
        .eq("id", currentUser.uid);
      if (error) {
        console.warn("setDisplayName failed:", error.message);
        return;
      }
      currentUser = { ...currentUser, displayName: name };
      emit();
    },
    getUsageToday: async () => {
      if (!currentUser || currentUser.isGuest) return 0;
      const { data, error } = await sb
        .from("usage_daily")
        .select("count")
        .eq("user_id", currentUser.uid)
        .eq("day", todayKey())
        .maybeSingle();
      if (error) {
        console.warn("getUsageToday failed:", error.message);
        return 0;
      }
      return data?.count || 0;
    },
    incrementUsage: async () => {
      if (!currentUser || currentUser.isGuest) return 0;
      // Use the SQL function `increment_usage` (defined in the migration) for
      // an atomic upsert + increment. Falls back to a select-then-upsert if the
      // RPC isn't present.
      const { data: rpcData, error: rpcErr } = await sb.rpc("increment_usage");
      if (!rpcErr && typeof rpcData === "number") return rpcData;
      // Fallback path
      const day = todayKey();
      const { data: existing } = await sb
        .from("usage_daily")
        .select("count")
        .eq("user_id", currentUser.uid)
        .eq("day", day)
        .maybeSingle();
      const next = (existing?.count || 0) + 1;
      const { error: upErr } = await sb
        .from("usage_daily")
        .upsert({ user_id: currentUser.uid, day, count: next }, { onConflict: "user_id,day" });
      if (upErr) console.warn("incrementUsage upsert failed:", upErr.message);
      return next;
    },
  };
}

// ===================================================================
// Path B: Supabase NOT configured — localStorage guest mode
// ===================================================================
function bootGuest() {
  const GUEST_USER_KEY = "agenticwhales:guest-user";
  const GUEST_USAGE_KEY = (uid) => `agenticwhales:usage:${uid}:${todayKey()}`;

  function loadGuest() {
    try {
      const raw = localStorage.getItem(GUEST_USER_KEY);
      if (raw) return JSON.parse(raw);
    } catch { }
    return null;
  }

  function saveGuest(u) {
    try { localStorage.setItem(GUEST_USER_KEY, JSON.stringify(u)); } catch { }
  }

  const existing = loadGuest();
  if (existing) {
    currentUser = existing;
    queueMicrotask(emit);
  }

  return {
    isConfigured: false,
    getAccessToken: () => null,
    signInWithGoogle: async () => {
      throw new Error("Supabase isn't configured. Use 'Continue as guest' or wire up supabase-client.js.");
    },
    signInAsGuest: (displayName) => {
      const uid = `guest-${Math.random().toString(36).slice(2, 10)}`;
      const u = {
        uid,
        displayName: displayName || "Guest",
        email: null,
        photoURL: null,
        tier: "novice",
        isGuest: true,
      };
      saveGuest(u);
      currentUser = u;
      emit();
    },
    signOut: async () => {
      try { localStorage.removeItem(GUEST_USER_KEY); } catch { }
      currentUser = null;
      emit();
    },
    setDisplayName: async (name) => {
      if (!currentUser) return;
      currentUser = { ...currentUser, displayName: name };
      saveGuest(currentUser);
      emit();
    },
    getUsageToday: async () => {
      if (!currentUser) return 0;
      try {
        const raw = localStorage.getItem(GUEST_USAGE_KEY(currentUser.uid));
        return raw ? Number(JSON.parse(raw).count || 0) : 0;
      } catch { return 0; }
    },
    incrementUsage: async () => {
      if (!currentUser) return 0;
      const key = GUEST_USAGE_KEY(currentUser.uid);
      let n = 0;
      try {
        const raw = localStorage.getItem(key);
        n = raw ? Number(JSON.parse(raw).count || 0) : 0;
      } catch { }
      n += 1;
      try { localStorage.setItem(key, JSON.stringify({ count: n, ts: Date.now() })); } catch { }
      return n;
    },
  };
}

// ---------- Public surface ----------
const backend = isConfigured
  ? await bootSupabase().catch((e) => {
    console.error("Supabase boot failed, falling back to guest mode:", e);
    return bootGuest();
  })
  : bootGuest();

window.AgenticWhalesAuth = {
  isConfigured: backend.isConfigured,
  onChange: (fn) => { listeners.add(fn); fn(currentUser); return () => listeners.delete(fn); },
  getUser: () => currentUser,
  // The Supabase access token (JWT). Sent as `Authorization: Bearer <token>`
  // on every /api fetch and as `?token=` on WebSocket connects so the server
  // can scope sessions/batches by user.
  getAccessToken: backend.getAccessToken,
  signInWithGoogle: backend.signInWithGoogle,
  signInAsGuest: backend.signInAsGuest || (() => { throw new Error("Guest sign-in only available when Supabase is unconfigured."); }),
  signOut: backend.signOut,
  setDisplayName: backend.setDisplayName,
  getUsageToday: backend.getUsageToday,
  incrementUsage: backend.incrementUsage,
  dailyLimitFor: (tier) => DAILY_LIMIT_BY_TIER[tier] ?? DAILY_LIMIT_BY_TIER.novice,
};

window.dispatchEvent(new CustomEvent("agenticwhales-auth-ready"));
