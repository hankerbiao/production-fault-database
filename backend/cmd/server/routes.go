package main

import "net/http"

func registerRoutes(h *server) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /", h.root)
	mux.HandleFunc("GET /api/openapi.json", h.apiDocument)
	mux.HandleFunc("GET /api/agent-guide.md", h.apiDocument)
	mux.HandleFunc("GET /api/health", h.health)
	mux.HandleFunc("GET /api/faults", h.faults)
	mux.HandleFunc("POST /api/faults/lookup", h.faultLookup)
	mux.HandleFunc("POST /api/faults/by-sns", h.faultRowsBySNS)
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
	mux.HandleFunc("GET /api/views/{viewID}/all", h.viewListAll)
	mux.HandleFunc("GET /api/views/{viewID}/stream", h.viewStream)
	mux.HandleFunc("GET /api/views/{viewID}/detail", h.viewDetail)
	mux.HandleFunc("GET /api/views/{viewID}/stats", h.viewStats)
	mux.HandleFunc("POST /api/sync/incremental", h.startIncrementalSync)
	mux.HandleFunc("GET /api/sync/status", h.syncStatus)
	mux.HandleFunc("GET /api/data-status", h.dataStatus)
	return mux
}
