package store

import (
	"context"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

// KKUnknownModel is the bucket used when a delivery cannot be tied back to a
// sales-order model. The API reports it separately so callers never mistake it
// for a real production model.
const KKUnknownModel = "未知机型"

// KKShipmentMonth is one delivery month. Quantity is the shipped unit count
// (LFIMG summed once per delivery batch); Devices is the distinct serial count.
type KKShipmentMonth struct {
	Month    string  `json:"month"`
	Quantity float64 `json:"quantity"`
	Devices  int     `json:"devices"`
}

// KKShipmentModel is one production model, measured the same way.
type KKShipmentModel struct {
	Model    string  `json:"model"`
	Quantity float64 `json:"quantity"`
	Devices  int     `json:"devices"`
}

// KKShipmentStats is the shipment denominator of the MFR dashboard: delivered
// units in a WADAT window, summed from LFIMG once per delivery batch.
type KKShipmentStats struct {
	DateFrom          string            `json:"dateFrom"`
	DateTo            string            `json:"dateTo"`
	RecordCount       int               `json:"recordCount"`
	DeviceCount       int               `json:"deviceCount"`
	Quantity          float64           `json:"quantity"`
	BatchCount        int               `json:"batchCount"`
	MissingQtyBatches int               `json:"missingQuantityBatches"`
	MissingSerial     int               `json:"missingSerialCount"`
	UndatedDevices    int               `json:"undatedDeviceCount"`
	FallbackModel     int               `json:"fallbackModelCount"`
	UnknownModel      int               `json:"unknownModelCount"`
	SalesOrderCount   int               `json:"salesOrderCount"`
	MatchedOrderCount int               `json:"matchedOrderCount"`
	ByMonth           []KKShipmentMonth `json:"byMonth"`
	ByModel           []KKShipmentModel `json:"byModel"`
	GeneratedAt       string            `json:"generatedAt"`
}

// KKShipmentDataError means a precise denominator cannot be calculated for the window.
type KKShipmentDataError struct {
	InvalidItems   int `json:"invalidItems"`
	QuantityIssues int `json:"quantityIssues"`
}

func (e *KKShipmentDataError) Error() string {
	return fmt.Sprintf("KK 出货数据有 %d 个无法确认的交付项，其中 %d 个数量缺失、无效或冲突", e.InvalidItems, e.QuantityIssues)
}

type kkShipmentBatch struct {
	SalesOrder string
	Material   string
	Quantity   float64
	Serials    []string
	DeliveryAt string
}

func (s *Store) KKDeliveryShipmentStats(ctx context.Context, filters KKDeliveryFilters) (KKShipmentStats, error) {
	stats := KKShipmentStats{
		DateFrom: filters.DateFrom, DateTo: filters.DateTo,
		GeneratedAt: time.Now().Format(time.RFC3339),
	}
	cursor, err := s.kkDeliveries.Aggregate(ctx, kkShipmentPipeline(filters),
		options.Aggregate().SetAllowDiskUse(true).SetBatchSize(500))
	if err != nil {
		return stats, err
	}
	defer cursor.Close(ctx)

	var batches []kkShipmentBatch
	var issues KKShipmentDataError
	devices := kkSerialSet{}
	for cursor.Next(ctx) {
		var row struct {
			ID struct {
				Delivery string `bson:"delivery"`
				Item     string `bson:"item"`
			} `bson:"_id"`
			Orders        []string `bson:"orders"`
			Materials     []string `bson:"materials"`
			Dates         []string `bson:"dates"`
			Serials       []string `bson:"serials"`
			Min           float64  `bson:"min"`
			Max           float64  `bson:"max"`
			InvalidQty    int      `bson:"invalidQty"`
			MissingSerial int      `bson:"missingSerial"`
			Rows          int      `bson:"rows"`
		}
		if err := cursor.Decode(&row); err != nil {
			return stats, err
		}
		stats.RecordCount += row.Rows
		stats.MissingSerial += row.MissingSerial
		badQuantity := row.InvalidQty > 0 || row.Min != row.Max || row.Min < 0 ||
			math.IsNaN(row.Min) || math.IsInf(row.Min, 0) || math.Trunc(row.Min) != row.Min
		if badQuantity {
			issues.QuantityIssues++
		}
		validDate := false
		if len(row.Dates) == 1 {
			_, err := time.Parse("20060102", row.Dates[0])
			validDate = err == nil
		}
		if badQuantity || !validDate || strings.TrimSpace(row.ID.Delivery) == "" || strings.TrimSpace(row.ID.Item) == "" {
			issues.InvalidItems++
			continue
		}

		serials := make([]string, 0, len(row.Serials))
		for _, sn := range row.Serials {
			if sn != "" {
				serials = append(serials, sn)
				devices[sn] = struct{}{}
			}
		}
		batch := kkShipmentBatch{Quantity: row.Min, Serials: serials, DeliveryAt: row.Dates[0]}
		if len(row.Orders) == 1 && len(row.Materials) == 1 {
			batch.SalesOrder, batch.Material = row.Orders[0], row.Materials[0]
		}
		batches = append(batches, batch)
	}
	if err := cursor.Err(); err != nil {
		return stats, err
	}
	if issues.InvalidItems != 0 {
		return stats, &issues
	}
	stats.DeviceCount = len(devices)

	resolved, err := s.resolveKKShipmentModels(ctx, batches, &stats)
	if err != nil {
		return stats, err
	}
	return summarizeKKShipments(batches, resolved, stats), nil
}

func kkShipmentPipeline(filters KKDeliveryFilters) mongo.Pipeline {
	clean := func(field string) bson.M {
		return bson.M{"$trim": bson.M{"input": bson.M{"$ifNull": bson.A{"$" + field, ""}}}}
	}
	countIf := func(condition bson.M) bson.M {
		return bson.M{"$sum": bson.M{"$cond": bson.A{condition, 1, 0}}}
	}
	return mongo.Pipeline{
		{{Key: "$match", Value: kkDeliveryFilter(filters)}},
		{{Key: "$project", Value: bson.M{
			"delivery": clean("VBELN_L"), "item": clean("POSNR_L"), "order": clean("VBELN"),
			"material": clean("MATNR"), "serial": clean("SN"), "date": "$WADAT",
			"qty": bson.M{"$convert": bson.M{"input": "$LFIMG", "to": "double", "onError": nil, "onNull": nil}},
		}}},
		{{Key: "$group", Value: bson.M{
			"_id":    bson.M{"delivery": "$delivery", "item": "$item"},
			"orders": bson.M{"$addToSet": "$order"}, "materials": bson.M{"$addToSet": "$material"},
			"dates": bson.M{"$addToSet": "$date"}, "serials": bson.M{"$addToSet": "$serial"},
			"min": bson.M{"$min": "$qty"}, "max": bson.M{"$max": "$qty"}, "rows": bson.M{"$sum": 1},
			"invalidQty":    countIf(bson.M{"$eq": bson.A{"$qty", nil}}),
			"missingSerial": countIf(bson.M{"$eq": bson.A{"$serial", ""}}),
		}}},
	}
}

type kkSerialSet map[string]struct{}
type kkModelSet map[string]struct{}

func (m kkModelSet) unique() string {
	if len(m) == 1 {
		for model := range m {
			return model
		}
	}
	return ""
}

type kkOrderItem struct{ order, material string }

func (s *Store) resolveKKShipmentModels(ctx context.Context, batches []kkShipmentBatch, stats *KKShipmentStats) ([]string, error) {
	orderModels, err := s.kkOrderItemModels(ctx, batches)
	if err != nil {
		return nil, err
	}
	resolved := make([]string, len(batches))
	matchedOrders := map[string]struct{}{}
	pending := kkSerialSet{}
	for i, batch := range batches {
		resolved[i] = orderModels[kkOrderItem{batch.SalesOrder, strings.TrimLeft(batch.Material, "0")}].unique()
		if resolved[i] != "" {
			matchedOrders[batch.SalesOrder] = struct{}{}
		} else if batch.Quantity > 0 && float64(len(batch.Serials)) == batch.Quantity {
			for _, sn := range batch.Serials {
				pending[sn] = struct{}{}
			}
		}
	}
	stats.MatchedOrderCount = len(matchedOrders)
	serials := make([]string, 0, len(pending))
	for sn := range pending {
		serials = append(serials, sn)
	}
	station, err := s.stationModels(ctx, serials)
	if err != nil {
		return nil, err
	}
	for i, batch := range batches {
		if resolved[i] != "" || batch.Quantity <= 0 || float64(len(batch.Serials)) != batch.Quantity {
			continue
		}
		model := ""
		for _, sn := range batch.Serials {
			candidate := station[sn]
			if candidate == "" || (model != "" && model != candidate) {
				model = ""
				break
			}
			model = candidate
		}
		if model != "" {
			resolved[i] = model
			stats.FallbackModel += int(batch.Quantity)
		}
	}
	return resolved, nil
}

func (s *Store) kkOrderItemModels(ctx context.Context, batches []kkShipmentBatch) (map[kkOrderItem]kkModelSet, error) {
	orders := map[string]struct{}{}
	for _, batch := range batches {
		if batch.SalesOrder != "" && batch.Material != "" {
			orders[batch.SalesOrder] = struct{}{}
		}
	}
	models := map[kkOrderItem]kkModelSet{}
	if len(orders) == 0 {
		return models, nil
	}
	values := make([]string, 0, len(orders))
	for order := range orders {
		values = append(values, order)
	}
	cursor, err := s.orders.Find(ctx, bson.M{"data.VBELN": bson.M{"$in": values}},
		options.Find().SetProjection(bson.M{"_id": 0, "data.VBELN": 1, "data.MATNR": 1, "data.MAKTX_TH": 1}))
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)
	for cursor.Next(ctx) {
		var row struct {
			Data struct {
				Order    string `bson:"VBELN"`
				Material string `bson:"MATNR"`
				Model    string `bson:"MAKTX_TH"`
			} `bson:"data"`
		}
		if err := cursor.Decode(&row); err != nil {
			return nil, err
		}
		material := strings.TrimLeft(strings.TrimSpace(row.Data.Material), "0")
		model := strings.TrimSpace(row.Data.Model)
		if material != "" && model != "" {
			key := kkOrderItem{strings.TrimSpace(row.Data.Order), material}
			if models[key] == nil {
				models[key] = kkModelSet{}
			}
			models[key][model] = struct{}{}
		}
	}
	return models, cursor.Err()
}

// stationModels resolves production models from station records by serial
// number. Only batches the sales board could not resolve reach this method.
func (s *Store) stationModels(ctx context.Context, serials []string) (map[string]string, error) {
	if len(serials) == 0 {
		return map[string]string{}, nil
	}
	station := s.views["Z_V_ZMES_T_001"]
	if station == nil {
		return map[string]string{}, nil
	}
	const chunkSize = 1000
	models := make(map[string]string, len(serials))
	for start := 0; start < len(serials); start += chunkSize {
		end := start + chunkSize
		if end > len(serials) {
			end = len(serials)
		}
		cursor, err := station.Aggregate(ctx, mongo.Pipeline{
			bson.D{{Key: "$match", Value: bson.M{"PCODE": bson.M{"$in": serials[start:end]}}}},
			bson.D{{Key: "$group", Value: bson.M{"_id": "$PCODE", "models": bson.M{"$addToSet": "$MAKTX_TH"}}}},
		}, options.Aggregate().SetAllowDiskUse(true))
		if err != nil {
			return nil, err
		}
		for cursor.Next(ctx) {
			var row struct {
				Serial string   `bson:"_id"`
				Models []string `bson:"models"`
			}
			if err := cursor.Decode(&row); err != nil {
				cursor.Close(ctx)
				return nil, err
			}
			candidates := kkModelSet{}
			for _, value := range row.Models {
				if model := strings.TrimSpace(value); model != "" {
					candidates[model] = struct{}{}
				}
			}
			if model := candidates.unique(); model != "" {
				models[row.Serial] = model
			}
		}
		if err := cursor.Err(); err != nil {
			cursor.Close(ctx)
			return nil, err
		}
		cursor.Close(ctx)
	}
	return models, nil
}

// summarizeKKShipments folds the delivery batches into month and model
// quantities. It is pure so the counting rules can be tested without MongoDB.
func summarizeKKShipments(batches []kkShipmentBatch, models []string, stats KKShipmentStats) KKShipmentStats {
	monthQty := map[string]float64{}
	monthDevices := map[string]kkSerialSet{}
	modelQty := map[string]float64{}
	modelDevices := map[string]kkSerialSet{}
	undated := 0
	orders := make(map[string]struct{}, len(batches))
	for i, batch := range batches {
		month := ""
		if len(batch.DeliveryAt) >= 6 {
			month = batch.DeliveryAt[:4] + "-" + batch.DeliveryAt[4:6]
		}
		if month == "" {
			undated += len(batch.Serials)
		} else {
			monthQty[month] += batch.Quantity
			addKKSerials(monthDevices, month, batch.Serials)
		}
		model := ""
		if i < len(models) {
			model = models[i]
		}
		if model == "" {
			model = KKUnknownModel
		}
		modelQty[model] += batch.Quantity
		addKKSerials(modelDevices, model, batch.Serials)
		if batch.SalesOrder != "" {
			orders[batch.SalesOrder] = struct{}{}
		}

		stats.Quantity += batch.Quantity
	}
	stats.BatchCount = len(batches)
	stats.UndatedDevices = undated
	stats.SalesOrderCount = len(orders)
	stats.UnknownModel = int(modelQty[KKUnknownModel])

	stats.ByMonth = make([]KKShipmentMonth, 0, len(monthQty))
	months := make([]string, 0, len(monthQty))
	for month := range monthQty {
		months = append(months, month)
	}
	sort.Strings(months)
	for _, month := range months {
		stats.ByMonth = append(stats.ByMonth, KKShipmentMonth{
			Month:    month,
			Quantity: monthQty[month],
			Devices:  len(monthDevices[month]),
		})
	}

	stats.ByModel = make([]KKShipmentModel, 0, len(modelQty))
	modelNames := make([]string, 0, len(modelQty))
	for model := range modelQty {
		modelNames = append(modelNames, model)
	}
	sort.Slice(modelNames, func(i, j int) bool {
		if modelQty[modelNames[i]] != modelQty[modelNames[j]] {
			return modelQty[modelNames[i]] > modelQty[modelNames[j]]
		}
		return modelNames[i] < modelNames[j]
	})
	for _, model := range modelNames {
		stats.ByModel = append(stats.ByModel, KKShipmentModel{
			Model:    model,
			Quantity: modelQty[model],
			Devices:  len(modelDevices[model]),
		})
	}
	return stats
}

// kkFilterWith adds one predicate to an existing delivery filter without
// nesting another $and level, so index selection keeps working.
func kkFilterWith(filter bson.M, extra bson.M) bson.M {
	if len(extra) == 0 {
		return filter
	}
	if len(filter) == 0 {
		return bson.M{"$and": bson.A{extra}}
	}
	if conditions, ok := filter["$and"].(bson.A); ok {
		merged := make(bson.A, 0, len(conditions)+1)
		merged = append(merged, conditions...)
		merged = append(merged, extra)
		return bson.M{"$and": merged}
	}
	return bson.M{"$and": bson.A{filter, extra}}
}

func addKKSerials(groups map[string]kkSerialSet, key string, serials []string) {
	if groups[key] == nil {
		groups[key] = kkSerialSet{}
	}
	for _, sn := range serials {
		groups[key][sn] = struct{}{}
	}
}
