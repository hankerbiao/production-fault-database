package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSyncManagerStartsAndPreventsConcurrentRuns(t *testing.T) {
	m := newSyncManager()
	old := os.Getenv("SYNC_SCRIPT_DIR")
	t.Cleanup(func() {
		_ = os.Setenv("SYNC_SCRIPT_DIR", old)
	})
	dir := t.TempDir()
	for _, name := range []string{"scripts/sync/sync_sales_orders.py", "scripts/sync/增量同步和清洗维修故障记录.py", "scripts/sync/station_records.py", "scripts/sync/order_bom_postings.py", "scripts/sync/serial_bindings.py"} {
		path := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte("import time; time.sleep(0.04); print('{\"success\": true}')"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	_ = os.Setenv("SYNC_SCRIPT_DIR", dir)
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
			if len(got.Summary) != 5 {
				t.Fatalf("expected five pipeline summaries, got=%v", got.Summary)
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("sync manager did not finish")
}

func TestSyncScriptDirMissing(t *testing.T) {
	old := os.Getenv("SYNC_SCRIPT_DIR")
	t.Cleanup(func() { _ = os.Setenv("SYNC_SCRIPT_DIR", old) })
	_ = os.Setenv("SYNC_SCRIPT_DIR", filepath.Join(t.TempDir(), "missing"))
	if _, err := syncScriptDir(); err == nil {
		t.Fatal("expected missing script error")
	}
}

func TestSyncManagerStopsAfterFailedStage(t *testing.T) {
	old := os.Getenv("SYNC_SCRIPT_DIR")
	t.Cleanup(func() { _ = os.Setenv("SYNC_SCRIPT_DIR", old) })
	dir := t.TempDir()
	scripts := map[string]string{
		"scripts/sync/sync_sales_orders.py":  "import sys; print('{\"success\": true}')",
		"scripts/sync/增量同步和清洗维修故障记录.py":      "print('{\"success\": true}')",
		"scripts/sync/station_records.py":    "import sys; print('{\"success\": false}'); sys.exit(1)",
		"scripts/sync/order_bom_postings.py": "print('{\"success\": true}')",
		"scripts/sync/serial_bindings.py":    "print('{\"success\": true}')",
	}
	for name, content := range scripts {
		path := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	_ = os.Setenv("SYNC_SCRIPT_DIR", dir)
	_ = os.Setenv("SYNC_PYTHON", "python3")
	m := newSyncManager()
	if _, err := m.start(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		status := m.statusSnapshot()
		if status.State != "running" {
			if status.State != "failed" {
				t.Fatalf("expected failed status, got %+v", status)
			}
			if len(status.Summary) != 2 {
				t.Fatalf("expected only prerequisite and failed stage, got %v", status.Summary)
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("sync manager did not finish")
}

func TestSyncCommandsUseTheSingleRepairWorkflow(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"scripts/sync/sync_sales_orders.py", "scripts/sync/增量同步和清洗维修故障记录.py", "scripts/sync/station_records.py", "scripts/sync/order_bom_postings.py", "scripts/sync/serial_bindings.py"} {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("SYNC_SCRIPT_DIR", root)
	commands, err := syncCommands()
	if err != nil {
		t.Fatal(err)
	}
	if commands[0].path != filepath.Join(root, "scripts", "sync", "sync_sales_orders.py") || len(commands[0].args) != 0 {
		t.Fatalf("sales command=%+v", commands[0])
	}
	if commands[2].path != filepath.Join(root, "scripts", "sync", "增量同步和清洗维修故障记录.py") || len(commands[2].args) != 4 || commands[2].args[0] != "--apply" || commands[2].args[1] != "--no-progress" || commands[2].args[2] != "--log-level" || commands[2].args[3] != "ERROR" {
		t.Fatalf("repair command=%+v", commands[2])
	}
}
