// The one thing the harness supplies besides the model endpoint: a database.
//
// Inconvo's DatabaseConnector is a single method, `query(q) => QueryResponse`,
// so the fixture answers from a fixed in-memory table rather than standing up a
// real engine. The metric here is how many model calls are re-run, not whether
// a query is correct, so a deterministic connector keeps the run reproducible
// and keeps the measurement on the thing being measured.

export const QUESTION =
  "Which product category produced the most revenue last quarter, and how did it compare with the quarter before?";

export const SCHEMA = [
  {
    name: "orders",
    summary: "One row per completed order.",
    columns: [
      { name: "id", type: "Int" },
      { name: "placed_at", type: "DateTime" },
      { name: "category", type: "String" },
      { name: "revenue_cents", type: "Int" },
    ],
  },
  {
    name: "customers",
    summary: "One row per customer account.",
    columns: [
      { name: "id", type: "Int" },
      { name: "region", type: "String" },
    ],
  },
];

const ROWS = [
  { category: "Hardware", revenue_cents: 4_210_000 },
  { category: "Software", revenue_cents: 3_880_000 },
  { category: "Services", revenue_cents: 1_140_000 },
];

export function fixtureConnector() {
  return {
    async query(_query: unknown) {
      return {
        type: "success",
        data: ROWS,
        rowCount: ROWS.length,
        sql: "-- supplied by the benchmark fixture",
        params: [],
      } as never;
    },
  };
}
