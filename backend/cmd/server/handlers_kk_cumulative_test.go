package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"production-fault-gateway/internal/store"
)

func TestKKCumulativeDeliveriesRequiresCutoffAndReturnsGroupedCounts(t *testing.T) {
	f := &fakeStore{}
	router := registerRoutes(newTestServer(f))
	const path = "/api/kk-sales-order-deliveries/cumulative-counts"
	for _, invalid := range []string{path, path + "?dateTo=2026-02-30"} {
		response := httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, invalid, nil))
		if response.Code != http.StatusBadRequest || f.gotKKFilters.DateTo != "" {
			t.Fatalf("invalid cutoff reached store: status=%d", response.Code)
		}
	}

	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path+"?dateTo=2026-11-25", nil))
	var body store.KKCumulativeResult
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || f.gotKKFilters.DateTo != "2026-11-25" ||
		body.DateTo != "2026-11-25" || body.Quantity != 3 || body.BatchCount != 1 || len(body.Items) != 1 ||
		body.Items[0] != (store.KKCumulativeDelivery{SalesOrder: "SO1", Month: "2026-11", Quantity: 3, Devices: 2}) {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
}
