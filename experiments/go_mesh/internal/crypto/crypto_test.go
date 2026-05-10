package crypto

import (
	"strings"
	"testing"
)

func TestCanonicalDeterministic(t *testing.T) {
	env := map[string]any{
		"id":      "abc",
		"to":      "tasks.create",
		"from":    "voice_actor",
		"payload": map[string]any{"z": 1, "a": map[string]any{"k": "v", "b": 2}},
	}
	a, err := Canonical(env)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := Canonical(env)
	if string(a) != string(b) {
		t.Fatalf("canonical not deterministic")
	}
	// must sort keys; "from" < "id" < "payload" < "to"
	want := `{"from":"voice_actor","id":"abc","payload":{"a":{"b":2,"k":"v"},"z":1},"to":"tasks.create"}`
	if string(a) != want {
		t.Fatalf("canonical mismatch:\n got: %s\nwant: %s", a, want)
	}
}

func TestCanonicalStripsSignature(t *testing.T) {
	env := map[string]any{"id": "abc", "signature": "deadbeef"}
	out, _ := Canonical(env)
	if strings.Contains(string(out), "signature") {
		t.Fatalf("signature must not appear in canonical form: %s", out)
	}
}

func TestSignVerifyRoundTrip(t *testing.T) {
	secret := "hunter2"
	env := map[string]any{
		"id":      "abc",
		"from":    "voice_actor",
		"to":      "tasks.create",
		"kind":    "invocation",
		"payload": map[string]any{"title": "buy milk"},
	}
	if err := AttachSignature(env, secret); err != nil {
		t.Fatal(err)
	}
	if !Verify(env, secret) {
		t.Fatalf("verify failed for valid signature")
	}
	// tamper
	env["payload"].(map[string]any)["title"] = "sell milk"
	if Verify(env, secret) {
		t.Fatalf("verify accepted tampered payload")
	}
}

func TestVerifyWrongSecret(t *testing.T) {
	env := map[string]any{"id": "x"}
	_ = AttachSignature(env, "right")
	if Verify(env, "wrong") {
		t.Fatalf("verify accepted wrong secret")
	}
}

func TestVerifyMissingSignature(t *testing.T) {
	env := map[string]any{"id": "x"}
	if Verify(env, "any") {
		t.Fatalf("verify accepted envelope with no signature")
	}
}
