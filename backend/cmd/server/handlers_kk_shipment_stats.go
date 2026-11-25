package main

import (
	"context"
	"net/http"
	"sync"
	"time"

	"production-fault-gateway/internal/store"
)

// kkShipmentStatsTTL keeps repeated dashboard loads off MongoDB. The shipment
// statistics are a month-level denominator, so a minute of staleness is safe.
const kkShipmentStatsTTL = 60 * time.Second

type kkShipmentStatsEntry struct {
	expires time.Time
	stats   store.KKShipmentStats
}

var kkShipmentStatsCache = struct {
	sync.Mutex
	entries map[string]kkShipmentStatsEntry
}{entries: map[string]kkShipmentStatsEntry{}}

func (s *server) kkDeliveryShipmentStats(w http.ResponseWriter, r *http.Request) {
	filters, _, _, err := parseKKDeliveryQuery(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if filters.DateFrom == "" || filters.DateTo == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "统计必须设置完整的交付日期范围"})
		return
	}
	if filters.SalesOrder != "" || filters.MaterialNumber != "" || filters.CustomerNumber != "" ||
		filters.Plant != "" || filters.SN != "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "出货统计仅支持交付日期范围"})
		return
	}

	key := filters.DateFrom + "|" + filters.DateTo
	now := time.Now()
	kkShipmentStatsCache.Lock()
	if entry, ok := kkShipmentStatsCache.entries[key]; ok && now.Before(entry.expires) {
		kkShipmentStatsCache.Unlock()
		writeJSON(w, http.StatusOK, entry.stats)
		return
	}
	kkShipmentStatsCache.Unlock()

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	stats, err := s.store.KKDeliveryShipmentStats(ctx, filters)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "无法统计 KK 出货数据"})
		return
	}

	kkShipmentStatsCache.Lock()
	for cacheKey, entry := range kkShipmentStatsCache.entries {
		if now.After(entry.expires) {
			delete(kkShipmentStatsCache.entries, cacheKey)
		}
	}
	kkShipmentStatsCache.entries[key] = kkShipmentStatsEntry{expires: now.Add(kkShipmentStatsTTL), stats: stats}
	kkShipmentStatsCache.Unlock()

	writeJSON(w, http.StatusOK, stats)
}
