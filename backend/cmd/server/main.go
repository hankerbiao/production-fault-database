package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"production-fault-gateway/internal/store"
)

type server struct {
	store  storeAPI
	sync   *syncManager
	config serviceConfig
}

func main() {
	if err := run(); err != nil {
		log.Print(err)
		os.Exit(1)
	}
}

func run() error {
	loadDotEnv()
	uri := mongoURI()
	database := getenv("MONGODB_DATABASE", "prod_line_fault_5000")
	repairCollection := getenv("MONGODB_COLLECTION", getenv("REPAIR_COLLECTION", "repair_records_sap"))
	orderCollection := getenv("TARGET_COLLECTION", "sales_orders_sap")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	s, err := store.New(ctx, uri, database, repairCollection, orderCollection)
	if err != nil {
		return err
	}
	defer func() {
		if err := s.Close(context.Background()); err != nil {
			log.Printf("close data sources: %v", err)
		}
	}()

	h := &server{store: s, sync: newSyncManager(), config: newServiceConfig()}
	mux := registerRoutes(h)

	// Keep the default aligned with the Vite development proxy and README.
	addr := getenv("PORT", "18080")
	log.Printf("fault gateway listening on :%s", addr)
	server := &http.Server{Addr: ":" + addr, Handler: gzipJSON(cors(mux))}
	signalCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return serveUntilShutdown(signalCtx, server)
}

func serveUntilShutdown(ctx context.Context, server *http.Server) error {
	done := make(chan error, 1)
	go func() { done <- server.ListenAndServe() }()
	select {
	case err := <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			_ = server.Close()
			return err
		}
		return nil
	}
}

func (s *server) root(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	http.Redirect(w, r, getenv("FRONTEND_URL", "http://127.0.0.1:5174/"), http.StatusTemporaryRedirect)
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
