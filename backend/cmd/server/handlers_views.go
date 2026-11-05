package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"production-fault-gateway/internal/store"
)

const maxStationBatchOrders = 500

type stationBatchRequest struct {
	DateFrom         string   `json:"dateFrom"`
	DateTo           string   `json:"dateTo"`
	TimeField        string   `json:"timeField"`
	ProductionOrders []string `json:"productionOrders"`
}

func (s *server) viewList(w http.ResponseWriter, r *http.Request) {
	page, pageSize := pagination(r)
	preview := r.PathValue("viewID") == "Z_V_ZMES_T_001" && parseBool(r.URL.Query().Get("preview"))
	result, err := s.store.ViewList(r.Context(), r.PathValue("viewID"), viewFilters(r.URL.Query()), page, pageSize, preview, includeTotal(r))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) viewBOMOptions(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.ViewOptions(r.Context(), "ZSGV_ZSD124", "cpx")
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) viewListAll(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.ViewListAll(r.Context(), r.PathValue("viewID"), viewFilters(r.URL.Query()))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) viewStream(w http.ResponseWriter, r *http.Request) {
	viewID := r.PathValue("viewID")
	if viewID == "Z_V_ZMES_T_001" {
		s.viewStationStream(w, r)
		return
	}
	if viewID != "ZSGV_ZSD124" {
		if viewID != "SCS_DOA" && viewID != "SCS_CHANGE" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "stream is only available for ZSGV_ZSD124, SCS_DOA or SCS_CHANGE"})
			return
		}
		streamTSV(w, strings.ToLower(viewID)+".tsv")
		if err := s.store.ViewSCSStream(r.Context(), viewID, viewFilters(r.URL.Query()), w); err != nil {
			writeStreamError(w, err)
		}
		return
	}
	filters := viewFilters(r.URL.Query())
	if err := store.ValidateBOMFilter(filters); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	streamTSV(w, "bom.tsv")
	_ = s.store.ViewBOMStream(r.Context(), filters, w)
}

func (s *server) viewStationStream(w http.ResponseWriter, r *http.Request) {
	var body bytes.Buffer
	if err := s.store.ViewStationStream(r.Context(), viewFilters(r.URL.Query()), &body); err != nil {
		writeStreamError(w, err)
		return
	}
	streamTSV(w, "station.tsv")
	_, _ = w.Write(body.Bytes())
}

func (s *server) viewStreamBatch(w http.ResponseWriter, r *http.Request) {
	if r.PathValue("viewID") != "Z_V_ZMES_T_001" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "batch stream is only available for Z_V_ZMES_T_001"})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 128<<10)
	var request stationBatchRequest
	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(&request); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON request body"})
		return
	}
	if len(request.ProductionOrders) == 0 || len(request.ProductionOrders) > maxStationBatchOrders {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "productionOrders must contain 1 to 500 orders"})
		return
	}
	orders := make([]string, 0, len(request.ProductionOrders))
	seen := make(map[string]struct{}, len(request.ProductionOrders))
	for _, order := range request.ProductionOrders {
		order = strings.TrimSpace(order)
		if order == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "productionOrders cannot contain empty values"})
			return
		}
		if _, ok := seen[order]; ok {
			continue
		}
		seen[order] = struct{}{}
		orders = append(orders, order)
	}
	filters := store.ViewFilters{DateFrom: strings.TrimSpace(request.DateFrom), DateTo: strings.TrimSpace(request.DateTo), TimeField: strings.TrimSpace(request.TimeField), ProductionOrder: strings.Join(orders, ",")}
	var body bytes.Buffer
	if err := s.store.ViewStationStream(r.Context(), filters, &body); err != nil {
		writeStreamError(w, err)
		return
	}
	streamTSV(w, "station.tsv")
	_, _ = w.Write(body.Bytes())
}

func (s *server) packagingCounts(w http.ResponseWriter, r *http.Request) {
	orders := strings.TrimSpace(r.URL.Query().Get("productionOrders"))
	if orders == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "productionOrders is required"})
		return
	}
	if len(strings.Split(orders, ",")) > 50 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "at most 50 production orders are allowed"})
		return
	}
	items, err := s.store.PackagingCounts(r.Context(), orders)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func writeStreamError(w http.ResponseWriter, err error) {
	status := http.StatusBadRequest
	if errors.Is(err, store.ErrStreamQueryTooLarge) {
		status = http.StatusRequestEntityTooLarge
	}
	w.Header().Del("Content-Disposition")
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

func streamTSV(w http.ResponseWriter, filename string) {
	w.Header().Set("Content-Type", "text/tab-separated-values; charset=utf-8")
	w.Header().Set("Content-Disposition", `attachment; filename="`+filename+`"`)
}

func (s *server) viewDetail(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimSpace(r.URL.Query().Get("id"))
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "id is required"})
		return
	}
	result, err := s.store.ViewDetail(r.Context(), r.PathValue("viewID"), id)
	if err != nil {
		if strings.Contains(err.Error(), "unknown view") {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		writeDetailError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) viewStats(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.ViewStats(r.Context(), r.PathValue("viewID"), viewFilters(r.URL.Query()))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}
