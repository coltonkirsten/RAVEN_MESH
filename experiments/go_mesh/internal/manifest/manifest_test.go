package manifest

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, name, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestLoadDemo(t *testing.T) {
	p := writeFile(t, "demo.yaml", `
nodes:
  - id: voice
    kind: actor
    runtime: local-process
    identity_secret: env:VOICE_SECRET_TEST
    surfaces:
      - name: inbox
        type: inbox
        invocation_mode: fire_and_forget
        schema: ./schemas/voice.json
  - id: tasks
    kind: capability
    runtime: local-process
    surfaces:
      - name: create
        type: tool
        invocation_mode: request_response

relationships:
  - from: voice
    to: tasks.create
`)
	t.Setenv("VOICE_SECRET_TEST", "supersecret")
	m, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if len(m.Nodes) != 2 {
		t.Fatalf("expected 2 nodes, got %d", len(m.Nodes))
	}
	if m.FindNode("voice").Secret != "supersecret" {
		t.Fatalf("env secret not resolved: %q", m.FindNode("voice").Secret)
	}
	// Auto-derived secret deterministic and non-empty.
	if got := m.FindNode("tasks").Secret; len(got) != 64 {
		t.Fatalf("autogen secret should be 64 hex chars, got %d", len(got))
	}
	// Schema path should be absolute.
	s := m.FindNode("voice").Surfaces[0].Schema
	if !filepath.IsAbs(s) {
		t.Fatalf("schema path not absolute: %s", s)
	}
	// Edge set
	edges := m.Edges()
	if !edges[Edge{From: "voice", To: "tasks.create"}] {
		t.Fatalf("edge missing: %#v", edges)
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := Load("/no/such/file"); err == nil {
		t.Fatalf("expected error for missing file")
	}
}
