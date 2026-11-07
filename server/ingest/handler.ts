// HTTP handler of the ingest function, independent of Supabase so it can be
// tested with a fake database.
//
// Response codes (the node only looks at 2xx vs. anything else):
//   200  batch stored, or already stored (resend from the outbox)
//   400  body is not JSON
//   401  missing or wrong x-ingest-key
//   405  method other than POST
//   409  (node_id, seq) already used for a different window_start
//   413  body larger than MAX_BODY_BYTES
//   422  JSON does not match contracts/message.schema.json
//   500  database error: the node keeps the batch and retries later

import { Ajv2020, type ErrorObject } from "npm:ajv@8.17.1/dist/2020.js";
import formatsPlugin from "npm:ajv-formats@3.0.1";
import contract from "../../contracts/message.schema.json" with { type: "json" };

// A full batch is ~5 KB; anything far bigger is not a node.
export const MAX_BODY_BYTES = 64 * 1024;

export type SaveResult = "stored" | "duplicate" | "conflict";

export interface HandlerOptions {
  ingestKey: string;
  saveBatch: (batch: Record<string, unknown>) => Promise<SaveResult>;
}

// ajv-formats is CommonJS: at runtime the default import is the plugin
// itself, but its types describe the module, hence the cast.
const addFormats = formatsPlugin as unknown as typeof formatsPlugin.default;

const ajv = new Ajv2020({ allErrors: true });
addFormats(ajv);
const validate = ajv.compile(contract);

function json(status: number, body: Record<string, unknown>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Constant-time comparison so the key cannot be guessed byte by byte.
function sameKey(given: string, expected: string): boolean {
  const a = new TextEncoder().encode(given);
  const b = new TextEncoder().encode(expected);
  let diff = a.length ^ b.length;
  for (let i = 0; i < b.length; i++) {
    diff |= (a[i] ?? 0) ^ b[i];
  }
  return diff === 0;
}

export function createHandler(opts: HandlerOptions) {
  if (!opts.ingestKey) {
    throw new Error("INGEST_KEY is not set");
  }

  return async function handle(req: Request): Promise<Response> {
    if (req.method !== "POST") {
      return json(405, { error: "method_not_allowed" });
    }

    if (!sameKey(req.headers.get("x-ingest-key") ?? "", opts.ingestKey)) {
      return json(401, { error: "unauthorized" });
    }

    const body = await req.text();
    if (new TextEncoder().encode(body).length > MAX_BODY_BYTES) {
      return json(413, { error: "payload_too_large" });
    }

    let batch: unknown;
    try {
      batch = JSON.parse(body);
    } catch {
      return json(400, { error: "invalid_json" });
    }

    if (!validate(batch)) {
      const details = (validate.errors ?? []).slice(0, 10).map((e: ErrorObject) => ({
        path: e.instancePath || "/",
        message: e.message,
      }));
      return json(422, { error: "schema_mismatch", details });
    }

    const { node_id, seq } = batch as { node_id: string; seq: number };

    let result: SaveResult;
    try {
      result = await opts.saveBatch(batch as Record<string, unknown>);
    } catch (err) {
      console.error(`ingest ${node_id}#${seq}: database error`, err);
      return json(500, { error: "database_error" });
    }

    if (result === "conflict") {
      console.warn(`ingest ${node_id}#${seq}: seq reused for another window`);
      return json(409, { error: "seq_conflict", node_id, seq });
    }

    return json(200, { status: result, node_id, seq });
  };
}
