// Package manifest parses the YAML manifest format shared with the Python
// and Elixir prototypes.
package manifest

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

type InvocationMode string

const (
	RequestResponse InvocationMode = "request_response"
	FireAndForget   InvocationMode = "fire_and_forget"
)

type Surface struct {
	Name           string         `yaml:"name"`
	Type           string         `yaml:"type"`
	InvocationMode InvocationMode `yaml:"invocation_mode"`
	Schema         string         `yaml:"schema"`
}

type Node struct {
	ID             string            `yaml:"id"`
	Kind           string            `yaml:"kind"`
	Runtime        string            `yaml:"runtime"`
	IdentitySecret string            `yaml:"identity_secret"`
	Metadata       map[string]string `yaml:"metadata"`
	Surfaces       []Surface         `yaml:"surfaces"`
	Secret         string            `yaml:"-"`
}

type Edge struct {
	From string `yaml:"from"`
	To   string `yaml:"to"`
}

type Manifest struct {
	Nodes         []Node `yaml:"nodes"`
	Relationships []Edge `yaml:"relationships"`
	BaseDir       string `yaml:"-"`
}

// Load reads + parses a YAML manifest. Secrets are resolved from
// environment variables (env:NAME) or auto-derived for missing values.
func Load(path string) (*Manifest, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read manifest: %w", err)
	}
	var m Manifest
	if err := yaml.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("yaml parse: %w", err)
	}
	m.BaseDir = filepath.Dir(path)
	for i := range m.Nodes {
		m.Nodes[i].Secret = resolveSecret(m.Nodes[i])
		for j, s := range m.Nodes[i].Surfaces {
			if s.Schema != "" && !filepath.IsAbs(s.Schema) {
				m.Nodes[i].Surfaces[j].Schema = filepath.Join(m.BaseDir, s.Schema)
			}
			if m.Nodes[i].Surfaces[j].InvocationMode == "" {
				m.Nodes[i].Surfaces[j].InvocationMode = RequestResponse
			}
		}
	}
	return &m, nil
}

func resolveSecret(n Node) string {
	spec := n.IdentitySecret
	if strings.HasPrefix(spec, "env:") {
		if v := os.Getenv(strings.TrimPrefix(spec, "env:")); v != "" {
			return v
		}
	} else if spec != "" {
		return spec
	}
	sum := sha256.Sum256([]byte("mesh:" + n.ID + ":autogen"))
	return hex.EncodeToString(sum[:])
}

// Edges returns relationships as a {from, to} -> bool set.
func (m *Manifest) Edges() map[Edge]bool {
	out := make(map[Edge]bool, len(m.Relationships))
	for _, r := range m.Relationships {
		out[r] = true
	}
	return out
}

// FindNode returns the node declaration for an id.
func (m *Manifest) FindNode(id string) *Node {
	for i, n := range m.Nodes {
		if n.ID == id {
			return &m.Nodes[i]
		}
	}
	return nil
}
