package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unicode"
	"unicode/utf8"
)

const materialVendorQueryTimeout = 20 * time.Second

var materialVendorQuerySlot = make(chan struct{}, 1)

type materialVendorQueryResult struct {
	Vendors []string `json:"vendors"`
}

func (s *server) materialVendor(w http.ResponseWriter, r *http.Request) {
	materialNumber := strings.TrimSpace(r.URL.Query().Get("materialNumber"))
	if materialNumber == "" || utf8.RuneCountInString(materialNumber) > 40 || strings.IndexFunc(materialNumber, unicode.IsControl) >= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "materialNumber 必须为 1 到 40 个有效字符"})
		return
	}

	select {
	case materialVendorQuerySlot <- struct{}{}:
		defer func() { <-materialVendorQuerySlot }()
	default:
		w.Header().Set("Retry-After", "1")
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "HANA 查询正在处理中，请稍后重试"})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), materialVendorQueryTimeout)
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
	script := filepath.Join(root, "scripts", "sources", "hana", "query_material_vendor.py")
	input, err := json.Marshal(map[string]string{"materialNumber": materialNumber})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "无法创建 HANA 查询请求"})
		return
	}

	cmd := exec.CommandContext(ctx, python, script)
	cmd.Dir = root
	cmd.Stdin = bytes.NewReader(input)
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = io.Discard
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
	if err := cmd.Run(); err != nil {
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

	var result materialVendorQueryResult
	if err := json.Unmarshal(output.Bytes(), &result); err != nil || result.Vendors == nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "HANA 查询返回格式无效"})
		return
	}

	suffix := materialNumber
	if runes := []rune(materialNumber); len(runes) > 8 {
		suffix = string(runes[len(runes)-8:])
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"materialNumber": materialNumber,
		"matchedSuffix":  suffix,
		"count":          len(result.Vendors),
		"vendors":        result.Vendors,
	})
}
