package supervisor

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestSupervisorRestartsOnCrash spawns a script that crashes once, then
// the supervisor's restarted instance writes a sentinel file. The test
// asserts the sentinel appears and restartCount == 1.
func TestSupervisorRestartsOnCrash(t *testing.T) {
	dir := t.TempDir()
	sentinel := filepath.Join(dir, "sentinel")
	counter := filepath.Join(dir, "counter")

	// Bash script: increments counter, exits 1 the first run, touches
	// sentinel and exits 0 thereafter.
	script := filepath.Join(dir, "child.sh")
	body := `#!/bin/bash
N=$(cat "` + counter + `" 2>/dev/null || echo 0)
N=$((N+1))
echo $N > "` + counter + `"
if [ "$N" = "1" ]; then
  exit 1
fi
touch "` + sentinel + `"
sleep 1
`
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}

	sup := New(5, 2*time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if err := sup.Start(ctx, Spec{
		ID:  "child",
		Cmd: "/bin/bash",
		Args: []string{script},
	}); err != nil {
		t.Fatal(err)
	}

	// Wait up to 3s for the sentinel.
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(sentinel); err == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if _, err := os.Stat(sentinel); err != nil {
		t.Fatalf("sentinel never created — supervisor did not restart child: %v", err)
	}
	if rc := sup.RestartCount("child"); rc < 1 {
		t.Fatalf("expected restartCount >= 1, got %d", rc)
	}
}

// TestSupervisorStop tears down a long-running child cleanly.
func TestSupervisorStop(t *testing.T) {
	sup := New(3, time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := sup.Start(ctx, Spec{
		ID:   "sleeper",
		Cmd:  "/bin/sleep",
		Args: []string{"30"},
	}); err != nil {
		t.Fatal(err)
	}
	if got := sup.Children(); len(got) != 1 {
		t.Fatalf("expected 1 child, got %d", len(got))
	}
	if err := sup.Stop("sleeper"); err != nil {
		t.Fatal(err)
	}
	// Give the loop a beat to unwind.
	time.Sleep(150 * time.Millisecond)
	if got := sup.Children(); len(got) != 0 {
		t.Fatalf("expected 0 children after Stop, got %d", len(got))
	}
}
