// Supabase Edge Function: receives batches from the nodes and stores them.
//
// Node --HTTPS + JSON--> this function --rpc ingest_batch()--> PostgreSQL
//
// Environment:
//   INGEST_KEY                 shared key the nodes send in x-ingest-key
//                              (set with `supabase secrets set`)
//   SUPABASE_URL               provided by Supabase
//   SUPABASE_SERVICE_ROLE_KEY  provided by Supabase; never leaves the server

import { createClient } from "npm:@supabase/supabase-js@2.45.4";
import { createHandler, type SaveResult } from "./handler.ts";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

const handler = createHandler({
  ingestKey: Deno.env.get("INGEST_KEY") ?? "",
  saveBatch: async (batch) => {
    const { data, error } = await supabase.rpc("ingest_batch", { payload: batch });
    if (error) throw error;
    return data as SaveResult;
  },
});

Deno.serve(handler);
