package main

import (
	"net/http"
	"os"
	"path/filepath"
)

// apiDocument serves the two public machine-readable contracts without
// exposing arbitrary files from the configured documentation directory.
func (s *server) apiDocument(w http.ResponseWriter, r *http.Request) {
	name := filepath.Base(r.URL.Path)
	contentType := "application/octet-stream"
	switch name {
	case "openapi.json":
		contentType = "application/json; charset=utf-8"
	case "agent-guide.md":
		contentType = "text/markdown; charset=utf-8"
	default:
		http.NotFound(w, r)
		return
	}

	docsDir := os.Getenv("API_DOCS_DIR")
	if docsDir == "" {
		docsDir = "docs"
		if _, err := os.Stat(docsDir); os.IsNotExist(err) {
			docsDir = filepath.Join("..", "docs")
		}
	}
	data, err := os.ReadFile(filepath.Join(docsDir, name))
	if err != nil {
		if os.IsNotExist(err) {
			http.NotFound(w, r)
			return
		}
		http.Error(w, "unable to read API document", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}
