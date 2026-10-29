package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"production-fault-gateway/internal/store"
)

func (f *fakeStore) SCSDevices(_ context.Context, _ store.SCSDeviceFilters, page, size int) (store.SCSDevicePage, error) {
	return store.SCSDevicePage{Items: []store.SCSDevice{}, Page: page, PageSize: size, Scope: "00100"}, nil
}

func TestSCSDeviceRoutePaginationValidation(t *testing.T) {
	server := &server{store: &fakeStore{}}
	handler := registerRoutes(server)
	for _, tc := range []struct {
		path   string
		status int
	}{{"/api/scs/doa/devices?page=1&pageSize=10&company5000=true", http.StatusOK}, {"/api/scs/doa/devices?pageSize=1001", http.StatusBadRequest}, {"/api/scs/doa/devices?company5000=maybe", http.StatusBadRequest}} {
		r := httptest.NewRequest("GET", tc.path, nil)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatalf("%s: got %d", tc.path, w.Code)
		}
	}
}
