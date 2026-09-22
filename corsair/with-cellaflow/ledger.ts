// The adjudicator. Every call the fake provider receives is appended and
// fsynced here before the response is written, so a process that dies the
// instant after spending a refresh token is still counted as having spent it.
// Nothing in this benchmark self-reports.
import { appendFileSync, closeSync, fsyncSync, openSync, readFileSync, existsSync, unlinkSync } from "node:fs";
import { join } from "node:path";

const LEDGER = join(import.meta.dirname, "ledger.jsonl");

export type Entry = {
  run: string;
  event: "refresh_accepted" | "refresh_rejected";
  presented: string;
  issued?: string;
  at: number;
};

export function record(entry: Entry): void {
  const fd = openSync(LEDGER, "a");
  try {
    appendFileSync(fd, JSON.stringify(entry) + "\n");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

export function entriesFor(run: string): Entry[] {
  if (!existsSync(LEDGER)) return [];
  return readFileSync(LEDGER, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Entry)
    .filter((e) => e.run === run);
}

export function reset(): void {
  if (existsSync(LEDGER)) unlinkSync(LEDGER);
}
