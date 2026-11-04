package main

import (
	"net/http"
	"strconv"
	"strings"

	"production-fault-gateway/internal/store"
)

func (s *server) scsDOADevices(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	page, size := 1, 100
	if v := strings.TrimSpace(q.Get("page")); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > 10000000 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "page 必须为 1 至 10000000 的整数"})
			return
		}
		page = n
	}
	if v := strings.TrimSpace(q.Get("pageSize")); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > 1000 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "pageSize 必须为 1 至 1000 的整数"})
			return
		}
		size = n
	}
	company := strings.TrimSpace(q.Get("company5000"))
	if company != "" && company5000Filter(company) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "company5000 必须为 true 或 false"})
		return
	}
	f := store.SCSDeviceFilters{Company5000: company, SalesOrder: strings.TrimSpace(q.Get("salesOrder")), ServiceOrder: strings.TrimSpace(q.Get("serviceOrder")), SN: strings.TrimSpace(q.Get("sn"))}
	result, err := s.store.SCSDevices(r.Context(), f, page, size)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}
