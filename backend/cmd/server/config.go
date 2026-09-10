package main

import (
	"context"
	"net/http"
	"net/url"
	"runtime"
	"strings"
	"time"
)

const serviceVersion = "0.1.0"

type serviceConfig struct {
	ServiceName   string             `json:"serviceName"`
	Version       string             `json:"version"`
	Environment   string             `json:"environment"`
	Runtime       string             `json:"runtime"`
	ListenAddress string             `json:"listenAddress"`
	FrontendURL   string             `json:"frontendURL"`
	APIBasePath   string             `json:"apiBasePath"`
	Database      databaseConfig     `json:"database"`
	Collections   []collectionConfig `json:"collections"`
	Features      []featureConfig    `json:"features"`
}

type databaseConfig struct {
	Address           string `json:"address"`
	Name              string `json:"name"`
	AuthSource        string `json:"authSource"`
	ReplicaSet        string `json:"replicaSet"`
	AuthenticationSet bool   `json:"authenticationConfigured"`
	TLS               bool   `json:"tls"`
	Status            string `json:"status"`
}

type collectionConfig struct {
	Label string `json:"label"`
	Name  string `json:"name"`
}

type featureConfig struct {
	Label   string `json:"label"`
	Enabled bool   `json:"enabled"`
}

func newServiceConfig() serviceConfig {
	database := getenv("MONGODB_DATABASE", "prod_line_fault_5000")
	repairCollection := getenv("MONGODB_COLLECTION", getenv("REPAIR_COLLECTION", "repair_records_sap"))
	orderCollection := getenv("TARGET_COLLECTION", "sales_orders_sap")
	uri := mongoURI()

	address, tls, uriHasCredentials := mongoConnectionDetails(uri)
	return serviceConfig{
		ServiceName:   "Production Fault Gateway",
		Version:       serviceVersion,
		Environment:   getenv("APP_ENV", getenv("ENVIRONMENT", "未设置")),
		Runtime:       runtime.Version() + " / " + runtime.GOOS + " / " + runtime.GOARCH,
		ListenAddress: ":" + getenv("PORT", "18080"),
		FrontendURL:   getenv("FRONTEND_URL", "http://127.0.0.1:5173/"),
		APIBasePath:   "/api",
		Database: databaseConfig{
			Address:           address,
			Name:              database,
			AuthSource:        getenv("MONGODB_AUTH_SOURCE", database),
			ReplicaSet:        getenv("MONGODB_REPLICA_SET", "未设置"),
			AuthenticationSet: osValueSet("MONGODB_USERNAME") || osValueSet("MONGODB_PASSWORD") || uriHasCredentials,
			TLS:               tls,
			Status:            "检查中",
		},
		Collections: []collectionConfig{
			{Label: "维修故障", Name: repairCollection},
			{Label: "销售订单", Name: orderCollection},
			{Label: "工位记录", Name: "station_records_sap"},
			{Label: "序列号绑定", Name: "serial_bindings_sap"},
			{Label: "订单 BOM 过账", Name: "order_bom_postings_sap"},
			{Label: "SCS DOA", Name: "scs_doa_records"},
			{Label: "SCS 换上换下", Name: "scs_change_records"},
		},
		Features: []featureConfig{
			{Label: "分页查询（每页 20 条）", Enabled: true},
			{Label: "增量同步", Enabled: true},
			{Label: "API 文档", Enabled: true},
			{Label: "TSV / CSV 导出", Enabled: true},
		},
	}
}

func osValueSet(key string) bool {
	return getenv(key, "") != ""
}

func mongoConnectionDetails(uri string) (address string, tls, hasCredentials bool) {
	address = "未设置"
	parsed, err := url.Parse(uri)
	if err != nil {
		return address, false, false
	}
	if parsed.Host != "" {
		address = parsed.Host
	}
	if parsed.User != nil {
		hasCredentials = true
	}
	tls = parsed.Scheme == "mongodb+srv"
	if value := strings.ToLower(parsed.Query().Get("tls")); value == "true" || value == "1" || value == "yes" {
		tls = true
	}
	if value := strings.ToLower(parsed.Query().Get("ssl")); value == "true" || value == "1" || value == "yes" {
		tls = true
	}
	return address, tls, hasCredentials
}

func (s *server) serviceConfig(w http.ResponseWriter, r *http.Request) {
	config := s.config
	if config.ServiceName == "" {
		config = newServiceConfig()
	}
	config.Database.Status = "不可用"
	if s.store != nil {
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		if err := s.store.Ping(ctx); err == nil {
			config.Database.Status = "已连接"
		}
	}
	writeJSON(w, http.StatusOK, config)
}
