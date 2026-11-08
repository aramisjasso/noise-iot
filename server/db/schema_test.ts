// Tests schema.sql on PGlite (Postgres compiled to WASM), so no Docker or
// Supabase project is needed. Run from the repo root:
//   deno test --allow-read --allow-env server/

import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.13";
import { PGlite } from "npm:@electric-sql/pglite@0.2.17";
import contract from "../../contracts/message.schema.json" with { type: "json" };

const schema = await Deno.readTextFile(new URL("./schema.sql", import.meta.url));
const example = contract.examples[0];

// Mimic the roles of a Supabase project. With exposeNewTables (the dashboard
// option "Automatically expose new tables") every role gets all privileges on
// new tables, so the tests prove that RLS, not missing grants, keeps anon out.
// Without it, roles get nothing by default.
async function newDb({ exposeNewTables = true } = {}): Promise<PGlite> {
  const db = new PGlite();
  await db.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin bypassrls;
    grant usage on schema public to anon, authenticated, service_role;
  `);
  if (exposeNewTables) {
    await db.exec(`
      alter default privileges in schema public
        grant all on tables to anon, authenticated, service_role;
      alter default privileges in schema public
        grant execute on functions to anon, authenticated, service_role;
    `);
  }
  await db.exec(schema);
  return db;
}

async function ingest(db: PGlite, payload: unknown): Promise<string> {
  const res = await db.query<{ r: string }>(
    "select ingest_batch($1::jsonb) as r",
    [JSON.stringify(payload)],
  );
  return res.rows[0].r;
}

async function count(db: PGlite, table: string): Promise<number> {
  const res = await db.query<{ n: number }>(`select count(*)::int as n from ${table}`);
  return res.rows[0].n;
}

Deno.test("schema.sql can be applied twice", async () => {
  const db = await newDb();
  await db.exec(schema);
  await db.close();
});

Deno.test("stores one batch as 1 + 600 + 60 rows", async () => {
  const db = await newDb();
  assertEquals(await ingest(db, example), "stored");
  assertEquals(await count(db, "batches"), 1);
  assertEquals(await count(db, "noise_levels"), 600);
  assertEquals(await count(db, "climate"), 60);

  const b = await db.query<{ transport: string; firmware_version: string }>(
    "select transport, firmware_version from batches",
  );
  assertEquals(b.rows[0], { transport: "wifi", firmware_version: "0.1.0" });

  // Chunk i lands at window_start + i seconds; null stays null.
  const levels = await db.query<{ i: number; level_db: number | null }>(`
    select extract(epoch from ts - '2026-11-05T18:00:00Z')::int as i, level_db
    from noise_levels order by ts`);
  assertEquals(levels.rows[0], { i: 0, level_db: 52 });
  assertEquals(levels.rows[599].i, 599);
  const nulls = levels.rows.filter((r) => r.level_db === null).map((r) => r.i);
  assertEquals(nulls, [example.levels_db.indexOf(null)]);

  const clim = await db.query<{ i: number; temp_c: number | null; rh: number | null }>(`
    select extract(epoch from ts - '2026-11-05T18:00:00Z')::int as i, temp_c, rh
    from climate order by ts`);
  assertEquals(clim.rows[3], { i: 30, temp_c: null, rh: null });
  assertEquals(clim.rows[59].i, 590);
  await db.close();
});

Deno.test("a resend is a duplicate and adds nothing", async () => {
  const db = await newDb();
  assertEquals(await ingest(db, example), "stored");
  assertEquals(await ingest(db, example), "duplicate");
  assertEquals(await count(db, "batches"), 1);
  assertEquals(await count(db, "noise_levels"), 600);
  await db.close();
});

Deno.test("a reused seq with another window is a conflict and stores nothing", async () => {
  const db = await newDb();
  await ingest(db, example);
  const reused = { ...example, window_start: "2026-11-05T18:30:00Z" };
  assertEquals(await ingest(db, reused), "conflict");
  assertEquals(await count(db, "noise_levels"), 600);
  await db.close();
});

Deno.test("next window of the same node is stored", async () => {
  const db = await newDb();
  await ingest(db, example);
  const next = { ...example, seq: 43, window_start: "2026-11-05T18:30:00Z" };
  assertEquals(await ingest(db, next), "stored");
  assertEquals(await count(db, "noise_levels"), 1200);
  await db.close();
});

Deno.test("anon can neither read, write nor call ingest_batch", async () => {
  const db = await newDb();
  await ingest(db, example);
  await db.exec("set role anon");
  assertEquals(await count(db, "noise_levels"), 0);
  await assertRejects(() =>
    db.query("insert into batches (node_id, seq, window_start, firmware_version) values ('x', 1, now(), 'v')")
  );
  await assertRejects(() => ingest(db, { ...example, seq: 99 }), Error, "permission denied");
  await db.exec("reset role");
  await db.close();
});

for (const exposeNewTables of [true, false]) {
  Deno.test(`service_role can store and resend (exposeNewTables=${exposeNewTables})`, async () => {
    const db = await newDb({ exposeNewTables });
    await db.exec("set role service_role");
    assertEquals(await ingest(db, example), "stored");
    assertEquals(await ingest(db, example), "duplicate");
    await db.exec("reset role");
    await db.close();
  });
}

Deno.test("anon is locked out when new tables are not exposed", async () => {
  const db = await newDb({ exposeNewTables: false });
  await db.exec("set role anon");
  await assertRejects(() => db.query("select * from batches"), Error, "permission denied");
  await assertRejects(() => ingest(db, example), Error, "permission denied");
  await db.exec("reset role");
  await db.close();
});
