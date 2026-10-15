package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"production-fault-gateway/internal/store"
)

func (s *server) exportRows(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Resource string   `json:"resource"`
		Keys     []string `json:"keys"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&body); err != nil {
		writeJSON(w, 400, map[string]string{"error": "invalid export request"})
		return
	}
	if body.Resource != "orders" && body.Resource != "faults" && body.Resource != "bindings" {
		writeJSON(w, 400, map[string]string{"error": "unknown export resource"})
		return
	}
	if len(body.Keys) > store.MaxExportKeys {
		writeJSON(w, 400, map[string]string{"error": "too many export keys"})
		return
	}
	for _, key := range body.Keys {
		if strings.TrimSpace(key) == "" || len(key) > 256 {
			writeJSON(w, 400, map[string]string{"error": "invalid export key"})
			return
		}
	}
	rows, err := s.store.ExportRows(r.Context(), body.Resource, body.Keys)
	if errors.Is(err, store.ErrExportBatchTooLarge) {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": err.Error()})
		return
	}
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": rows, "total": len(rows), "complete": true})
}

func (s *server) exportVersion(w http.ResponseWriter, r *http.Request) {
	version, err := s.store.ExportVersion(r.Context())
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, version)
}
