// A fake payment gateway, and the ledger that adjudicates.
//
// Every refund is appended and fsynced before the call returns, so an agent
// that dies the instant after refunding is still counted as having refunded.
// Nothing self-reports.
import { appendFileSync, closeSync, existsSync, fsyncSync, openSync, readFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";

const LEDGER = join(import.meta.dirname, "ledger.jsonl");

export type Refund = { run: string; ticket: string; amount: number; agent: string; at: number };

export function refund(entry: Omit<Refund, "at">): void {
  const fd = openSync(LEDGER, "a");
  try {
    appendFileSync(fd, JSON.stringify({ ...entry, at: Date.now() }) + "\n");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

export function refundsFor(run: string): Refund[] {
  if (!existsSync(LEDGER)) return [];
  return readFileSync(LEDGER, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Refund)
    .filter((r) => r.run === run);
}

export function reset(): void {
  if (existsSync(LEDGER)) unlinkSync(LEDGER);
}
