package mesh

import (
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"time"
)

// AuditLog is a JSON-lines append-only event log. Safe for concurrent use.
type AuditLog struct {
	mu sync.Mutex
	f  *os.File
}

func OpenAudit(path string) (*AuditLog, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open audit: %w", err)
	}
	return &AuditLog{f: f}, nil
}

func (a *AuditLog) Write(event string, payload map[string]any) error {
	if a == nil {
		return nil
	}
	rec := map[string]any{
		"ts":      time.Now().UTC().Format(time.RFC3339Nano),
		"event":   event,
		"payload": payload,
	}
	line, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if _, err := a.f.Write(append(line, '\n')); err != nil {
		return err
	}
	return nil
}

func (a *AuditLog) Close() error {
	if a == nil || a.f == nil {
		return nil
	}
	return a.f.Close()
}
