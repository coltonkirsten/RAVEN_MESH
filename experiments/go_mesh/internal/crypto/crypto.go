// Package crypto implements wire-compatible HMAC-SHA256 envelope signing.
//
// Canonical form mirrors the Python prototype:
//
//	json.dumps(envelope_minus_signature, sort_keys=True, separators=(",", ":"))
//
// We hand-roll the canonical encoder so that map key order is deterministic
// regardless of Go's randomized map iteration.
package crypto

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// Canonical returns the byte form used as the HMAC input. The "signature"
// key is stripped before encoding.
func Canonical(envelope map[string]any) ([]byte, error) {
	clone := make(map[string]any, len(envelope))
	for k, v := range envelope {
		if k == "signature" {
			continue
		}
		clone[k] = v
	}
	var b strings.Builder
	if err := encodeValue(&b, clone); err != nil {
		return nil, err
	}
	return []byte(b.String()), nil
}

// Sign returns hex-encoded HMAC-SHA256 of the canonical envelope.
func Sign(envelope map[string]any, secret string) (string, error) {
	body, err := Canonical(envelope)
	if err != nil {
		return "", err
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil)), nil
}

// Verify compares the envelope's "signature" against a recomputed HMAC.
func Verify(envelope map[string]any, secret string) bool {
	sigVal, ok := envelope["signature"].(string)
	if !ok {
		return false
	}
	expected, err := Sign(envelope, secret)
	if err != nil {
		return false
	}
	return hmac.Equal([]byte(sigVal), []byte(expected))
}

// AttachSignature mutates envelope to include a fresh "signature" field.
func AttachSignature(envelope map[string]any, secret string) error {
	sig, err := Sign(envelope, secret)
	if err != nil {
		return err
	}
	envelope["signature"] = sig
	return nil
}

func encodeValue(b *strings.Builder, v any) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		raw, err := json.Marshal(x)
		if err != nil {
			return err
		}
		b.Write(raw)
	case int:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case float64:
		// Match python's repr for whole-number floats: 1.0 -> "1.0", but
		// our envelopes only use json.Number / int values, so this is
		// only invoked on payload data.
		raw, err := json.Marshal(x)
		if err != nil {
			return err
		}
		b.Write(raw)
	case json.Number:
		b.WriteString(x.String())
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			raw, err := json.Marshal(k)
			if err != nil {
				return err
			}
			b.Write(raw)
			b.WriteByte(':')
			if err := encodeValue(b, x[k]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := encodeValue(b, item); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	default:
		return fmt.Errorf("crypto.canonical: unsupported value type %T", v)
	}
	return nil
}
