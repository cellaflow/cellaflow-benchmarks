// The adjudicator. Every model call the fake provider receives is appended and
// fsynced here before the response is written, so a process that dies the
// instant after a model call is still counted as having made it. Nothing in
// this benchmark self-reports, and in particular the agent tree is never asked
// how much work it did.
import {
  appendFileSync,
  closeSync,
  existsSync,
  fsyncSync,
  openSync,
  readFileSync,
  unlinkSync,
} from "node:fs";
import { join } from "node:path";

const LEDGER = join(import.meta.dirname, "ledger.jsonl");

export type Entry = {
  run: string;
  event: "model_call";
  /** Which of the eleven call sites this was, fingerprinted from the request. */
  site: string;
  /** Attempt 1 is the original answer; 2 is the retry after the crash. */
  attempt: number;
  promptTokens: number;
  completionTokens: number;
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
