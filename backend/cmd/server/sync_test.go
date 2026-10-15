package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSyncScriptDirMissing(t *testing.T) {
	old := os.Getenv("SYNC_SCRIPT_DIR")
	t.Cleanup(func() { _ = os.Setenv("SYNC_SCRIPT_DIR", old) })
	_ = os.Setenv("SYNC_SCRIPT_DIR", filepath.Join(t.TempDir(), "missing"))
	if _, err := syncScriptDir(); err == nil {
		t.Fatal("expected missing script error")
	}
}
