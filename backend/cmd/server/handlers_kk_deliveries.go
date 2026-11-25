package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"go.mongodb.org/mongo-driver/mongo"
	"production-fault-gateway/internal/store"
)

const maxKKDeliveryPage = 1_000_000

var (
	errInvalidKKDeliveryDate            = errors.New("日期格式必须为 YYYY-MM-DD")
	errInvalidKKDeliveryDateRange       = errors.New("开始日期不能晚于结束日期")
	errInvalidKKDeliveryExportDateRange = errors.New("导出必须设置完整的交付日期范围，且最多为 31 天（含首尾）")
	errInvalidKKDeliveryPagination      = errors.New("分页参数无效：页码须为正数，每页数量须在 1 到 100 之间")
)

func (s *server) kkDeliveryRecords(w http.ResponseWriter, r *http.Request) {
	filters, page, pageSize, err := parseKKDeliveryQuery(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	result, err := s.store.KKDeliveryRecords(ctx, filters, page, pageSize)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "无法读取 KK 销售订单交付数据"})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *server) kkDeliveryExport(w http.ResponseWriter, r *http.Request) {
	filters, _, _, err := parseKKDeliveryQuery(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if err := validateKKDeliveryExportDateRange(filters.DateFrom, filters.DateTo); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	// Build the complete CSV in a temporary file before sending headers. If
	// MongoDB fails halfway through, the client gets a 503 instead of a
	// truncated file with a misleading 200 response.
	tmp, err := os.CreateTemp("", "kk-delivery-*.csv")
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "无法创建导出文件"})
		return
	}
	defer os.Remove(tmp.Name())
	defer tmp.Close()

	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Minute)
	defer cancel()
	if err := s.store.KKDeliveryStream(ctx, filters, tmp); err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "无法生成 KK 交付导出文件"})
		return
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "无法读取导出文件"})
		return
	}
	info, err := tmp.Stat()
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "无法读取导出文件"})
		return
	}

	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition", "attachment; filename=kk-sales-order-deliveries.csv")
	w.Header().Set("Content-Length", strconv.FormatInt(info.Size()+3, 10))
	if _, err := w.Write([]byte{0xEF, 0xBB, 0xBF}); err != nil {
		return
	}
	_, _ = io.Copy(w, tmp)
}

func (s *server) kkDeliveryDetail(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	if strings.TrimSpace(id) == "" || len(id) > 1024 || !utf8.ValidString(id) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "记录 ID 无效"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	doc, err := s.store.KKDeliveryDetail(ctx, id)
	if errors.Is(err, mongo.ErrNoDocuments) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "未找到该条交付记录"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "无法读取 KK 销售订单交付详情"})
		return
	}
	writeJSON(w, http.StatusOK, doc)
}

func parseKKDeliveryQuery(r *http.Request) (store.KKDeliveryFilters, int, int, error) {
	query := r.URL.Query()
	dateFrom, err := parseKKDeliveryDate(query.Get("dateFrom"))
	if err != nil {
		return store.KKDeliveryFilters{}, 0, 0, err
	}
	dateTo, err := parseKKDeliveryDate(query.Get("dateTo"))
	if err != nil {
		return store.KKDeliveryFilters{}, 0, 0, err
	}
	if dateFrom != "" && dateTo != "" && dateFrom > dateTo {
		return store.KKDeliveryFilters{}, 0, 0, errInvalidKKDeliveryDateRange
	}

	fields := []string{"salesOrder", "materialNumber", "customerNumber", "plant", "sn"}
	values := make([]string, len(fields))
	for index, field := range fields {
		value := strings.TrimSpace(query.Get(field))
		if utf8.RuneCountInString(value) > 100 {
			return store.KKDeliveryFilters{}, 0, 0, fmt.Errorf("%s 长度不能超过 100 个字符", field)
		}
		values[index] = value
	}
	page, err := parseKKDeliveryPositiveInt(query.Get("page"), 1, maxKKDeliveryPage)
	if err != nil {
		return store.KKDeliveryFilters{}, 0, 0, err
	}
	pageSize, err := parseKKDeliveryPositiveInt(query.Get("pageSize"), 20, store.MaxKKDeliveryPageSize)
	if err != nil {
		return store.KKDeliveryFilters{}, 0, 0, err
	}
	return store.KKDeliveryFilters{
		DateFrom: dateFrom, DateTo: dateTo, SalesOrder: values[0],
		MaterialNumber: values[1], CustomerNumber: values[2], Plant: values[3], SN: values[4],
	}, page, pageSize, nil
}

func validateKKDeliveryExportDateRange(dateFrom, dateTo string) error {
	if dateFrom == "" || dateTo == "" {
		return errInvalidKKDeliveryExportDateRange
	}
	from, _ := time.Parse("2006-01-02", dateFrom)
	to, _ := time.Parse("2006-01-02", dateTo)
	if to.Sub(from) > 30*24*time.Hour {
		return errInvalidKKDeliveryExportDateRange
	}
	return nil
}

func parseKKDeliveryDate(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", nil
	}
	parsed, err := time.Parse("2006-01-02", value)
	if err != nil || parsed.Format("2006-01-02") != value {
		return "", errInvalidKKDeliveryDate
	}
	return value, nil
}

func parseKKDeliveryPositiveInt(value string, fallback, maximum int) (int, error) {
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 1 || parsed > maximum {
		return 0, errInvalidKKDeliveryPagination
	}
	return parsed, nil
}
