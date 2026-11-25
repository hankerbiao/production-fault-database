package main

import (
	"context"
	"net/http"
	"strings"
	"time"
)

func (s *server) kkCumulativeDeliveries(w http.ResponseWriter, r *http.Request) {
	dateTo, err := parseKKDeliveryDate(strings.TrimSpace(r.URL.Query().Get("dateTo")))
	if err != nil || dateTo == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "dateTo 必须为 YYYY-MM-DD 日期"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	result, err := s.store.KKCumulativeDeliveries(ctx, dateTo)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}
