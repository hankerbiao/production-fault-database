package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSyncManagerStartsAndPreventsConcurrentRuns(t *testing.T) {
	m := newSyncManager()
	old := os.Getenv("SYNC_SCRIPT_PATH")
	oldViewDir := os.Getenv("SYNC_VIEW_SCRIPT_DIR")
	t.Cleanup(func() {
		_ = os.Setenv("SYNC_SCRIPT_PATH", old)
		_ = os.Setenv("SYNC_VIEW_SCRIPT_DIR", oldViewDir)
	})
	dir := t.TempDir()
	script := filepath.Join(dir, "sync.py")
	if err := os.WriteFile(script, []byte("import time; time.sleep(0.2)"), 0o600); err != nil {
		t.Fatal(err)
	}
	_ = os.Setenv("SYNC_VIEW_SCRIPT_DIR", dir)
	for _, name := range []string{"sync_zsgv_zsd124.py", "sync_zsgv_zpp_sernolist.py", "sync_z_v_zmes_t_001.py"} {
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	_ = os.Setenv("SYNC_SCRIPT_PATH", script)
	_ = os.Setenv("SYNC_PYTHON", "python3")
	status, err := m.start()
	if err != nil || status.State != "running" {
		t.Fatalf("start status=%+v err=%v", status, err)
	}
	if _, err := m.start(); err == nil {
		t.Fatal("expected concurrent start to fail")
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if got := m.statusSnapshot(); got.State != "running" {
			if got.State != "success" {
				t.Fatalf("final status=%+v", got)
			}
			if len(got.Summary) != 4 {
				t.Fatalf("expected four sync summaries, got=%v", got.Summary)
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("sync manager did not finish")
}

func TestSyncScriptPathMissing(t *testing.T) {
	old := os.Getenv("SYNC_SCRIPT_PATH")
	t.Cleanup(func() { _ = os.Setenv("SYNC_SCRIPT_PATH", old) })
	_ = os.Setenv("SYNC_SCRIPT_PATH", filepath.Join(t.TempDir(), "missing.py"))
	if _, err := syncScriptPath(); err == nil {
		t.Fatal("expected missing script error")
	}
}
