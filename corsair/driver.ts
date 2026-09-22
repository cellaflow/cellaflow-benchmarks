// One process. Mirrors what Corsair's `core/auth/oauth-access.ts` does on a
// stale token: check freshness, dedupe concurrent refreshes through their real
// `singleFlight`, re-check inside the flight, then spend the refresh token and
// persist the result through their real key manager.
//
// Several concurrent callers are started on purpose. Within one process
// Corsair's singleFlight collapses them to a single provider call, and the
// control row proves it does. The question this benchmark asks is what happens
// when there are two processes.
import { CORSAIR_SRC, PROVIDER_URL } from "./config.js";
import { openStore } from "./keystore.js";

const RUN = process.env.BENCH_RUN!;
const DB = process.env.BENCH_DB!;
const CALLERS = Number(process.env.BENCH_CALLERS ?? 4);
// "after_exchange" kills this process the instant the provider has rotated the
// token and before anything is written back. That is the window no lock can
// close: the refresh_token has been spent at the provider, and the only record
// that it happened is about to be lost with the process.
const CRASH = process.env.BENCH_CRASH ?? "";

const isFresh = (expiresAt: string | undefined) =>
  expiresAt !== undefined && Number(expiresAt) > Date.now() / 1000 + 60;

async function main() {
  const { singleFlight } = await import(`${CORSAIR_SRC}/core/auth/single-flight.ts`);
  const store = await openStore(DB);
  const km = store.km;

  const refreshIfStale = async (): Promise<void> => {
    const [access, expiresAt] = await Promise.all([
      km.get_access_token(),
      km.get_expires_at(),
    ]);
    if (access && isFresh(expiresAt)) return;

    await singleFlight(km, "refresh", async () => {
      // Re-check under the flight: a concurrent caller in THIS process may have
      // already refreshed and persisted while we waited. Corsair does the same.
      const [a2, e2] = await Promise.all([km.get_access_token(), km.get_expires_at()]);
      if (a2 && isFresh(e2)) return;

      const refreshToken = await km.get_refresh_token();
      if (!refreshToken) throw new Error("no refresh_token stored");

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
        // The provider already consumed this token for someone else. Corsair
        // surfaces this to the caller; nothing repairs the stored credential.
        console.log(`[corsair] refresh rejected: the stored refresh_token was already spent`);
        return;
      }

      const tokens = (await res.json()) as {
        access_token: string;
        refresh_token?: string;
        expires_in: number;
      };

      // Both crash modes land here. This arm has no durable record between the
      // exchange and the write, so there is only one point at which to die.
      if (CRASH === "after_exchange" || CRASH === "after_commit") {
        console.log("[corsair] dying with the new token still only in memory");
        process.exit(137);
      }

      await km.set_access_token(tokens.access_token);
      if (tokens.refresh_token) await km.set_refresh_token(tokens.refresh_token);
      await km.set_expires_at(String(Math.floor(Date.now() / 1000) + tokens.expires_in));
    });
  };

  await Promise.all(Array.from({ length: CALLERS }, () => refreshIfStale()));
  await store.close();
}

main().catch((err) => {
  console.error("[corsair] driver failed:", err?.message ?? err);
  process.exit(1);
});
