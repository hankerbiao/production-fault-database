package main

import (
	"net/http"
	"strings"
)

func (s *server) viewList(w http.ResponseWriter, r *http.Request) {
	page, pageSize := pagination(r)
	result, err := s.store.ViewList(r.Context(), r.PathValue("viewID"), viewFilters(r.URL.Query()), page, pageSize)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
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
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "stream is only available for ZSGV_ZSD124"})
		return
	}
	streamTSV(w, "bom.tsv")
	_ = s.store.ViewBOMStream(r.Context(), viewFilters(r.URL.Query()), w)
}

func (s *server) viewStationStream(w http.ResponseWriter, r *http.Request) {
	streamTSV(w, "station.tsv")
	_ = s.store.ViewStationStream(r.Context(), viewFilters(r.URL.Query()), w)
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
