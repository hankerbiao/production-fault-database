package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"production-fault-gateway/internal/store"
)

func TestKKShipmentStatsRouteReturnsModelCounts(t *testing.T) {
	f := &fakeStore{}
	router := registerRoutes(newTestServer(f))
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet,
		"/api/kk-sales-order-deliveries/shipment-stats?dateFrom=2026-10-01&dateTo=2026-10-31", nil))

	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	if f.gotKKFilters.DateFrom != "2026-10-01" || f.gotKKFilters.DateTo != "2026-10-31" {
		t.Fatalf("unexpected filters: %+v", f.gotKKFilters)
	}
	var body struct {
		DeviceCount int `json:"deviceCount"`
		ByModel     []struct {
			Model   string `json:"model"`
			Devices int    `json:"devices"`
		} `json:"byModel"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatalf("invalid body: %v (%s)", err, response.Body.String())
	}
	if body.DeviceCount != 2 || len(body.ByModel) != 1 || body.ByModel[0].Model != "6215 C2" {
		t.Fatalf("unexpected body: %s", response.Body.String())
	}
}

func TestKKShipmentStatsRouteRequiresCompleteDateRange(t *testing.T) {
	f := &fakeStore{}
	router := registerRoutes(newTestServer(f))
	for _, target := range []string{
		"/api/kk-sales-order-deliveries/shipment-stats",
		"/api/kk-sales-order-deliveries/shipment-stats?dateFrom=2026-10-01",
		"/api/kk-sales-order-deliveries/shipment-stats?dateTo=2026-10-31",
	} {
		response := httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, target, nil))
		if response.Code != http.StatusBadRequest {
			t.Fatalf("%s status=%d", target, response.Code)
		}
	}
}

func TestKKShipmentStatsRejectsAmbiguousQuantity(t *testing.T) {
	router := registerRoutes(newTestServer(&fakeStore{
		err: &store.KKShipmentDataError{InvalidItems: 9, QuantityIssues: 9},
	}))
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet,
		"/api/kk-sales-order-deliveries/shipment-stats?dateFrom=2020-01-01&dateTo=2026-11-25", nil))
	var body struct {
		QuantityIssues int `json:"quantityIssues"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil ||
		response.Code != http.StatusUnprocessableEntity || body.QuantityIssues != 9 {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
}
