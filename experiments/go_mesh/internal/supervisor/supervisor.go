// Package supervisor manages worker subprocesses with bounded restart.
// It is the Go analogue of Elixir's DynamicSupervisor with
// {strategy: :one_for_one, max_restarts: N, max_seconds: T}.
package supervisor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os/exec"
	"sync"
	"time"
)

type Spec struct {
	ID      string
	Cmd     string
	Args    []string
	Env     []string
	Stdout  io.Writer
	Stderr  io.Writer
	OnStart func()
	OnExit  func(error)
}

type Supervisor struct {
	maxRestarts int
	window      time.Duration

	mu       sync.Mutex
	children map[string]*childState
}

type childState struct {
	spec        Spec
	cancel      context.CancelFunc
	restartLog  []time.Time
	stopped     bool
	lastErr     error
	restartCount int
}

func New(maxRestarts int, window time.Duration) *Supervisor {
	if maxRestarts <= 0 {
		maxRestarts = 5
	}
	if window <= 0 {
		window = 5 * time.Second
	}
	return &Supervisor{
		maxRestarts: maxRestarts,
		window:      window,
		children:    make(map[string]*childState),
	}
}

// Start launches a subprocess and supervises it. If the process exits
// with non-zero (or any error), it's relaunched up to maxRestarts in
// the rolling window before being given up on.
func (s *Supervisor) Start(parent context.Context, spec Spec) error {
	s.mu.Lock()
	if _, dup := s.children[spec.ID]; dup {
		s.mu.Unlock()
		return fmt.Errorf("already supervised: %s", spec.ID)
	}
	ctx, cancel := context.WithCancel(parent)
	st := &childState{spec: spec, cancel: cancel}
	s.children[spec.ID] = st
	s.mu.Unlock()

	go s.loop(ctx, st)
	return nil
}

// Stop terminates a supervised subprocess and removes it.
func (s *Supervisor) Stop(id string) error {
	s.mu.Lock()
	st, ok := s.children[id]
	if !ok {
		s.mu.Unlock()
		return fmt.Errorf("not supervised: %s", id)
	}
	st.stopped = true
	st.cancel()
	delete(s.children, id)
	s.mu.Unlock()
	return nil
}

// Children returns currently-running ids.
func (s *Supervisor) Children() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.children))
	for id := range s.children {
		out = append(out, id)
	}
	return out
}

// RestartCount exposes restart count for an id (for tests).
func (s *Supervisor) RestartCount(id string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	if st, ok := s.children[id]; ok {
		return st.restartCount
	}
	return -1
}

func (s *Supervisor) loop(ctx context.Context, st *childState) {
	for {
		err := s.runOnce(ctx, st)

		s.mu.Lock()
		stopped := st.stopped
		s.mu.Unlock()
		if stopped || ctx.Err() != nil {
			return
		}

		s.mu.Lock()
		st.lastErr = err
		allow := s.allowRestartLocked(st)
		if allow {
			st.restartCount++
		} else {
			delete(s.children, st.spec.ID)
		}
		s.mu.Unlock()

		if !allow {
			log.Printf("[supervisor] %s: restart budget exhausted, last err=%v", st.spec.ID, err)
			if st.spec.OnExit != nil {
				st.spec.OnExit(err)
			}
			return
		}
		log.Printf("[supervisor] %s exited (%v); restarting", st.spec.ID, err)
		// Brief backoff to avoid tight crash loops in test scenarios.
		time.Sleep(50 * time.Millisecond)
	}
}

func (s *Supervisor) runOnce(ctx context.Context, st *childState) error {
	cmd := exec.CommandContext(ctx, st.spec.Cmd, st.spec.Args...)
	cmd.Env = st.spec.Env
	if st.spec.Stdout != nil {
		cmd.Stdout = st.spec.Stdout
	}
	if st.spec.Stderr != nil {
		cmd.Stderr = st.spec.Stderr
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start: %w", err)
	}
	if st.spec.OnStart != nil {
		st.spec.OnStart()
	}
	if err := cmd.Wait(); err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			return fmt.Errorf("exit %d", ee.ExitCode())
		}
		return err
	}
	return errors.New("exit 0")
}

// allowRestartLocked enforces "no more than N restarts in window". Caller
// must hold s.mu.
func (s *Supervisor) allowRestartLocked(st *childState) bool {
	now := time.Now()
	cutoff := now.Add(-s.window)
	pruned := st.restartLog[:0]
	for _, t := range st.restartLog {
		if t.After(cutoff) {
			pruned = append(pruned, t)
		}
	}
	if len(pruned) >= s.maxRestarts {
		st.restartLog = pruned
		return false
	}
	st.restartLog = append(pruned, now)
	return true
}
