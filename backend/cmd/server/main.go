package main

import (
	"context"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"production-fault-gateway/internal/store"
)

type server struct {
	store storeAPI
	sync  *syncManager
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
	mux := registerRoutes(h)

	// Keep the default aligned with the Vite development proxy and README.
	addr := getenv("PORT", "18080")
	log.Printf("fault gateway listening on :%s", addr)
	log.Fatal(http.ListenAndServe(":"+addr, gzipJSON(cors(mux))))
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
