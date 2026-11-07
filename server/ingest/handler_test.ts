// Run from the repo root: deno test --allow-read server/

import { assertEquals } from "jsr:@std/assert@1.0.13";
import contract from "../../contracts/message.schema.json" with { type: "json" };
import { createHandler, type SaveResult } from "./handler.ts";

const KEY = "test-key";
const example = contract.examples[0];

function setup(result: SaveResult | Error = "stored") {
  const saved: Record<string, unknown>[] = [];
  const handle = createHandler({
    ingestKey: KEY,
    saveBatch: (batch) => {
      saved.push(batch);
      return result instanceof Error ? Promise.reject(result) : Promise.resolve(result);
    },
  });
  return { handle, saved };
}

function post(body: unknown, key: string | null = KEY): Request {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (key !== null) headers["x-ingest-key"] = key;
  return new Request("http://localhost/ingest", {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

Deno.test("stores the contract example", async () => {
  const { handle, saved } = setup("stored");
  const res = await handle(post(example));
  assertEquals(res.status, 200);
  assertEquals(await res.json(), { status: "stored", node_id: "tepic-01", seq: 42 });
  assertEquals(saved.length, 1);
});

Deno.test("a resend answers 200 so the node clears its outbox", async () => {
  const { handle } = setup("duplicate");
  const res = await handle(post(example));
  assertEquals(res.status, 200);
  assertEquals((await res.json()).status, "duplicate");
});

Deno.test("seq reused for another window is 409", async () => {
  const { handle } = setup("conflict");
  assertEquals((await handle(post(example))).status, 409);
});

Deno.test("rejects a missing or wrong key", async () => {
  const { handle, saved } = setup();
  assertEquals((await handle(post(example, null))).status, 401);
  assertEquals((await handle(post(example, "wrong"))).status, 401);
  assertEquals((await handle(post(example, KEY + "x"))).status, 401);
  assertEquals(saved.length, 0);
});

Deno.test("rejects methods other than POST", async () => {
  const { handle } = setup();
  const res = await handle(new Request("http://localhost/ingest", { method: "GET" }));
  assertEquals(res.status, 405);
});

Deno.test("rejects invalid JSON", async () => {
  const { handle } = setup();
  assertEquals((await handle(post("{not json"))).status, 400);
});

Deno.test("rejects oversized bodies", async () => {
  const { handle } = setup();
  assertEquals((await handle(post("x".repeat(70 * 1024)))).status, 413);
});

Deno.test("rejects batches that break the contract", async () => {
  const { handle, saved } = setup();
  const cases: Record<string, unknown>[] = [
    { ...example, levels_db: example.levels_db.slice(1) }, // 599 levels
    { ...example, window_start: "2026-11-05T18:00:00-06:00" }, // not UTC
    { ...example, window_start: "yesterday Z" }, // not a date-time
    { ...example, sample_rate_hz: 16000 },
    { ...example, seq: -1 },
    { ...example, extra: true },
    { ...example, climate: [{ offset_s: 600, temp_c: 24, rh: 60 }] },
  ];
  for (const bad of cases) {
    const res = await handle(post(bad));
    assertEquals(res.status, 422, JSON.stringify(Object.keys(bad)));
    assertEquals((await res.json()).error, "schema_mismatch");
  }
  const { node_id: _, ...noNode } = example;
  assertEquals((await handle(post(noNode))).status, 422);
  assertEquals(saved.length, 0);
});

Deno.test("database errors are 500 so the node retries", async () => {
  const { handle } = setup(new Error("connection refused"));
  const original = console.error;
  console.error = () => {};
  try {
    assertEquals((await handle(post(example))).status, 500);
  } finally {
    console.error = original;
  }
});
