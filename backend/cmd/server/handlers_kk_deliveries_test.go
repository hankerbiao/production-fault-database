package main

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"production-fault-gateway/internal/store"
)

func TestKKDeliveryRouteParsesFiltersAndPagination(t *testing.T) {
	f := &fakeStore{kkDeliveries: store.KKDeliveryListResult{
		Items: []store.KKDelivery{{SalesOrder: "0000123456", MaterialNumber: "000000000000061234", DeliveryDate: "20261124", CustomerNumber: "0000012345", Plant: "1000"}},
		Page:  2, PageSize: 50, Total: 1,
	}}
	router := registerRoutes(newTestServer(f))
	req := httptest.NewRequest(http.MethodGet, "/api/kk-sales-order-deliveries?dateFrom=2026-11-01&dateTo=2026-11-30&salesOrder=0000123456&materialNumber=61234&customerNumber=0000012345&plant=1000&sn=612340123456789&page=2&pageSize=50", nil)
	response := httptest.NewRecorder()
	router.ServeHTTP(response, req)
	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	if f.gotKKFilters.DateFrom != "2026-11-01" || f.gotKKFilters.DateTo != "2026-11-30" ||
		f.gotKKFilters.SalesOrder != "0000123456" || f.gotKKFilters.MaterialNumber != "61234" ||
		f.gotKKFilters.CustomerNumber != "0000012345" || f.gotKKFilters.Plant != "1000" ||
		f.gotKKFilters.SN != "612340123456789" {
		t.Fatalf("unexpected filters: %+v", f.gotKKFilters)
	}
	if f.gotPage != 2 || f.gotPageSize != 50 {
		t.Fatalf("page=%d pageSize=%d", f.gotPage, f.gotPageSize)
	}
}

func TestKKDeliveryRouteRejectsInvalidDateAndPagination(t *testing.T) {
	router := registerRoutes(newTestServer(&fakeStore{}))
	for _, target := range []string{
		"/api/kk-sales-order-deliveries?dateFrom=2026-02-31",
		"/api/kk-sales-order-deliveries?dateFrom=2026-12-01&dateTo=2026-11-30",
		"/api/kk-sales-order-deliveries?page=0",
		"/api/kk-sales-order-deliveries?pageSize=101",
	} {
		t.Run(target, func(t *testing.T) {
			response := httptest.NewRecorder()
			router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, target, nil))
			if response.Code != http.StatusBadRequest {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
		})
	}
}

func TestKKDeliveryRouteReturnsServiceUnavailableOnReadError(t *testing.T) {
	router := registerRoutes(newTestServer(&fakeStore{err: errors.New("database unavailable")}))
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/kk-sales-order-deliveries", nil))
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestKKDeliveryExportRequiresCompleteRangeOfAtMost31Days(t *testing.T) {
	router := registerRoutes(newTestServer(&fakeStore{}))
	for _, target := range []string{
		"/api/kk-sales-order-deliveries/export",
		"/api/kk-sales-order-deliveries/export?dateFrom=2026-11-01",
		"/api/kk-sales-order-deliveries/export?dateFrom=2026-11-01&dateTo=2026-12-02",
	} {
		t.Run(target, func(t *testing.T) {
			response := httptest.NewRecorder()
			router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, target, nil))
			if response.Code != http.StatusBadRequest {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
		})
	}

	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet,
		"/api/kk-sales-order-deliveries/export?dateFrom=2026-11-01&dateTo=2026-12-01&sn=SN1", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
}
