// The same driver as the audit one level up, with the refresh wrapped in a
// CellaFlow leased tool.
//
// Corsair's in-process singleFlight is left exactly where it was. This arm adds
// cross-process coordination on top of it; it does not replace it.
//
// `tool()` does three things a lock cannot do together: it admits one caller,
// it records what that caller produced, and it hands that record to whoever
// asks next. The third is the one that matters here. If the holder dies between
// spending the refresh token and storing the new one, a lock has nothing to
// hand over and the token is simply gone.
import { durableTools, tool } from "@cellaflow/sdk";
import { CORSAIR_SRC, INTEGRATION, PROVIDER_URL, TENANT } from "./config.js";
import { openStore } from "./keystore.js";

const RUN = process.env.BENCH_RUN!;
const DB = process.env.BENCH_DB!;
const CALLERS = Number(process.env.BENCH_CALLERS ?? 4);
const CRASH = process.env.BENCH_CRASH ?? "";

// Keyed on the credential, not on the run or the process, so every replica
// anywhere derives the same key. That is the whole point.
const LEASE_KEY = `corsair:refresh:${TENANT}:${INTEGRATION}:${RUN}`;

const isFresh = (expiresAt: string | undefined) =>
  expiresAt !== undefined && Number(expiresAt) > Date.now() / 1000 + 60;

type Tokens = { access_token: string; refresh_token?: string; expires_in: number };

/**
 * Exchanges the refresh token at the provider.
 *
 * Wrapped in `tool` with an explicit key, so the engine admits one caller and
 * commits the result. A later caller deriving the same key does not run this
 * body: it is handed what this call returned, even if this process has since
 * died.
 */
const exchangeToken = tool(
  async (refreshToken: string): Promise<Tokens | null> => {
    const res = await fetch(`${PROVIDER_URL}/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        run: RUN,
        grant_type: "refresh_token",
        refresh_token: refreshToken,
      }).toString(),
    });
    if (!res.ok) {
      console.log("[corsair] refresh rejected: the stored refresh_token was already spent");
      return null;
    }
    const tokens = (await res.json()) as Tokens;

    // The token is now spent at the provider. Everything after this point is a
    // race against losing the only copy of what it became, and this crash lands
    // before `tool` commits, so nothing records it.
    if (CRASH === "after_exchange") {
      console.log("[corsair] dying with the new token still only in memory");
      process.exit(137);
    }

    return tokens;
  },
  { idempotencyKey: LEASE_KEY, toolName: "oauth_refresh" },
);

async function main() {
  const { singleFlight } = await import(`${CORSAIR_SRC}/core/auth/single-flight.ts`);
  const store = await openStore(DB);
  const km = store.km;

  const persist = async (t: Tokens): Promise<void> => {
    await km.set_access_token(t.access_token);
    if (t.refresh_token) await km.set_refresh_token(t.refresh_token);
    await km.set_expires_at(String(Math.floor(Date.now() / 1000) + t.expires_in));
  };

  const refreshIfStale = async (): Promise<void> => {
    const [access, expiresAt] = await Promise.all([
      km.get_access_token(),
      km.get_expires_at(),
    ]);
    if (access && isFresh(expiresAt)) return;

    // Corsair's in-process dedupe, unchanged.
    await singleFlight(km, "refresh", async () => {
      const [a2, e2] = await Promise.all([km.get_access_token(), km.get_expires_at()]);
      if (a2 && isFresh(e2)) return;

      const stored = await km.get_refresh_token();
      if (!stored) throw new Error("no refresh_token stored");

      // Either performs the exchange, or returns the result of whoever already
      // did. The caller cannot tell which, and does not need to.
      const tokens = await exchangeToken(stored);
      if (!tokens) return;

      // Reached here on a recovered result too, which is the recovery path: a
      // previous process spent the token and died before storing it, and the
      // result survived the process that produced it.
      if (CRASH === "after_commit") {
        console.log("[corsair] dying after the exchange was recorded, before storing it");
        process.exit(137);
      }

      await persist(tokens);
    });
  };

  await durableTools(
    RUN,
    { coordinationId: RUN, target: process.env.CELLAFLOW_TARGET ?? "localhost:50051" },
    async () => {
      await Promise.all(Array.from({ length: CALLERS }, () => refreshIfStale()));
    },
  );

  await store.close();
}

main().catch((err) => {
  console.error("[corsair] driver failed:", err?.message ?? err);
  process.exit(1);
});
