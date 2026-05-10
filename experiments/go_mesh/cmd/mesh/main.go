// mesh-go: a single static binary with three subcommands:
//
//	mesh serve <manifest.yaml>  -- run the in-process mesh + HTTP API
//	mesh echo                   -- run an echo node (used by supervisor)
//	mesh supervise <manifest.yaml> -- spawn echo subprocesses for every node
//
// The split lets us demonstrate both in-process channel routing
// (serve) and subprocess supervision (supervise + echo).
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"mesh_go/internal/echonode"
	"mesh_go/internal/manifest"
	"mesh_go/internal/mesh"
	"mesh_go/internal/supervisor"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "serve":
		os.Exit(serve(os.Args[2:]))
	case "echo":
		os.Exit(echoCmd(os.Args[2:]))
	case "supervise":
		os.Exit(superviseCmd(os.Args[2:]))
	case "version":
		fmt.Println("mesh-go 0.1.0")
	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: mesh {serve|echo|supervise|version} ...")
}

func serve(args []string) int {
	if len(args) < 1 {
		fmt.Fprintln(os.Stderr, "usage: mesh serve <manifest.yaml> [addr]")
		return 2
	}
	addr := ":7777"
	if len(args) >= 2 {
		addr = args[1]
	}
	m, err := manifest.Load(args[0])
	if err != nil {
		log.Printf("manifest load: %v", err)
		return 1
	}
	auditPath := filepath.Join(filepath.Dir(args[0]), "audit.log")
	audit, err := mesh.OpenAudit(auditPath)
	if err != nil {
		log.Printf("audit open: %v", err)
		return 1
	}
	defer audit.Close()

	core := mesh.New(audit)
	defer core.Stop()
	core.LoadManifest(m)

	// Auto-attach in-process echo handlers for any node whose manifest
	// surfaces match the echo contract. This makes `mesh serve` useful
	// out-of-the-box for the demo manifest.
	echoer := echonode.New()
	for _, n := range m.Nodes {
		for _, s := range n.Surfaces {
			_ = core.RegisterHandler(n.ID, s.Name, echoer.Handle(s.Name))
		}
	}

	srv := mesh.NewServer(core)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	log.Printf("mesh serving on %s (manifest=%s, audit=%s)", addr, args[0], auditPath)
	if err := srv.Listen(ctx, addr); err != nil {
		log.Printf("listen: %v", err)
		return 1
	}
	return 0
}

func echoCmd(args []string) int {
	id := "echo"
	if len(args) >= 1 {
		id = args[0]
	}
	log.Printf("[echo:%s] running. ctrl-c to stop.", id)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	<-ctx.Done()
	return 0
}

func superviseCmd(args []string) int {
	if len(args) < 1 {
		fmt.Fprintln(os.Stderr, "usage: mesh supervise <manifest.yaml>")
		return 2
	}
	m, err := manifest.Load(args[0])
	if err != nil {
		log.Printf("manifest load: %v", err)
		return 1
	}
	exe, err := os.Executable()
	if err != nil {
		log.Printf("locate self: %v", err)
		return 1
	}

	sup := supervisor.New(5, 5*time.Second)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	for _, n := range m.Nodes {
		spec := supervisor.Spec{
			ID:     n.ID,
			Cmd:    exe,
			Args:   []string{"echo", n.ID},
			Env:    os.Environ(),
			Stdout: os.Stdout,
			Stderr: os.Stderr,
		}
		if err := sup.Start(ctx, spec); err != nil {
			log.Printf("supervisor start %s: %v", n.ID, err)
		}
	}

	<-ctx.Done()
	for _, id := range sup.Children() {
		_ = sup.Stop(id)
	}
	return 0
}
