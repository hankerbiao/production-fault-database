package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"production-fault-gateway/internal/store"
)

type fakeStore struct {
	pingErr              error
	list                 store.ListResult
	orders               store.OrderListResult
	stats                store.StatsResult
	ostats               store.OrderStatsResult
	models               store.OrderModelsResult
	err                  error
	gotPage, gotPageSize int
	gotFilters           store.Filters
	gotOrderFilters      store.OrderFilters
	gotID                string
	gotAll               bool
}

func (f *fakeStore) Ping(context.Context) error { return f.pingErr }
func (f *fakeStore) List(_ context.Context, filters store.Filters, page, pageSize int) (store.ListResult, error) {
	f.gotFilters, f.gotPage, f.gotPageSize = filters, page, pageSize
	if f.err != nil {
		return store.ListResult{}, f.err
	}
	return f.list, nil
}
func (f *fakeStore) FaultDetail(_ context.Context, id string) (store.FaultDetail, error) {
	f.gotID = id
	if f.err != nil {
		return store.FaultDetail{}, f.err
	}
	return store.FaultDetail{}, nil
}
func (f *fakeStore) Stats(_ context.Context, filters store.Filters) (store.StatsResult, error) {
	f.gotFilters = filters
	if f.err != nil {
		return store.StatsResult{}, f.err
	}
	return f.stats, nil
}
func (f *fakeStore) Orders(_ context.Context, filters store.OrderFilters, page, pageSize int) (store.OrderListResult, error) {
	f.gotOrderFilters, f.gotPage, f.gotPageSize = filters, page, pageSize
	if f.err != nil {
		return store.OrderListResult{}, f.err
	}
	return f.orders, nil
}
func (f *fakeStore) OrdersAll(_ context.Context, filters store.OrderFilters) (store.OrderListResult, error) {
	f.gotOrderFilters, f.gotAll = filters, true
	if f.err != nil {
		return store.OrderListResult{}, f.err
	}
	return f.orders, nil
}
func (f *fakeStore) OrderDetail(_ context.Context, id string) (store.OrderDetail, error) {
	f.gotID = id
	if f.err != nil {
		return store.OrderDetail{}, f.err
	}
	return store.OrderDetail{}, nil
}
func (f *fakeStore) OrderStats(_ context.Context, filters store.OrderFilters) (store.OrderStatsResult, error) {
	f.gotOrderFilters = filters
	if f.err != nil {
		return store.OrderStatsResult{}, f.err
	}
	return f.ostats, nil
}
func (f *fakeStore) OrderModels(_ context.Context, _ string) (store.OrderModelsResult, error) {
	if f.err != nil {
		return store.OrderModelsResult{}, f.err
	}
	return f.models, nil
}
func (f *fakeStore) DataStatus(context.Context) (store.DataStatus, error) {
	return store.DataStatus{}, f.err
}
func (f *fakeStore) FaultSNs(context.Context, store.Filters) ([]store.FaultSN, error) {
	return nil, f.err
}
func (f *fakeStore) FaultRowsBySNS(context.Context, []string, string, string, string) ([]bson.M, error) {
	return nil, f.err
}
func (f *fakeStore) ViewList(context.Context, string, store.ViewFilters, int, int) (store.ViewListResult, error) {
	return store.ViewListResult{}, f.err
}
func (f *fakeStore) ViewListAll(context.Context, string, store.ViewFilters) (store.ViewListResult, error) {
	return store.ViewListResult{}, f.err
}
func (f *fakeStore) ViewBOMStream(context.Context, store.ViewFilters, io.Writer) error { return f.err }
func (f *fakeStore) ViewStationStream(context.Context, store.ViewFilters, io.Writer) error {
	return f.err
}
func (f *fakeStore) ViewDetail(context.Context, string, string) (store.ViewDetailResult, error) {
	return store.ViewDetailResult{}, f.err
}
func (f *fakeStore) ViewStats(context.Context, string, store.ViewFilters) (store.ViewStatsResult, error) {
	return store.ViewStatsResult{}, f.err
}

func newTestServer(f *fakeStore) *server { return &server{store: f, sync: newSyncManager()} }

func TestAPIDocumentEndpoints(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "openapi.json"), []byte(`{"openapi":"3.0.3"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "agent-guide.md"), []byte("# Agent"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("API_DOCS_DIR", dir)
	s := newTestServer(&fakeStore{})
	for _, tc := range []struct{ path, contentType, body string }{{"/api/openapi.json", "application/json; charset=utf-8", `{"openapi":"3.0.3"}`}, {"/api/agent-guide.md", "text/markdown; charset=utf-8", "# Agent"}} {
		r := httptest.NewRecorder()
		s.apiDocument(r, httptest.NewRequest(http.MethodGet, tc.path, nil))
		if r.Code != http.StatusOK || r.Header().Get("Content-Type") != tc.contentType || strings.TrimSpace(r.Body.String()) != tc.body {
			t.Fatalf("%s: status=%d type=%q body=%q", tc.path, r.Code, r.Header().Get("Content-Type"), r.Body.String())
		}
	}
	r := httptest.NewRecorder()
	s.apiDocument(r, httptest.NewRequest(http.MethodGet, "/api/secret.txt", nil))
	if r.Code != http.StatusNotFound {
		t.Fatalf("unsupported status=%d", r.Code)
	}
	t.Setenv("API_DOCS_DIR", filepath.Join(dir, "missing"))
	r = httptest.NewRecorder()
	s.apiDocument(r, httptest.NewRequest(http.MethodGet, "/api/openapi.json", nil))
	if r.Code != http.StatusNotFound {
		t.Fatalf("missing status=%d", r.Code)
	}
}

func TestOpenAPICoversRegisteredAPIRoutes(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	data, err := os.ReadFile(filepath.Join(filepath.Dir(file), "..", "..", "..", "docs", "openapi.json"))
	if err != nil {
		t.Fatal(err)
	}
	var spec struct {
		Paths map[string]map[string]json.RawMessage `json:"paths"`
	}
	if err := json.Unmarshal(data, &spec); err != nil {
		t.Fatal(err)
	}
	expected := map[string][]string{
		"/api/health": {"get"}, "/api/faults": {"get"}, "/api/faults/lookup": {"post"}, "/api/faults/by-sns": {"get", "post"},
		"/api/faults/by-orders": {"get"}, "/api/faults/detail": {"get"}, "/api/faults/stats": {"get"}, "/api/orders": {"get"},
		"/api/orders/all": {"get"}, "/api/orders/detail": {"get"}, "/api/orders/stats": {"get"}, "/api/orders/models": {"get"},
		"/api/views/{viewID}": {"get"}, "/api/views/{viewID}/all": {"get"}, "/api/views/{viewID}/stream": {"get"}, "/api/views/{viewID}/detail": {"get"}, "/api/views/{viewID}/stats": {"get"},
		"/api/sync/incremental": {"post"}, "/api/sync/status": {"get"}, "/api/data-status": {"get"},
	}
	for path, methods := range expected {
		for _, method := range methods {
			if _, ok := spec.Paths[path][method]; !ok {
				t.Errorf("missing OpenAPI operation %s %s", method, path)
			}
		}
	}
}

func TestHealthAndJSONErrors(t *testing.T) {
	t.Run("healthy", func(t *testing.T) {
		f := &fakeStore{}
		r := httptest.NewRecorder()
		newTestServer(f).health(r, httptest.NewRequest(http.MethodGet, "/api/health", nil))
		if r.Code != http.StatusOK || r.Header().Get("Content-Type") != "application/json; charset=utf-8" {
			t.Fatalf("status=%d content-type=%q", r.Code, r.Header().Get("Content-Type"))
		}
	})
	t.Run("unhealthy", func(t *testing.T) {
		f := &fakeStore{pingErr: errors.New("mongo down")}
		r := httptest.NewRecorder()
		newTestServer(f).health(r, httptest.NewRequest(http.MethodGet, "/api/health", nil))
		var body map[string]any
		if r.Code != http.StatusServiceUnavailable || json.NewDecoder(r.Body).Decode(&body) != nil || body["status"] != "unhealthy" {
			t.Fatalf("code=%d body=%v", r.Code, body)
		}
	})
}

func TestFaultsNormalisePaginationAndFilters(t *testing.T) {
	f := &fakeStore{list: store.ListResult{Page: 1, PageSize: 20}}
	r := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/faults?page=0&pageSize=999&keyword=%20abc%20&hostBarcode=%20PC-1%20", nil)
	newTestServer(f).faults(r, req)
	if r.Code != http.StatusOK || f.gotPage != 1 || f.gotPageSize != 20 {
		t.Fatalf("status=%d page=%d size=%d", r.Code, f.gotPage, f.gotPageSize)
	}
	if f.gotFilters.Keyword != "abc" || f.gotFilters.HostBarcode != "PC-1" {
		t.Fatalf("filters=%+v", f.gotFilters)
	}
}

func TestDetailEndpoints(t *testing.T) {
	for _, tc := range []struct {
		name, path string
		handler    func(*server, http.ResponseWriter, *http.Request)
	}{
		{"fault", "/api/faults/detail", (*server).faultDetail},
		{"order", "/api/orders/detail", (*server).orderDetail},
	} {
		t.Run(tc.name+" missing id", func(t *testing.T) {
			r := httptest.NewRecorder()
			tc.handler(newTestServer(&fakeStore{}), r, httptest.NewRequest(http.MethodGet, tc.path, nil))
			if r.Code != http.StatusBadRequest {
				t.Fatalf("status=%d", r.Code)
			}
		})
		t.Run(tc.name+" not found", func(t *testing.T) {
			f := &fakeStore{err: mongo.ErrNoDocuments}
			r := httptest.NewRecorder()
			tc.handler(newTestServer(f), r, httptest.NewRequest(http.MethodGet, tc.path+"?id=x", nil))
			if r.Code != http.StatusNotFound || f.gotID != "x" {
				t.Fatalf("status=%d id=%q", r.Code, f.gotID)
			}
		})
	}
}

func TestOrdersAndCORS(t *testing.T) {
	f := &fakeStore{orders: store.OrderListResult{Page: 2, PageSize: 3}}
	r := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/orders?page=2&pageSize=3&source=SG&gstrsFrom=2026-01-01&gstrsTo=2026-01-31&keyword=%20SO-1%20", nil)
	newTestServer(f).orders(r, req)
	if r.Code != http.StatusOK || f.gotPage != 2 || f.gotPageSize != 3 {
		t.Fatalf("status=%d page=%d size=%d", r.Code, f.gotPage, f.gotPageSize)
	}
	if f.gotOrderFilters.Keyword != "SO-1" || f.gotOrderFilters.Source != "SG" || f.gotOrderFilters.GSTRSFrom != "2026-01-01" || f.gotOrderFilters.GSTRSTo != "2026-01-31" {
		t.Fatalf("filters=%+v", f.gotOrderFilters)
	}

	h := cors(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusTeapot) }))
	preflight := httptest.NewRecorder()
	h.ServeHTTP(preflight, httptest.NewRequest(http.MethodOptions, "/api", nil))
	if preflight.Code != http.StatusNoContent || preflight.Header().Get("Access-Control-Allow-Origin") != "*" {
		t.Fatalf("preflight=%d headers=%v", preflight.Code, preflight.Header())
	}
}

func TestOrdersAllUsesFiltersWithoutBoardPagination(t *testing.T) {
	f := &fakeStore{orders: store.OrderListResult{Items: []store.Order{{AUFNR: "PO-ALL"}}, Total: 1}}
	r := httptest.NewRecorder()
	newTestServer(f).orders(r, httptest.NewRequest(http.MethodGet, "/api/orders?all=true&productionOrder=PO-ALL&source=SG&page=9&pageSize=20", nil))
	if r.Code != http.StatusOK || !f.gotAll || f.gotOrderFilters.ProductionOrder != "PO-ALL" || f.gotOrderFilters.Source != "SG" {
		t.Fatalf("status=%d all=%v filters=%+v", r.Code, f.gotAll, f.gotOrderFilters)
	}
}

func TestOrderModels(t *testing.T) {
	f := &fakeStore{models: store.OrderModelsResult{Items: []string{"M-1", "M-2"}}}
	r := httptest.NewRecorder()
	newTestServer(f).orderModels(r, httptest.NewRequest(http.MethodGet, "/api/orders/models?keyword=M", nil))
	if r.Code != http.StatusOK {
		t.Fatalf("status=%d", r.Code)
	}
	var result store.OrderModelsResult
	if err := json.NewDecoder(r.Body).Decode(&result); err != nil || len(result.Items) != 2 {
		t.Fatalf("result=%+v err=%v", result, err)
	}
}

func TestFilterHelpers(t *testing.T) {
	q := url.Values{"keyword": {" a "}, "source": {" SG "}, "sns": {"SN1,SN2"}, "productionOrders": {"00012,13"}, "dateFrom": {"2026-01-01"}, "station": {"ST-1"}, "timeField": {" planned "}}
	if repairFilters(q).Keyword != "a" || orderFilters(q).Source != "SG" {
		t.Fatal("query values were not trimmed")
	}
	filters := repairFilters(q)
	if filters.SNS != "SN1,SN2" || filters.ProductionOrders != "00012,13" || filters.DateFrom != "2026-01-01" || filters.Station != "ST-1" || filters.TimeField != "planned" {
		t.Fatalf("extended filters=%+v", filters)
	}
	if !viewFilters(url.Values{"missingSalesOrder": {" true "}}).MissingSalesOrder {
		t.Fatal("missingSalesOrder was not parsed")
	}
}

func TestViewAndDataStatusRoutes(t *testing.T) {
	f := &fakeStore{}
	r := httptest.NewRecorder()
	newTestServer(f).viewList(r, httptest.NewRequest(http.MethodGet, "/api/views/Z_V_ZMES_T_001?stationCode=LINE-1&sn=SN-1&dateFrom=2026-01-01", nil))
	if r.Code != http.StatusOK {
		t.Fatalf("view route status=%d", r.Code)
	}
	r = httptest.NewRecorder()
	newTestServer(f).dataStatus(r, httptest.NewRequest(http.MethodGet, "/api/data-status", nil))
	if r.Code != http.StatusOK {
		t.Fatalf("data status=%d", r.Code)
	}
}

func TestDefaultServerPortMatchesDevelopmentProxy(t *testing.T) {
	t.Setenv("PORT", "")
	if port := getenv("PORT", "18080"); port != "18080" {
		t.Fatalf("default port=%q, want 18080", port)
	}
}

func TestRegisterRoutesKeepsDocumentedPaths(t *testing.T) {
	h := newTestServer(&fakeStore{})
	mux := registerRoutes(h)
	paths := []struct{ method, path string }{
		{http.MethodGet, "/api/faults/by-sns"}, {http.MethodGet, "/api/faults/by-orders"},
		{http.MethodGet, "/api/orders/all"}, {http.MethodGet, "/api/views/ZSGV_ZSD124"},
		{http.MethodGet, "/api/views/ZSGV_ZSD124/detail?id=x"}, {http.MethodGet, "/api/views/ZSGV_ZSD124/stats"},
		{http.MethodPost, "/api/sync/incremental"}, {http.MethodGet, "/api/sync/status"},
	}
	for _, route := range paths {
		t.Run(route.method+route.path, func(t *testing.T) {
			req := httptest.NewRequest(route.method, route.path, nil)
			rr := httptest.NewRecorder()
			mux.ServeHTTP(rr, req)
			if rr.Code == http.StatusNotFound {
				t.Fatalf("route %s %s returned 404", route.method, route.path)
			}
		})
	}
}
