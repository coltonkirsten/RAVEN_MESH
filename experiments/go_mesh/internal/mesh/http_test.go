package mesh_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"mesh_go/internal/echonode"
	"mesh_go/internal/manifest"
	"mesh_go/internal/mesh"
)

func TestHTTPInvokeAndIntrospect(t *testing.T) {
	c := mesh.New(nil)
	defer c.Stop()
	c.RegisterNode(manifest.Node{ID: "from", Secret: "s1"})
	c.RegisterNode(manifest.Node{
		ID:     "to",
		Secret: "s2",
		Surfaces: []manifest.Surface{{Name: "echo", InvocationMode: manifest.RequestResponse}},
	})
	echo := echonode.New()
	_ = c.RegisterHandler("to", "echo", echo.Handle("echo"))
	c.AddEdge("from", "to.echo")

	srv := httptest.NewServer(mesh.NewServer(c).Handler())
	defer srv.Close()

	body := strings.NewReader(`{"from":"from","to":"to.echo","payload":{"hi":"world"}}`)
	resp, err := http.Post(srv.URL+"/v0/admin/invoke", "application/json", body)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if out["echo"] == nil {
		t.Fatalf("expected echoed payload, got %#v", out)
	}

	resp2, err := http.Get(srv.URL + "/v0/admin/introspect")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var intro map[string]any
	_ = json.NewDecoder(resp2.Body).Decode(&intro)
	nodes := intro["nodes"].([]any)
	if len(nodes) != 2 {
		t.Fatalf("expected 2 nodes in introspect, got %d", len(nodes))
	}
}

func TestHTTPSSETail(t *testing.T) {
	c := mesh.New(nil)
	defer c.Stop()
	c.RegisterNode(manifest.Node{ID: "from", Secret: "s1"})
	c.RegisterNode(manifest.Node{
		ID:     "to",
		Secret: "s2",
		Surfaces: []manifest.Surface{{Name: "echo", InvocationMode: manifest.RequestResponse}},
	})
	echo := echonode.New()
	_ = c.RegisterHandler("to", "echo", echo.Handle("echo"))
	c.AddEdge("from", "to.echo")

	srv := httptest.NewServer(mesh.NewServer(c).Handler())
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	req, _ := http.NewRequestWithContext(ctx, "GET", srv.URL+"/v0/admin/tail", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("expected SSE content-type, got %q", ct)
	}

	// Trigger an envelope after subscribing.
	go func() {
		time.Sleep(100 * time.Millisecond)
		_, _ = http.Post(srv.URL+"/v0/admin/invoke", "application/json",
			bytes.NewReader([]byte(`{"from":"from","to":"to.echo","payload":{}}`)))
	}()

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)
	saw := false
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "event: envelope") {
			saw = true
			break
		}
	}
	if !saw {
		t.Fatalf("never received envelope event over SSE")
	}
}
