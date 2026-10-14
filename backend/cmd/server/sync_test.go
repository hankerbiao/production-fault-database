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

func TestSyncRequestIncludesTask(t *testing.T) {
	tests := []struct {
		name    string
		request syncRunRequest
		taskID  string
		want    bool
	}{
		{name: "all tasks", request: syncRunRequest{}, taskID: "sales_order_delivery_records", want: true},
		{name: "selected task", request: syncRunRequest{TaskIDs: []string{"sales_orders", "sales_order_delivery_records"}}, taskID: "sales_order_delivery_records", want: true},
		{name: "unrelated task", request: syncRunRequest{TaskIDs: []string{"sales_orders"}}, taskID: "sales_order_delivery_records", want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := syncRequestIncludesTask(test.request, test.taskID); got != test.want {
				t.Fatalf("syncRequestIncludesTask()=%t, want %t", got, test.want)
			}
		})
	}
}
