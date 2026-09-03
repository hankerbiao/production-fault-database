package main

import (
	"net/http"
	"strings"
)

func (s *server) orders(w http.ResponseWriter, r *http.Request) {
	if parseBool(r.URL.Query().Get("all")) {
		s.ordersAll(w, r)
		return
	}
	page, pageSize := pagination(r)
	result, err := s.store.Orders(r.Context(), orderFilters(r.URL.Query()), page, pageSize)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) ordersAll(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.OrdersAll(r.Context(), orderFilters(r.URL.Query()))
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) orderDetail(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimSpace(r.URL.Query().Get("id"))
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "id is required"})
		return
	}
	result, err := s.store.OrderDetail(r.Context(), id)
	if err != nil {
		writeDetailError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) orderStats(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.OrderStats(r.Context(), orderFilters(r.URL.Query()))
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) orderModels(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.OrderModels(r.Context(), r.URL.Query().Get("keyword"))
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}
