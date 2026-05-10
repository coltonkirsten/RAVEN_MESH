export type Surface = {
  name: string;
  type: "tool" | "inbox";
  invocation_mode: "request_response" | "fire_and_forget";
  schema: Record<string, any>;
};

export type Node = {
  id: string;
  kind: "actor" | "capability" | "approval" | "hybrid" | string;
  runtime: string;
  metadata: Record<string, any>;
  connected: boolean;
  surfaces: Surface[];
};

export type Relationship = { from: string; to: string };

export type EnvelopeEvent = {
  ts: string;
  direction: "in" | "out";
  from_node: string | null;
  to_surface: string | null;
  msg_id: string | null;
  correlation_id: string | null;
  kind: string | null;
  payload: any;
  wrapped: any;
  signature_valid: boolean;
  route_status: string;
};

export type AdminState = {
  manifest_path: string;
  audit_path: string;
  nodes: Node[];
  relationships: Relationship[];
  envelope_tail: EnvelopeEvent[];
};
