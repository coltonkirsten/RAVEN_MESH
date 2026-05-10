package mesh_test

import (
	"context"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"mesh_go/internal/crypto"
	"mesh_go/internal/echonode"
	"mesh_go/internal/manifest"
	"mesh_go/internal/mesh"
)

type Envelope = mesh.Envelope
type Core = mesh.Core

func newTestCore(t *testing.T) *Core {
	t.Helper()
	c := mesh.New(nil)
	t.Cleanup(c.Stop)
	return c
}

func registerEcho(t *testing.T, c *Core, id string, surfaces []string) {
	t.Helper()
	n := manifest.Node{
		ID:     id,
		Kind:   "capability",
		Secret: "secret-" + id,
	}
	for _, s := range surfaces {
		n.Surfaces = append(n.Surfaces, manifest.Surface{
			Name:           s,
			Type:           "tool",
			InvocationMode: manifest.RequestResponse,
		})
	}
	c.RegisterNode(n)
	echo := echonode.New()
	for _, s := range surfaces {
		if err := c.RegisterHandler(id, s, echo.Handle(s)); err != nil {
			t.Fatal(err)
		}
	}
}

func TestRouteRequestResponse(t *testing.T) {
	c := newTestCore(t)
	registerEcho(t, c, "from", []string{"echo"})
	registerEcho(t, c, "to", []string{"echo"})
	c.AddEdge("from", "to.echo")

	resp, err := c.Invoke(context.Background(), "from", "to.echo", map[string]any{"x": "hi"})
	if err != nil {
		t.Fatalf("invoke: %v", err)
	}
	got, ok := resp["echo"].(map[string]any)
	if !ok || got["x"] != "hi" {
		t.Fatalf("unexpected response: %#v", resp)
	}
}

func TestRouteEdgeDenied(t *testing.T) {
	c := newTestCore(t)
	registerEcho(t, c, "a", []string{"echo"})
	registerEcho(t, c, "b", []string{"echo"})
	// no AddEdge — must be rejected
	_, err := c.Invoke(context.Background(), "a", "b.echo", map[string]any{})
	if err == nil || err.Error() != "denied_no_relationship" {
		t.Fatalf("expected denied_no_relationship, got %v", err)
	}
}

func TestRouteBadSignature(t *testing.T) {
	c := newTestCore(t)
	registerEcho(t, c, "a", []string{"echo"})
	registerEcho(t, c, "b", []string{"echo"})
	c.AddEdge("a", "b.echo")

	env := Envelope{
		"id":      "x",
		"from":    "a",
		"to":      "b.echo",
		"kind":    "invocation",
		"payload": map[string]any{},
	}
	_ = crypto.AttachSignature(env, "wrong-secret")
	_, err := c.Route(context.Background(), env, false)
	if err == nil || err.Error() != "bad_signature" {
		t.Fatalf("expected bad_signature, got %v", err)
	}
}

func TestRouteFireAndForget(t *testing.T) {
	c := newTestCore(t)
	from := manifest.Node{ID: "from", Secret: "s1"}
	to := manifest.Node{
		ID:     "to",
		Secret: "s2",
		Surfaces: []manifest.Surface{
			{Name: "inbox", InvocationMode: manifest.FireAndForget},
		},
	}
	c.RegisterNode(from)
	c.RegisterNode(to)
	echo := echonode.New()
	if err := c.RegisterHandler("to", "inbox", echo.Handle("inbox")); err != nil {
		t.Fatal(err)
	}
	c.AddEdge("from", "to.inbox")

	resp, err := c.Invoke(context.Background(), "from", "to.inbox", map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	if resp["status"] != "accepted" {
		t.Fatalf("expected accepted, got %#v", resp)
	}
	// Wait for the goroutine to run.
	deadline := time.Now().Add(time.Second)
	for echo.Seen() == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if echo.Seen() == 0 {
		t.Fatalf("echo handler never ran")
	}
}

func TestSchemaValidation(t *testing.T) {
	c := newTestCore(t)
	abs, _ := filepath.Abs("../../schemas/echo.json")
	from := manifest.Node{ID: "from", Secret: "s1"}
	to := manifest.Node{
		ID:     "to",
		Secret: "s2",
		Surfaces: []manifest.Surface{
			{Name: "echo", InvocationMode: manifest.RequestResponse, Schema: abs},
		},
	}
	c.RegisterNode(from)
	c.RegisterNode(to)
	echo := echonode.New()
	_ = c.RegisterHandler("to", "echo", echo.Handle("echo"))
	c.AddEdge("from", "to.echo")

	// echo schema is permissive `{type: object, additionalProperties: true}`,
	// so an object payload validates and a string payload fails.
	if _, err := c.Invoke(context.Background(), "from", "to.echo", map[string]any{"a": 1}); err != nil {
		t.Fatalf("valid payload rejected: %v", err)
	}
}

func TestSubscribeSeesEnvelopes(t *testing.T) {
	c := newTestCore(t)
	registerEcho(t, c, "a", []string{"echo"})
	registerEcho(t, c, "b", []string{"echo"})
	c.AddEdge("a", "b.echo")

	ch, unsub := c.Subscribe()
	defer unsub()

	var wg sync.WaitGroup
	wg.Add(1)
	var got Envelope
	go func() {
		defer wg.Done()
		select {
		case env := <-ch:
			got = env
		case <-time.After(time.Second):
			t.Errorf("subscriber timeout")
		}
	}()

	if _, err := c.Invoke(context.Background(), "a", "b.echo", map[string]any{}); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	if got["from"] != "a" || got["to"] != "b.echo" {
		t.Fatalf("unexpected envelope: %#v", got)
	}
}
