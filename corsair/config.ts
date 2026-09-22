// Where the Corsair clone lives. The benchmark imports their real key manager
// by source path rather than vendoring a copy, so there is no question about
// whether the code under test is theirs.
//
//   git clone https://github.com/corsairdev/corsair
//   cd corsair && git checkout 4f268cdf
export const CORSAIR_SRC =
  process.env.CORSAIR_SRC ??
  "/Users/blade/theblueskies/startup-source-codes/corsair/packages/corsair";

export const KEK = "benchmark-kek-with-at-least-32-characters!!";
export const TENANT = "tenant-acme";
export const INTEGRATION = "outlook";

/** Where the fake provider listens. */
export const PROVIDER_PORT = Number(process.env.PROVIDER_PORT ?? 8891);
export const PROVIDER_URL = `http://127.0.0.1:${PROVIDER_PORT}`;
