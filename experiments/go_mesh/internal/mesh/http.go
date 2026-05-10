package mesh

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Server wires Core to HTTP routes:
//
//	POST /v0/route          — externally signed envelope, route as-is
//	POST /v0/admin/invoke   — sign with target node's secret + route
//	GET  /v0/admin/tail     — SSE stream of every envelope
//	GET  /v0/admin/introspect — dump nodes + edges
type Server struct {
	core *Core
	mux  *http.ServeMux
}

func NewServer(c *Core) *Server {
	s := &Server{core: c, mux: http.NewServeMux()}
	s.mux.HandleFunc("POST /v0/route", s.handleRoute)
	s.mux.HandleFunc("POST /v0/admin/invoke", s.handleInvoke)
	s.mux.HandleFunc("GET /v0/admin/tail", s.handleTail)
	s.mux.HandleFunc("GET /v0/admin/introspect", s.handleIntrospect)
	return s
}

func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) handleRoute(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "read_body", err)
		return
	}
	var env Envelope
	if err := json.Unmarshal(body, &env); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err)
		return
	}
	resp, err := s.core.Route(r.Context(), env, false)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleInvoke(w http.ResponseWriter, r *http.Request) {
	var req struct {
		From    string         `json:"from"`
		To      string         `json:"to"`
		Payload map[string]any `json:"payload"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err)
		return
	}
	resp, err := s.core.Invoke(r.Context(), req.From, req.To, req.Payload)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error(), nil)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleTail(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	ch, unsub := s.core.Subscribe()
	defer unsub()

	// initial flush so client knows we're alive
	fmt.Fprint(w, ": ok\n\n")
	flusher.Flush()

	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()

	for {
		select {
		case env, open := <-ch:
			if !open {
				return
			}
			data, _ := json.Marshal(env)
			fmt.Fprintf(w, "event: envelope\ndata: %s\n\n", data)
			flusher.Flush()
		case <-heartbeat.C:
			fmt.Fprint(w, ": ping\n\n")
			flusher.Flush()
		case <-r.Context().Done():
			return
		}
	}
}

func (s *Server) handleIntrospect(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.core.Introspect())
}

// Listen starts an HTTP server with graceful shutdown on ctx done.
func (s *Server) Listen(ctx context.Context, addr string) error {
	srv := &http.Server{Addr: addr, Handler: s.mux}
	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()
	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	case err := <-errCh:
		return err
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, reason string, err error) {
	out := map[string]any{"error": reason}
	if err != nil {
		out["details"] = err.Error()
	}
	writeJSON(w, code, out)
}
