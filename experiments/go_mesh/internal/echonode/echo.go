// Package echonode is the trivial reference node: it echoes payloads
// back. Used both in-process for tests and as a subcommand spawned
// by the supervisor over a real socket.
package echonode

import (
	"context"
	"fmt"
	"sync/atomic"

	"mesh_go/internal/mesh"
)

type Echo struct {
	seen atomic.Int64
}

func New() *Echo { return &Echo{} }

// Handle implements mesh.SurfaceHandler with three contracts:
//
//	echo  -> {"echo": payload, "seen": N}
//	inbox -> swallowed (returns empty)
//	crash -> deliberate panic for supervisor tests
func (e *Echo) Handle(surface string) mesh.SurfaceHandler {
	switch surface {
	case "echo":
		return func(_ context.Context, env mesh.Envelope) (map[string]any, error) {
			n := e.seen.Add(1)
			return map[string]any{
				"echo": env["payload"],
				"seen": n,
			}, nil
		}
	case "inbox":
		return func(_ context.Context, _ mesh.Envelope) (map[string]any, error) {
			e.seen.Add(1)
			return map[string]any{}, nil
		}
	case "crash":
		return func(_ context.Context, _ mesh.Envelope) (map[string]any, error) {
			panic("intentional crash")
		}
	default:
		return func(_ context.Context, _ mesh.Envelope) (map[string]any, error) {
			return nil, fmt.Errorf("unknown_surface: %s", surface)
		}
	}
}

// Seen returns the number of envelopes processed (for tests).
func (e *Echo) Seen() int64 { return e.seen.Load() }
