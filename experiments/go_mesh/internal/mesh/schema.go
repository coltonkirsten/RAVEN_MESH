package mesh

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

// SchemaCache compiles each schema once and caches by absolute path.
// jsonschema/v6 expects URL-style locations, so we register file
// contents as in-memory resources and key them by file path.
type SchemaCache struct {
	mu       sync.Mutex
	compiler *jsonschema.Compiler
	schemas  map[string]*jsonschema.Schema
}

func NewSchemaCache() *SchemaCache {
	return &SchemaCache{
		compiler: jsonschema.NewCompiler(),
		schemas:  make(map[string]*jsonschema.Schema),
	}
}

func (c *SchemaCache) Validate(schemaPath string, payload any) error {
	if schemaPath == "" {
		return nil
	}
	abs, err := filepath.Abs(schemaPath)
	if err != nil {
		return fmt.Errorf("schema path: %w", err)
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	sch, ok := c.schemas[abs]
	if !ok {
		raw, err := os.ReadFile(abs)
		if err != nil {
			return fmt.Errorf("read schema %s: %w", abs, err)
		}
		doc, err := jsonschema.UnmarshalJSON(bytesReader(raw))
		if err != nil {
			return fmt.Errorf("parse schema %s: %w", abs, err)
		}
		url := "file://" + abs
		if err := c.compiler.AddResource(url, doc); err != nil {
			return fmt.Errorf("register schema %s: %w", abs, err)
		}
		sch, err = c.compiler.Compile(url)
		if err != nil {
			return fmt.Errorf("compile schema %s: %w", abs, err)
		}
		c.schemas[abs] = sch
	}
	if err := sch.Validate(payload); err != nil {
		return fmt.Errorf("schema validation failed: %w", err)
	}
	return nil
}
