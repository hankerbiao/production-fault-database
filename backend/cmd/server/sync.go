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
	RunID      string         `json:"runId,omitempty"`
	State      string         `json:"state"`
	Mode       string         `json:"mode,omitempty"`
	StartedAt  string         `json:"startedAt,omitempty"`
	FinishedAt string         `json:"finishedAt,omitempty"`
	Message    string         `json:"message,omitempty"`
	Summary    map[string]any `json:"summary,omitempty"`
}

type syncRunRequest struct {
	Mode      string   `json:"mode"`
	TaskIDs   []string `json:"taskIds,omitempty"`
	StartDate string   `json:"startDate,omitempty"`
	EndDate   string   `json:"endDate,omitempty"`
}

type syncTaskDefinition struct {
	ID            string   `json:"id"`
	Label         string   `json:"label"`
	Dependencies  []string `json:"dependencies"`
	FullSupported bool     `json:"fullSupported"`
}

var managedSyncTasks = []syncTaskDefinition{
	{ID: "sales_orders", Label: "销售订单", FullSupported: true},
	{ID: "station_records", Label: "工位记录", Dependencies: []string{"sales_orders"}, FullSupported: true},
	{ID: "repair_records", Label: "维修故障", Dependencies: []string{"sales_orders", "station_records"}, FullSupported: true},
	{ID: "order_bom_postings", Label: "订单 BOM 过账", Dependencies: []string{"sales_orders"}, FullSupported: true},
	{ID: "serial_bindings", Label: "序列号绑定", FullSupported: true},
	{ID: "scs_doa", Label: "SCS DOA", Dependencies: []string{"sales_orders"}, FullSupported: true},
	{ID: "scs_change", Label: "SCS 换上换下", Dependencies: []string{"scs_doa"}, FullSupported: true},
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

// startRun launches the durable Python runner. Its MongoDB state remains
// queryable even if this HTTP process restarts while the child is working.
func (m *syncManager) startRun(request syncRunRequest) (syncStatus, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.status.State == "running" {
		return m.status, fmt.Errorf("已有同步任务运行中")
	}
	if request.Mode == "" {
		request.Mode = "incremental"
	}
	if request.Mode != "incremental" && request.Mode != "full" {
		return m.status, fmt.Errorf("mode 必须为 incremental 或 full")
	}
	if request.Mode == "full" && request.StartDate == "" {
		return m.status, fmt.Errorf("全量同步必须提供 startDate")
	}
	known := make(map[string]bool, len(managedSyncTasks))
	for _, task := range managedSyncTasks {
		known[task.ID] = true
	}
	for _, taskID := range request.TaskIDs {
		if !known[taskID] {
			return m.status, fmt.Errorf("未知同步任务 %s", taskID)
		}
	}
	root, err := syncScriptDir()
	if err != nil {
		return m.status, err
	}
	script := filepath.Join(root, "scripts", "sync", "syncctl.py")
	if _, err := os.Stat(script); err != nil {
		return m.status, fmt.Errorf("找不到同步编排器 %s", script)
	}
	python := os.Getenv("SYNC_PYTHON")
	if python == "" {
		python = "python"
	}
	runID := fmt.Sprintf("api-%d", time.Now().UTC().UnixNano())
	args := []string{script, "run", "--run-id", runID, "--mode", request.Mode, "--source", "api"}
	if request.StartDate != "" {
		args = append(args, "--start-date", request.StartDate)
	}
	if request.EndDate != "" {
		args = append(args, "--end-date", request.EndDate)
	}
	if len(request.TaskIDs) > 0 {
		args = append(args, "--tasks")
		args = append(args, request.TaskIDs...)
	}
	started := time.Now().UTC()
	m.status = syncStatus{RunID: runID, State: "running", Mode: request.Mode, StartedAt: started.Format(time.RFC3339), Message: "正在启动同步编排器"}
	go func() {
		result, runErr := runSyncCommand(python, script, args[1:])
		finished := time.Now().UTC()
		status := syncStatus{RunID: runID, State: "success", Mode: request.Mode, StartedAt: started.Format(time.RFC3339), FinishedAt: finished.Format(time.RFC3339), Message: "同步完成", Summary: result}
		if runErr != nil || result["state"] != "success" {
			status.State = "failed"
			if runErr != nil {
				status.Message = runErr.Error()
			} else {
				status.Message = "同步失败"
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
	salesOrdersScript := filepath.Join(root, "scripts", "sync", "sync_sales_orders.py")
	repairRecordsScript := filepath.Join(root, "scripts", "sync", "增量同步和清洗维修故障记录.py")
	commands := []syncCommand{
		{name: "sales_orders", path: salesOrdersScript},
		{name: "station_records", path: filepath.Join(root, "scripts", "sync", "station_records.py"), args: []string{"--mode", "incremental", "--apply"}},
		{name: "repair_records", path: repairRecordsScript, args: []string{"--apply", "--no-progress", "--log-level", "ERROR"}},
		{name: "order_bom_postings", path: filepath.Join(root, "scripts", "sync", "order_bom_postings.py"), args: []string{"--mode", "incremental", "--apply"}},
		{name: "serial_bindings", path: filepath.Join(root, "scripts", "sync", "serial_bindings.py"), args: []string{"--mode", "incremental", "--apply"}},
	}
	for _, command := range commands {
		path := command.path
		if _, statErr := os.Stat(path); statErr != nil {
			return nil, fmt.Errorf("找不到同步脚本 %s，请检查 SYNC_SCRIPT_DIR", path)
		}
	}
	return commands, nil
}
