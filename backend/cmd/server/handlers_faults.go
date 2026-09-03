package main

import (
	"encoding/json"
	"net/http"
	"strings"

	"go.mongodb.org/mongo-driver/mongo"
	"production-fault-gateway/internal/store"
)

func (s *server) faults(w http.ResponseWriter, r *http.Request) {
	page, pageSize := pagination(r)
	result, err := s.store.List(r.Context(), repairFilters(r.URL.Query()), page, pageSize)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) faultLookup(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ProductionOrders []string `json:"productionOrders"`
		SalesOrders      []string `json:"salesOrders"`
		DateFrom         string   `json:"dateFrom"`
		DateTo           string   `json:"dateTo"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON body"})
		return
	}
	if len(body.ProductionOrders) == 0 && len(body.SalesOrders) == 0 {
		writeJSON(w, http.StatusOK, map[string]any{"items": []store.FaultSN{}})
		return
	}
	items, err := s.store.FaultSNs(r.Context(), store.Filters{ProductionOrders: strings.Join(body.ProductionOrders, ","), SalesOrders: strings.Join(body.SalesOrders, ","), DateFrom: body.DateFrom, DateTo: body.DateTo})
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (s *server) faultRowsBySNS(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SNS      []string `json:"sns"`
		DateFrom string   `json:"dateFrom"`
		DateTo   string   `json:"dateTo"`
		Station  string   `json:"station"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON body"})
		return
	}
	if len(body.SNS) > store.MaxBulkQueryRows {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "too many SN values"})
		return
	}
	items, err := s.store.FaultRowsBySNS(r.Context(), body.SNS, body.DateFrom, body.DateTo, body.Station)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (s *server) faultsBySNS(w http.ResponseWriter, r *http.Request)    { s.faults(w, r) }
func (s *server) faultsByOrders(w http.ResponseWriter, r *http.Request) { s.faults(w, r) }

func (s *server) stats(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.Stats(r.Context(), repairFilters(r.URL.Query()))
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) faultDetail(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimSpace(r.URL.Query().Get("id"))
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "id is required"})
		return
	}
	result, err := s.store.FaultDetail(r.Context(), id)
	if err != nil {
		writeDetailError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func writeStoreError(w http.ResponseWriter, err error) {
	writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
}
func writeDetailError(w http.ResponseWriter, err error) {
	if err == mongo.ErrNoDocuments {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "record not found"})
		return
	}
	writeStoreError(w, err)
}
