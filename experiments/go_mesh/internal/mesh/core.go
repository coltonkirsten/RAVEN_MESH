// Package mesh implements the in-process router: envelope signing,
// edge ACL, request/response correlation, fan-out to SSE subscribers,
// and JSON-line audit. Network-attached nodes ride on top via HTTP
// handlers (see node_http.go).
package mesh

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"mesh_go/internal/crypto"
	"mesh_go/internal/manifest"
)

// Envelope is the wire shape. Stored as map for canonical-JSON
// signing parity with Python/Elixir.
type Envelope = map[string]any

// SurfaceHandler is invoked for incoming envelopes addressed to a
// specific node + surface. Return (payload, nil) for a successful
// reply; for fire-and-forget, the return is ignored.
type SurfaceHandler func(ctx context.Context, env Envelope) (map[string]any, error)

type registeredNode struct {
	decl     manifest.Node
	handlers map[string]SurfaceHandler
	connected bool
}

// Core owns mesh state. All mutating operations route through opCh
// so we never need mu around the hot path. (We do use sync.Map for
// SSE subscribers since they fan-in/out independently.)
type Core struct {
	opCh chan func(*coreState)

	subs   sync.Map // chan Envelope -> struct{}
	audit  *AuditLog
	cache  *SchemaCache

	stopCh chan struct{}
	wg     sync.WaitGroup
}

type coreState struct {
	nodes map[string]*registeredNode
	edges map[manifest.Edge]bool
}

func New(audit *AuditLog) *Core {
	c := &Core{
		opCh:   make(chan func(*coreState), 64),
		audit:  audit,
		cache:  NewSchemaCache(),
		stopCh: make(chan struct{}),
	}
	st := &coreState{
		nodes: make(map[string]*registeredNode),
		edges: make(map[manifest.Edge]bool),
	}
	c.wg.Add(1)
	go func() {
		defer c.wg.Done()
		for {
			select {
			case op := <-c.opCh:
				op(st)
			case <-c.stopCh:
				return
			}
		}
	}()
	return c
}

func (c *Core) Stop() {
	close(c.stopCh)
	c.wg.Wait()
}

func (c *Core) do(op func(*coreState)) {
	done := make(chan struct{})
	c.opCh <- func(st *coreState) {
		op(st)
		close(done)
	}
	<-done
}

// LoadManifest registers all nodes + edges from a parsed manifest.
// Network nodes start without a handler — they connect later via
// RegisterHandler from the HTTP node-side adapter.
func (c *Core) LoadManifest(m *manifest.Manifest) {
	c.do(func(st *coreState) {
		for i := range m.Nodes {
			n := m.Nodes[i]
			st.nodes[n.ID] = &registeredNode{
				decl:     n,
				handlers: make(map[string]SurfaceHandler),
			}
		}
		for e := range m.Edges() {
			st.edges[e] = true
		}
	})
}

// RegisterNode adds a node + secret without going through a manifest.
// Useful for tests and for in-process echo nodes.
func (c *Core) RegisterNode(n manifest.Node) {
	c.do(func(st *coreState) {
		st.nodes[n.ID] = &registeredNode{
			decl:     n,
			handlers: make(map[string]SurfaceHandler),
		}
	})
}

// RegisterHandler wires a surface to its in-process handler and marks
// the node connected.
func (c *Core) RegisterHandler(nodeID, surface string, h SurfaceHandler) error {
	var rerr error
	c.do(func(st *coreState) {
		n, ok := st.nodes[nodeID]
		if !ok {
			rerr = fmt.Errorf("unknown node %q", nodeID)
			return
		}
		n.handlers[surface] = h
		n.connected = true
	})
	return rerr
}

// AddEdge inserts a single from->to relationship.
func (c *Core) AddEdge(from, to string) {
	c.do(func(st *coreState) {
		st.edges[manifest.Edge{From: from, To: to}] = true
	})
}

// SecretFor returns the registered secret for a node id (or "" if not
// found).
func (c *Core) SecretFor(id string) string {
	var s string
	c.do(func(st *coreState) {
		if n, ok := st.nodes[id]; ok {
			s = n.decl.Secret
		}
	})
	return s
}

// Invoke builds, signs, and routes an envelope on behalf of a node.
func (c *Core) Invoke(ctx context.Context, fromID, target string, payload map[string]any) (map[string]any, error) {
	secret := c.SecretFor(fromID)
	if secret == "" {
		return nil, fmt.Errorf("unknown_node: %s", fromID)
	}
	id := newID()
	env := Envelope{
		"id":             id,
		"correlation_id": id,
		"from":           fromID,
		"to":             target,
		"kind":           "invocation",
		"payload":        payload,
		"timestamp":      time.Now().UTC().Format(time.RFC3339Nano),
	}
	if err := crypto.AttachSignature(env, secret); err != nil {
		return nil, err
	}
	return c.Route(ctx, env, true)
}

// Route validates + delivers an externally-signed envelope.
func (c *Core) Route(ctx context.Context, env Envelope, sigPreverified bool) (map[string]any, error) {
	defer c.broadcast(env)

	fromID, _ := env["from"].(string)
	to, _ := env["to"].(string)

	var (
		fromDecl    *registeredNode
		targetDecl  *registeredNode
		surfaceName string
		surfaceDecl *manifest.Surface
		edgesAllow  bool
	)
	c.do(func(st *coreState) {
		fromDecl = st.nodes[fromID]
		parts := strings.SplitN(to, ".", 2)
		if len(parts) == 2 {
			surfaceName = parts[1]
			if td, ok := st.nodes[parts[0]]; ok {
				targetDecl = td
				for i := range td.decl.Surfaces {
					if td.decl.Surfaces[i].Name == surfaceName {
						surfaceDecl = &td.decl.Surfaces[i]
						break
					}
				}
			}
		}
		edgesAllow = st.edges[manifest.Edge{From: fromID, To: to}]
	})

	switch {
	case fromDecl == nil:
		env["_route_status"] = "unknown_node"
		return nil, errors.New("unknown_node")
	case !sigPreverified && !crypto.Verify(env, fromDecl.decl.Secret):
		env["_route_status"] = "bad_signature"
		return nil, errors.New("bad_signature")
	case targetDecl == nil || surfaceDecl == nil:
		env["_route_status"] = "unknown_target"
		return nil, errors.New("unknown_target")
	case !edgesAllow:
		env["_route_status"] = "denied_no_relationship"
		return nil, errors.New("denied_no_relationship")
	}

	// Validate payload against surface schema before dispatch.
	if payload, ok := env["payload"].(map[string]any); ok {
		if err := c.cache.Validate(surfaceDecl.Schema, payload); err != nil {
			env["_route_status"] = "schema_invalid"
			return nil, err
		}
	}

	handler, hasHandler := targetDecl.handlers[surfaceName]
	if !hasHandler {
		env["_route_status"] = "node_unreachable"
		return nil, errors.New("node_unreachable")
	}

	if surfaceDecl.InvocationMode == manifest.FireAndForget {
		go func() {
			ctx2, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			_, _ = handler(ctx2, env)
		}()
		env["_route_status"] = "accepted"
		_ = c.audit.Write("envelope.routed", map[string]any{"id": env["id"], "to": to, "mode": "fire_and_forget"})
		return map[string]any{"status": "accepted", "id": env["id"]}, nil
	}

	resp, err := handler(ctx, env)
	if err != nil {
		env["_route_status"] = "handler_error"
		_ = c.audit.Write("envelope.error", map[string]any{"id": env["id"], "to": to, "err": err.Error()})
		return nil, err
	}
	env["_route_status"] = "routed"
	_ = c.audit.Write("envelope.routed", map[string]any{"id": env["id"], "to": to, "mode": "request_response"})
	return resp, nil
}

// Subscribe returns a buffered channel of envelopes routed through Core.
// Caller must Unsubscribe to release resources.
func (c *Core) Subscribe() (<-chan Envelope, func()) {
	ch := make(chan Envelope, 32)
	c.subs.Store(ch, struct{}{})
	return ch, func() {
		c.subs.Delete(ch)
		close(ch)
	}
}

func (c *Core) broadcast(env Envelope) {
	c.subs.Range(func(k, _ any) bool {
		ch := k.(chan Envelope)
		select {
		case ch <- env:
		default:
			// Slow subscriber — drop rather than block the router.
		}
		return true
	})
}

// Introspect returns connected/declared state for the admin endpoint.
func (c *Core) Introspect() map[string]any {
	out := map[string]any{}
	c.do(func(st *coreState) {
		nodes := make([]map[string]any, 0, len(st.nodes))
		for _, n := range st.nodes {
			surfaces := make([]map[string]any, 0, len(n.decl.Surfaces))
			for _, s := range n.decl.Surfaces {
				surfaces = append(surfaces, map[string]any{
					"name":            s.Name,
					"type":            s.Type,
					"invocation_mode": string(s.InvocationMode),
				})
			}
			nodes = append(nodes, map[string]any{
				"id":        n.decl.ID,
				"kind":      n.decl.Kind,
				"connected": n.connected,
				"surfaces":  surfaces,
			})
		}
		edges := make([]map[string]string, 0, len(st.edges))
		for e := range st.edges {
			edges = append(edges, map[string]string{"from": e.From, "to": e.To})
		}
		out["nodes"] = nodes
		out["edges"] = edges
	})
	return out
}

func newID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}
