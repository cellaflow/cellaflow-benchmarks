# The same Inconvo, with every model call leased

Identical driver, identical graph, identical sub-agents, identical Inconvo at
`fa63f29` as [the audit one level up](../). The driver is not even copied:
[`harness.ts`](harness.ts) spawns `../driver.ts` the way the audit spawns it,
and changes one environment variable.

That variable points `BENCH_PROVIDER_URL` at [`proxy.ts`](proxy.ts), a leasing
proxy that forwards to the same fake model endpoint. Every request through it
is leased and committed, keyed on the request body rather than on a counter.

```
                                  calls  retry  total  wasted
Inconvo alone
  control, one answer, no crash    15.0    0.0   15.0     0.0
  crash mid-answer, then retry      9.0   15.0   24.0     9.0

Inconvo + Cellaflow
  control, one answer, no crash    15.0    0.0   15.0     0.0
  crash mid-answer, then retry      7.0    9.0   16.0     1.0
```

Five runs per row. **The full write-up, including why the key comes from the
request body and why each call gets its own session, is in
[the audit's README](../#what-cellaflow-adds).** This directory is the second
arm of that measurement, not a separate finding.

## The one call still lost

Sixteen rather than fifteen. That call was in flight when the process died: the
provider had served it and the process was gone before the result committed. No
lease closes that window, because the window is before the first durable write.
What the record buys is shrinking it from the whole answer to one call.

## Run it

Set the audit arm up first, including the Inconvo clone and its `pnpm install`,
because this arm runs that arm's driver:

```bash
cd ..                                   # the audit arm
git clone https://github.com/inconvoai/inconvo
cd inconvo && git checkout fa63f29
corepack pnpm install --filter "@repo/agents..."
cd ..
npm install

cd with-cellaflow
docker compose up -d --wait             # the engine, the only service
npm install
npx tsx harness.ts
```

Their `package.json` requires node >= 22 and pins pnpm 11.1.2. `INCONVO_SRC`
overrides the clone location if it lives somewhere other than the audit arm's
directory.

No OpenAI account and no model spend: every call goes to the local fake
provider, through the proxy.

Model calls are counted from the provider's own append-and-fsync ledger,
written before any response is sent. The agent tree is never asked how much
work it did.
