package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"production-fault-gateway/internal/store"
)

type dataCheckStorage interface {
	DataCheckConfig(context.Context) (store.DataCheckConfig, error)
	SaveDataCheckConfig(context.Context, store.DataCheckConfig) error
	CreateDataCheckRun(context.Context, bson.M) error
	UpdateDataCheckRun(context.Context, string, bson.M) error
	DataCheckRun(context.Context, string) (bson.M, error)
	DataCheckRuns(context.Context, int) ([]bson.M, error)
}

type dataCheckManager struct {
	storage dataCheckStorage
	mu      sync.Mutex
	running bool
}

const dataCheckStaleAfter = 3*time.Hour + 5*time.Minute

func newDataCheckManager(storage dataCheckStorage) *dataCheckManager {
	return &dataCheckManager{storage: storage}
}

func (m *dataCheckManager) start(ctx context.Context, runID, trigger string) (bson.M, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.reconcileStale(ctx)
	if m.running {
		return nil, fmt.Errorf("已有数据比对任务运行中")
	}
	for _, existing := range m.activeRuns(ctx) {
		return nil, fmt.Errorf("已有数据比对任务运行中: %s", textValue(existing["_id"]))
	}
	now := time.Now().UTC()
	run := bson.M{"_id": runID, "trigger": trigger, "state": "running", "started_at": now}
	if err := m.storage.CreateDataCheckRun(ctx, run); err != nil {
		return nil, err
	}
	m.running = true
	go m.execute(context.Background(), runID)
	return run, nil
}

func (m *dataCheckManager) activeRuns(ctx context.Context) []bson.M {
	runs, err := m.storage.DataCheckRuns(ctx, 100)
	if err != nil {
		log.Printf("读取活动数据比对运行失败: %v", err)
		return nil
	}
	cutoff := time.Now().UTC().Add(-dataCheckStaleAfter)
	active := make([]bson.M, 0, 1)
	for _, run := range runs {
		if textValue(run["state"]) != "running" {
			continue
		}
		started, err := time.Parse(time.RFC3339, timeValue(run["started_at"]))
		if err == nil && !started.Before(cutoff) {
			active = append(active, run)
		}
	}
	return active
}

func (m *dataCheckManager) reconcileStale(ctx context.Context) {
	runs, err := m.storage.DataCheckRuns(ctx, 100)
	if err != nil {
		log.Printf("读取数据比对运行记录失败: %v", err)
		return
	}
	cutoff := time.Now().UTC().Add(-dataCheckStaleAfter)
	for _, run := range runs {
		if textValue(run["state"]) != "running" {
			continue
		}
		started, err := time.Parse(time.RFC3339, timeValue(run["started_at"]))
		if err != nil || !started.Before(cutoff) {
			continue
		}
		id := textValue(run["_id"])
		if id == "" {
			continue
		}
		if err := m.storage.UpdateDataCheckRun(ctx, id, bson.M{
			"state": "abandoned", "message": "服务重启后检测到超时的运行记录",
			"finished_at": time.Now().UTC(),
		}); err != nil {
			log.Printf("回收超时数据比对运行记录失败: run_id=%s err=%v", id, err)
		}
	}
}

func (m *dataCheckManager) execute(parent context.Context, runID string) {
	defer func() {
		m.mu.Lock()
		m.running = false
		m.mu.Unlock()
	}()
	ctx, cancel := context.WithTimeout(parent, 3*time.Hour)
	defer cancel()
	summary, runErr := runDataCheckCommand(ctx)
	state := "success"
	message := "比对完成，未发现差异"
	if runErr != nil || summary["success"] != true {
		state = "failed"
		message = "数据比对失败"
		if runErr != nil {
			message = runErr.Error()
		}
	} else if summary["has_anomalies"] == true {
		state = "warning"
		message = "发现数据差异"
	}
	fields := bson.M{"state": state, "message": message, "summary": summary, "finished_at": time.Now().UTC()}
	if err := m.storage.UpdateDataCheckRun(context.Background(), runID, fields); err != nil {
		log.Printf("更新数据比对运行记录失败: run_id=%s err=%v", runID, err)
	}
}

func runDataCheckCommand(ctx context.Context) (map[string]any, error) {
	root, err := syncScriptDir()
	if err != nil {
		return nil, err
	}
	script := filepath.Join(root, "scripts", "sync", "compare_station_records.py")
	if _, err := os.Stat(script); err != nil {
		return nil, fmt.Errorf("找不到比对脚本 %s", script)
	}
	python := os.Getenv("SYNC_PYTHON")
	if python == "" {
		python = "python"
	}
	cmd := exec.CommandContext(ctx, python, script)
	cmd.Dir = filepath.Dir(script)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err = cmd.Run()
	if stderr.Len() > 0 {
		log.Printf("数据比对输出: %s", strings.TrimSpace(stderr.String()))
	}
	var summary map[string]any
	if jsonErr := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &summary); jsonErr != nil {
		if err != nil {
			return map[string]any{"success": false, "error": strings.TrimSpace(stderr.String())}, err
		}
		return nil, fmt.Errorf("比对脚本未输出有效 JSON: %w", jsonErr)
	}
	if err != nil {
		return summary, err
	}
	if summary["success"] != true {
		return summary, fmt.Errorf("比对脚本执行失败")
	}
	return summary, nil
}

func (m *dataCheckManager) schedule(ctx context.Context) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			m.scheduleIfDue(ctx)
		}
	}
}

func (m *dataCheckManager) scheduleIfDue(ctx context.Context) {
	m.reconcileStale(ctx)
	config, err := m.storage.DataCheckConfig(ctx)
	if err != nil || !config.Enabled {
		if err != nil {
			log.Printf("读取数据比对配置失败: %v", err)
		}
		return
	}
	location, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		log.Printf("加载上海时区失败: %v", err)
		return
	}
	now := time.Now().In(location)
	scheduledTime, err := time.Parse("15:04", config.DailyTime)
	if err != nil {
		log.Printf("数据比对时间配置无效: %q", config.DailyTime)
		return
	}
	if now.Hour()*60+now.Minute() < scheduledTime.Hour()*60+scheduledTime.Minute() {
		return
	}
	date := now.Format("2006-01-02")
	runID := "daily-" + date
	if _, err := m.storage.DataCheckRun(ctx, runID); err == nil {
		return
	} else if !errors.Is(err, mongo.ErrNoDocuments) {
		log.Printf("读取每日数据比对记录失败: %v", err)
		return
	}
	if _, err := m.start(ctx, runID, "scheduled"); err != nil && !mongo.IsDuplicateKeyError(err) {
		log.Printf("启动每日数据比对失败: %v", err)
	}
}

func (s *server) dataCheckStorage(w http.ResponseWriter) (dataCheckStorage, bool) {
	storage, ok := s.store.(dataCheckStorage)
	if !ok {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "数据比对存储不可用"})
		return nil, false
	}
	return storage, true
}

func (s *server) dataCheckConfig(w http.ResponseWriter, r *http.Request) {
	storage, ok := s.dataCheckStorage(w)
	if !ok {
		return
	}
	config, err := storage.DataCheckConfig(r.Context())
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"config": config})
}

func (s *server) saveDataCheckConfig(w http.ResponseWriter, r *http.Request) {
	storage, ok := s.dataCheckStorage(w)
	if !ok {
		return
	}
	var config store.DataCheckConfig
	if err := json.NewDecoder(r.Body).Decode(&config); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "配置格式无效"})
		return
	}
	if _, err := time.Parse("15:04", config.DailyTime); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "dailyTime 必须为 HH:MM"})
		return
	}
	if err := storage.SaveDataCheckConfig(r.Context(), config); err != nil {
		writeStoreError(w, err)
		return
	}
	config.Timezone = "Asia/Shanghai"
	config.UpdatedAt = time.Now().UTC()
	writeJSON(w, http.StatusOK, map[string]any{"config": config})
}

func (s *server) startDataCheck(w http.ResponseWriter, r *http.Request) {
	_, ok := s.dataCheckStorage(w)
	if !ok {
		return
	}
	runID := fmt.Sprintf("manual-%d", time.Now().UTC().UnixNano())
	run, err := s.dataCheck.start(r.Context(), runID, "manual")
	if err != nil {
		if mongo.IsDuplicateKeyError(err) {
			writeJSON(w, http.StatusConflict, map[string]string{"error": "该数据比对已启动"})
			return
		}
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusAccepted, run)
}

func (s *server) dataCheckRuns(w http.ResponseWriter, r *http.Request) {
	storage, ok := s.dataCheckStorage(w)
	if !ok {
		return
	}
	if s.dataCheck != nil {
		s.dataCheck.reconcileStale(r.Context())
	}
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	runs, err := storage.DataCheckRuns(r.Context(), limit)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": runs})
}
