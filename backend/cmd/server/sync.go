package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type syncManager struct {
	mu     sync.RWMutex
	status syncStatus
}

type syncStatus struct {
	State      string         `json:"state"`
	StartedAt  string         `json:"startedAt,omitempty"`
	FinishedAt string         `json:"finishedAt,omitempty"`
	Message    string         `json:"message,omitempty"`
	Summary    map[string]any `json:"summary,omitempty"`
}

func newSyncManager() *syncManager {
	return &syncManager{status: syncStatus{State: "idle", Message: "等待同步"}}
}

func (m *syncManager) statusSnapshot() syncStatus {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.status
}

func (m *syncManager) start() (syncStatus, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.status.State == "running" {
		return m.status, fmt.Errorf("已有同步任务运行中")
	}

	commands, err := syncCommands()
	if err != nil {
		return m.status, err
	}
	python := os.Getenv("SYNC_PYTHON")
	if python == "" {
		python = "python"
	}
	started := time.Now().UTC()
	m.status = syncStatus{State: "running", StartedAt: started.Format(time.RFC3339), Message: "正在执行增量同步"}

	go func() {
		summaries := make(map[string]any, len(commands))
		var runErr error
		for _, command := range commands {
			result, err := runSyncCommand(python, command.path, command.args)
			if result != nil {
				summaries[command.name] = result
			}
			if err != nil && runErr == nil {
				runErr = err
				break
			}
		}
		finished := time.Now().UTC()
		status := syncStatus{State: "success", StartedAt: started.Format(time.RFC3339), FinishedAt: finished.Format(time.RFC3339), Message: "增量同步完成"}
		if runErr != nil {
			status.State = "failed"
			status.Message = runErr.Error()
		}
		status.Summary = summaries
		for _, summary := range summaries {
			if value, ok := summary.(map[string]any); ok {
				if success, ok := value["success"].(bool); ok && !success {
					status.State = "failed"
				}
			}
		}
		m.mu.Lock()
		m.status = status
		m.mu.Unlock()
	}()
	return m.status, nil
}

type syncCommand struct {
	name string
	path string
	args []string
}

func runSyncCommand(python, script string, args []string) (map[string]any, error) {
	cmd := exec.Command(python, append([]string{script}, args...)...)
	cmd.Dir = filepath.Dir(script)
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	err := cmd.Run()
	var summary map[string]any
	if strings.TrimSpace(output.String()) == "" && err == nil {
		return map[string]any{"success": true}, nil
	}
	if jsonErr := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &summary); jsonErr != nil {
		if err == nil {
			err = fmt.Errorf("%s 输出不是有效 JSON: %v", filepath.Base(script), jsonErr)
		}
		return map[string]any{"success": false, "error": strings.TrimSpace(output.String())}, err
	}
	if success, ok := summary["success"].(bool); ok && !success && err == nil {
		err = fmt.Errorf("%s 同步失败", filepath.Base(script))
	}
	return summary, err
}

func syncScriptDir() (string, error) {
	if configured := os.Getenv("SYNC_SCRIPT_DIR"); configured != "" {
		path, err := filepath.Abs(configured)
		if err == nil {
			if info, statErr := os.Stat(path); statErr == nil && info.IsDir() {
				return path, nil
			}
		}
		return "", fmt.Errorf("找不到同步脚本目录 %s", configured)
	}
	candidates := []string{"..", "."}
	for _, candidate := range candidates {
		path, err := filepath.Abs(candidate)
		if err == nil {
			if info, statErr := os.Stat(path); statErr == nil && info.IsDir() {
				return path, nil
			}
		}
	}
	return "", fmt.Errorf("找不到同步脚本目录，请配置 SYNC_SCRIPT_DIR")
}

func syncCommands() ([]syncCommand, error) {
	root, err := syncScriptDir()
	if err != nil {
		return nil, err
	}
	commands := []syncCommand{
		{name: "sales_orders", path: filepath.Join(root, "repair_records.py"), args: []string{"--sales-only", "--mode", "incremental", "--apply"}},
		{name: "station_records", path: filepath.Join(root, "station_records.py"), args: []string{"--mode", "incremental", "--apply"}},
		{name: "repair_records", path: filepath.Join(root, "repair_records.py"), args: []string{"--mode", "incremental", "--apply"}},
		{name: "order_bom_postings", path: filepath.Join(root, "order_bom_postings.py"), args: []string{"--mode", "incremental", "--apply"}},
		{name: "serial_bindings", path: filepath.Join(root, "serial_bindings.py"), args: []string{"--mode", "incremental", "--apply"}},
	}
	for _, command := range commands {
		path := command.path
		if _, statErr := os.Stat(path); statErr != nil {
			return nil, fmt.Errorf("找不到同步脚本 %s，请检查 SYNC_SCRIPT_DIR", path)
		}
	}
	return commands, nil
}
