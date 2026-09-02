package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/mongo"
	"production-fault-gateway/internal/store"
)

type server struct {
	store storeAPI
	sync  *syncManager
}

// storeAPI keeps HTTP tests independent from a live MongoDB server.
type storeAPI interface {
	Ping(context.Context) error
	List(context.Context, store.Filters, int, int) (store.ListResult, error)
	FaultDetail(context.Context, string) (store.FaultDetail, error)
	Stats(context.Context, store.Filters) (store.StatsResult, error)
	Orders(context.Context, store.OrderFilters, int, int) (store.OrderListResult, error)
	OrderDetail(context.Context, string) (store.OrderDetail, error)
	OrderStats(context.Context, store.OrderFilters) (store.OrderStatsResult, error)
	OrderModels(context.Context, string) (store.OrderModelsResult, error)
}

func main() {
	loadDotEnv()
	uri := mongoURI()
	database := getenv("MONGODB_DATABASE", "prod_line_fault_5000")
	repairCollection := getenv("MONGODB_COLLECTION", getenv("REPAIR_COLLECTION", "repair_records_sap"))
	orderCollection := getenv("TARGET_COLLECTION", "sales_orders_sap")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	s, err := store.New(ctx, uri, database, repairCollection, orderCollection)
	if err != nil {
		log.Fatalf("connect mongodb: %v", err)
	}
	defer s.Close(context.Background())

	h := &server{store: s, sync: newSyncManager()}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /", h.root)
	mux.HandleFunc("GET /api/health", h.health)
	mux.HandleFunc("GET /api/faults", h.faults)
	mux.HandleFunc("GET /api/faults/by-sns", h.faultsBySNS)
	mux.HandleFunc("GET /api/faults/by-orders", h.faultsByOrders)
	mux.HandleFunc("GET /api/faults/detail", h.faultDetail)
	mux.HandleFunc("GET /api/faults/stats", h.stats)
	mux.HandleFunc("GET /api/orders", h.orders)
	mux.HandleFunc("GET /api/orders/all", h.ordersAll)
	mux.HandleFunc("GET /api/orders/detail", h.orderDetail)
	mux.HandleFunc("GET /api/orders/stats", h.orderStats)
	mux.HandleFunc("GET /api/orders/models", h.orderModels)
	mux.HandleFunc("GET /api/views/{viewID}", h.viewList)
	mux.HandleFunc("GET /api/views/{viewID}/detail", h.viewDetail)
	mux.HandleFunc("GET /api/views/{viewID}/stats", h.viewStats)
	mux.HandleFunc("POST /api/sync/incremental", h.startIncrementalSync)
	mux.HandleFunc("GET /api/sync/status", h.syncStatus)
	mux.HandleFunc("GET /api/data-status", h.dataStatus)

	// Keep the default aligned with the Vite development proxy and README.
	addr := getenv("PORT", "18080")
	log.Printf("fault gateway listening on :%s", addr)
	log.Fatal(http.ListenAndServe(":"+addr, cors(mux)))
}

func (s *server) root(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	http.Redirect(w, r, getenv("FRONTEND_URL", "http://127.0.0.1:5173/"), http.StatusTemporaryRedirect)
}

func (s *server) health(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	if err := s.store.Ping(ctx); err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "unhealthy", "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *server) faults(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	page, _ := strconv.Atoi(q.Get("page"))
	if page < 1 {
		page = 1
	}
	pageSize, _ := strconv.Atoi(q.Get("pageSize"))
	if pageSize < 1 || pageSize > 100 {
		pageSize = 20
	}
	filters := repairFilters(q)
	result, err := s.store.List(r.Context(), filters, page, pageSize)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) faultsBySNS(w http.ResponseWriter, r *http.Request)    { s.faults(w, r) }
func (s *server) faultsByOrders(w http.ResponseWriter, r *http.Request) { s.faults(w, r) }

func (s *server) stats(w http.ResponseWriter, r *http.Request) {
	filters := repairFilters(r.URL.Query())
	result, err := s.store.Stats(r.Context(), filters)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
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
	if err == mongo.ErrNoDocuments {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "record not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) orders(w http.ResponseWriter, r *http.Request) {
	if parseBool(r.URL.Query().Get("all")) {
		s.ordersAll(w, r)
		return
	}
	page, pageSize := pagination(r)
	result, err := s.store.Orders(r.Context(), orderFilters(r.URL.Query()), page, pageSize)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) ordersAll(w http.ResponseWriter, r *http.Request) {
	api, ok := s.store.(interface {
		OrdersAll(context.Context, store.OrderFilters) (store.OrderListResult, error)
	})
	if !ok {
		writeJSON(w, http.StatusNotImplemented, map[string]string{"error": "bulk order API unavailable"})
		return
	}
	result, err := api.OrdersAll(r.Context(), orderFilters(r.URL.Query()))
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
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
	if err == mongo.ErrNoDocuments {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "record not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) orderStats(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.OrderStats(r.Context(), orderFilters(r.URL.Query()))
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) orderModels(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.OrderModels(r.Context(), r.URL.Query().Get("keyword"))
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) viewList(w http.ResponseWriter, r *http.Request) {
	api, ok := s.store.(interface {
		ViewList(context.Context, string, store.ViewFilters, int, int) (store.ViewListResult, error)
	})
	if !ok {
		writeJSON(w, http.StatusNotImplemented, map[string]string{"error": "view API unavailable"})
		return
	}
	page, pageSize := pagination(r)
	q := r.URL.Query()
	result, err := api.ViewList(r.Context(), r.PathValue("viewID"), viewFilters(q), page, pageSize)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) viewDetail(w http.ResponseWriter, r *http.Request) {
	api, ok := s.store.(interface {
		ViewDetail(context.Context, string, string) (store.ViewDetailResult, error)
	})
	if !ok {
		writeJSON(w, http.StatusNotImplemented, map[string]string{"error": "view API unavailable"})
		return
	}
	id := strings.TrimSpace(r.URL.Query().Get("id"))
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "id is required"})
		return
	}
	result, err := api.ViewDetail(r.Context(), r.PathValue("viewID"), id)
	if err == mongo.ErrNoDocuments {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "record not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) viewStats(w http.ResponseWriter, r *http.Request) {
	api, ok := s.store.(interface {
		ViewStats(context.Context, string, store.ViewFilters) (store.ViewStatsResult, error)
	})
	if !ok {
		writeJSON(w, http.StatusNotImplemented, map[string]string{"error": "view API unavailable"})
		return
	}
	q := r.URL.Query()
	result, err := api.ViewStats(r.Context(), r.PathValue("viewID"), viewFilters(q))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) startIncrementalSync(w http.ResponseWriter, r *http.Request) {
	status, err := s.sync.start()
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]any{"error": err.Error(), "status": status})
		return
	}
	writeJSON(w, http.StatusAccepted, status)
}

func (s *server) syncStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.sync.statusSnapshot())
}

func (s *server) dataStatus(w http.ResponseWriter, r *http.Request) {
	api, ok := s.store.(interface {
		DataStatus(context.Context) (store.DataStatus, error)
	})
	if !ok {
		writeJSON(w, http.StatusNotImplemented, map[string]string{"error": "data status API unavailable"})
		return
	}
	result, err := api.DataStatus(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	status := s.sync.statusSnapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"salesOrdersLastSyncedAt":    result.SalesOrdersLastSyncedAt,
		"faultsLastSyncedAt":         result.FaultsLastSyncedAt,
		"stationRecordsLastSyncedAt": result.StationRecordsLastSyncedAt,
		"serialBindingsLastSyncedAt": result.SerialBindingsLastSyncedAt,
		"bomPostingsLastSyncedAt":    result.BOMPostingsLastSyncedAt,
		"state":                      status.State, "startedAt": status.StartedAt, "finishedAt": status.FinishedAt,
	})
}

func pagination(r *http.Request) (int, int) {
	page, _ := strconv.Atoi(r.URL.Query().Get("page"))
	if page < 1 {
		page = 1
	}
	pageSize, _ := strconv.Atoi(r.URL.Query().Get("pageSize"))
	if pageSize < 1 || pageSize > 100 {
		pageSize = 20
	}
	return page, pageSize
}

func repairFilters(q url.Values) store.Filters {
	return store.Filters{Keyword: strings.TrimSpace(q.Get("keyword")), HostBarcode: strings.TrimSpace(q.Get("hostBarcode")), DefectResponsibility: strings.TrimSpace(q.Get("defectResponsibility")), NGStation: strings.TrimSpace(q.Get("ngStation")), SalesOrder: strings.TrimSpace(q.Get("salesOrder")), ProductionOrder: strings.TrimSpace(q.Get("productionOrder")), SNS: strings.TrimSpace(q.Get("sns")), ProductionOrders: strings.TrimSpace(q.Get("productionOrders")), SalesOrders: strings.TrimSpace(q.Get("salesOrders")), DateFrom: strings.TrimSpace(q.Get("dateFrom")), DateTo: strings.TrimSpace(q.Get("dateTo")), Station: strings.TrimSpace(q.Get("station")), ProductModel: strings.TrimSpace(q.Get("productModel"))}
}

func orderFilters(q url.Values) store.OrderFilters {
	return store.OrderFilters{Keyword: strings.TrimSpace(q.Get("keyword")), Source: strings.TrimSpace(q.Get("source")), GSTRSFrom: strings.TrimSpace(q.Get("gstrsFrom")), GSTRSTo: strings.TrimSpace(q.Get("gstrsTo")), SalesOrder: strings.TrimSpace(q.Get("salesOrder")), ProductionOrder: strings.TrimSpace(q.Get("productionOrder")), SerialNumber: strings.TrimSpace(q.Get("serialNumber")), ProductModel: strings.TrimSpace(q.Get("productModel")), Customer: strings.TrimSpace(q.Get("customer")), Base: strings.TrimSpace(q.Get("base")), DateFrom: strings.TrimSpace(q.Get("dateFrom")), DateTo: strings.TrimSpace(q.Get("dateTo")), OrderScope: strings.TrimSpace(q.Get("orderScope"))}
}

func viewFilters(q url.Values) store.ViewFilters {
	return store.ViewFilters{Keyword: strings.TrimSpace(q.Get("keyword")), From: strings.TrimSpace(q.Get("from")), To: strings.TrimSpace(q.Get("to")), DateFrom: strings.TrimSpace(q.Get("dateFrom")), DateTo: strings.TrimSpace(q.Get("dateTo")), StationCode: strings.TrimSpace(q.Get("stationCode")), SN: strings.TrimSpace(q.Get("sn")), ProductionOrder: strings.TrimSpace(q.Get("productionOrder")), SalesOrder: strings.TrimSpace(q.Get("salesOrder")), Base: strings.TrimSpace(q.Get("base")), ProductModel: strings.TrimSpace(q.Get("productModel")), HeadOrder: strings.TrimSpace(q.Get("headOrder")), ItemOrder: strings.TrimSpace(q.Get("itemOrder")), HeadSN: strings.TrimSpace(q.Get("headSn")), ItemSN: strings.TrimSpace(q.Get("itemSn")), MaterialCode: strings.TrimSpace(q.Get("materialCode"))}
}

func parseBool(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "y":
		return true
	default:
		return false
	}
}

func cors(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func getenv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func loadDotEnv() {
	paths := []string{os.Getenv("ENV_FILE"), ".env", filepath.Join("..", ".env")}
	for _, path := range paths {
		if path == "" {
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(data), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			key, value, ok := strings.Cut(line, "=")
			key = strings.TrimSpace(key)
			if ok && key != "" && os.Getenv(key) == "" {
				_ = os.Setenv(key, strings.Trim(strings.TrimSpace(value), "\"'"))
			}
		}
		return
	}
}

func mongoURI() string {
	if uri := os.Getenv("MONGODB_URI"); uri != "" {
		return uri
	}
	hosts := strings.TrimSpace(os.Getenv("MONGODB_HOSTS"))
	if hosts == "" {
		return "mongodb://localhost:27017"
	}
	auth := ""
	if username, password := os.Getenv("MONGODB_USERNAME"), os.Getenv("MONGODB_PASSWORD"); username != "" || password != "" {
		auth = url.QueryEscape(username) + ":" + url.QueryEscape(password) + "@"
	}
	database := url.PathEscape(getenv("MONGODB_DATABASE", "prod_line_fault_5000"))
	query := url.Values{}
	query.Set("authSource", getenv("MONGODB_AUTH_SOURCE", getenv("MONGODB_DATABASE", "admin")))
	if replicaSet := os.Getenv("MONGODB_REPLICA_SET"); replicaSet != "" {
		query.Set("replicaSet", replicaSet)
	}
	return "mongodb://" + auth + hosts + "/" + database + "?" + query.Encode()
}
