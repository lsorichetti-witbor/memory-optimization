export interface Memory {
  id: string;
  memory: string;
  user_id?: string;
  agent_id?: string;
  created_at?: string;
  updated_at?: string;
  /**
   * Everything outside the reserved payload keys. A memory written with no
   * agent_id still carries its scope here, so the table can show where it
   * belongs instead of rendering an empty cell.
   */
  metadata?: Record<string, unknown> | null;
}

export interface ApiKey {
  id: string;
  label: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ApiKeyCreateResponse {
  id: string;
  label: string;
  key: string;
  key_prefix: string;
  created_at: string;
}

export interface ApiRequestLog {
  id: string;
  created_at: string;
  method: string;
  path: string;
  status_code: number;
  latency_ms: number;
  auth_type: string;
}

/**
 * `user`, `agent` and `run` are Mem0's own identifiers. `scope` is derived from
 * the scope/scope_key metadata, and exists because the three identifiers cannot
 * express every grouping: a global-scope memory carries only a user_id, so it
 * had no entity of its own and was visible only inside the user's total.
 */
export type EntityType = "user" | "agent" | "run" | "scope";

export interface Entity {
  id: string;
  type: EntityType;
  total_memories: number;
  created_at: string | null;
  updated_at: string | null;
}
