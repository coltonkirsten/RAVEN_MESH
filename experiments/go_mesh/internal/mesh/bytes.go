package mesh

import "bytes"

// bytesReader is a tiny shim so schema.go doesn't grow an import block
// just for io.Reader from a byte slice.
func bytesReader(b []byte) *bytes.Reader { return bytes.NewReader(b) }
