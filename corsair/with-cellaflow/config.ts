// Where the Corsair clone lives. The benchmark imports their real key manager
// by source path rather than vendoring a copy, so there is no question about
// whether the code under test is theirs.
//
//   git clone https://github.com/corsairdev/corsair
//   cd corsair && git checkout 4f268cdf && cd ..
//
// Clone it into this directory and nothing needs configuring. CORSAIR_SRC
// overrides that if the clone lives somewhere else.
import { existsSync } from "node:fs";
import { join } from "node:path";

const HERE = import.meta.dirname;

/**
 * The clone next to this file, then the one in the sibling arm, so a single
 * checkout serves both arms. Falling back to the local path when neither
 * exists keeps the error message about a missing directory rather than about
 * `undefined`.
 */
function findCorsair(): string {
  const candidates = [
    join(HERE, "corsair/packages/corsair"),
    join(HERE, "../corsair/packages/corsair"),
  ];
  return candidates.find(existsSync) ?? candidates[0];
}

export const CORSAIR_SRC = process.env.CORSAIR_SRC ?? findCorsair();

export const KEK = "benchmark-kek-with-at-least-32-characters!!";
export const TENANT = "tenant-acme";
export const INTEGRATION = "outlook";

/** Where the fake provider listens. */
export const PROVIDER_PORT = Number(process.env.PROVIDER_PORT ?? 8892);
export const PROVIDER_URL = `http://127.0.0.1:${PROVIDER_PORT}`;
