package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"go.mongodb.org/mongo-driver/bson"
)

var machineHANAQuerySlot = make(chan struct{}, 1)

func (s *server) machineConfigHANA(w http.ResponseWriter, r *http.Request) {
	sn := strings.TrimSpace(r.URL.Query().Get("sn"))
	if sn == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "缺少 sn 参数"})
		return
	}
	if len(sn) > 40 || strings.ContainsAny(sn, " \t\r\n") {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sn 参数无效"})
		return
	}
	select {
	case machineHANAQuerySlot <- struct{}{}:
		defer func() { <-machineHANAQuerySlot }()
	default:
		w.Header().Set("Retry-After", "1")
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "HANA 查询正在处理中，请稍后重试"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	root, err := syncScriptDir()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "HANA 查询脚本未配置"})
		return
	}
	python := os.Getenv("SYNC_PYTHON")
	if python == "" {
		python = "python"
	}
	input, _ := json.Marshal(map[string]string{"sn": sn})
	cmd := exec.CommandContext(ctx, python, filepath.Join(root, "scripts", "sources", "hana", "query_machine_config.py"))
	cmd.Dir = root
	cmd.Stdin = bytes.NewReader(input)
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		err := cmd.Process.Signal(syscall.SIGTERM)
		if errors.Is(err, os.ErrProcessDone) {
			return nil
		}
		return err
	}
	cmd.WaitDelay = 2 * time.Second
	output, err := cmd.Output()
	if err != nil {
		if r.Context().Err() != nil {
			return
		}
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			writeJSON(w, http.StatusGatewayTimeout, map[string]string{"error": "HANA 查询超时"})
			return
		}
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "HANA 查询失败"})
		return
	}
	var result struct {
		Order string   `json:"order"`
		Rows  []bson.M `json:"rows"`
	}
	if json.Unmarshal(output, &result) != nil || result.Rows == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "HANA 查询返回格式无效"})
		return
	}
	if result.Order == "" {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "未找到 SN " + sn + " 对应的生产订单"})
		return
	}
	writeJSON(w, http.StatusOK, buildMachineConfigItems(result.Order, result.Rows))
}
