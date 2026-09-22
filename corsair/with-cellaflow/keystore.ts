// Builds a real Corsair account key manager against a file-backed SQLite, so
// two OS processes share one credential store. The schema mirrors the one in
// Corsair's own tests/setup-db.ts; only `:memory:` becomes a file, because an
// in-memory database cannot be raced.
import Database from "better-sqlite3";
import { Kysely, SqliteDialect } from "kysely";
import { CORSAIR_SRC, INTEGRATION, KEK, TENANT } from "./config.js";
import { SqliteDatePlugin } from "./sqlite-date-plugin.js";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS corsair_integrations (
  id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  name TEXT NOT NULL, config TEXT NOT NULL, dek TEXT NULL);
CREATE TABLE IF NOT EXISTS corsair_accounts (
  id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  tenant_id TEXT NOT NULL, integration_id TEXT NOT NULL, config TEXT NOT NULL, dek TEXT NULL);
`;

export async function openStore(dbPath: string) {
  const { createAccountKeyManager } = await import(`${CORSAIR_SRC}/core/auth/key-manager.ts`);
  const sqlite = new Database(dbPath);
  sqlite.exec(SCHEMA);
  const db = new Kysely<any>({
    dialect: new SqliteDialect({ database: sqlite }),
    plugins: [new SqliteDatePlugin()],
  });
  const database = { db };

  const km = createAccountKeyManager({
    authType: "oauth_2",
    integrationName: INTEGRATION,
    tenantId: TENANT,
    kek: KEK,
    database,
  });

  return { db, database, km, close: async () => { await db.destroy(); sqlite.close(); } };
}

/** Seed one connected tenant whose access token has already expired. */
export async function seed(dbPath: string, refreshToken: string) {
  const { encryptConfig, encryptDEK, generateDEK } = await import(
    `${CORSAIR_SRC}/core/auth/encryption.ts`
  );
  const sqlite = new Database(dbPath);
  sqlite.exec(SCHEMA);
  const db = new Kysely<any>({
    dialect: new SqliteDialect({ database: sqlite }),
    plugins: [new SqliteDatePlugin()],
  });

  const now = Date.now();
  const dek = generateDEK();
  const encryptedDek = await encryptDEK(dek, KEK);

  await db.insertInto("corsair_integrations").values({
    id: "integration-1", created_at: now, updated_at: now,
    name: INTEGRATION, config: encryptConfig({}, dek), dek: encryptedDek,
  }).execute();

  await db.insertInto("corsair_accounts").values({
    id: "account-1", created_at: now, updated_at: now,
    tenant_id: TENANT, integration_id: "integration-1",
    // expires_at in the past: every process that looks will decide to refresh.
    config: encryptConfig(
      { access_token: "a0", expires_at: "1", refresh_token: refreshToken },
      dek,
    ),
    dek: encryptedDek,
  }).execute();

  await db.destroy();
  sqlite.close();
}
