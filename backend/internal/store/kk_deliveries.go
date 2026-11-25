package store

import (
	"context"
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

const MaxKKDeliveryPageSize = 100

func (s *Store) KKDeliveryRecords(ctx context.Context, filters KKDeliveryFilters, page, pageSize int) (KKDeliveryListResult, error) {
	filter := kkDeliveryFilter(filters)
	pipeline := kkDeliveryPagePipeline(filter, s.kkDeliveries.Name(), page, pageSize)
	cursor, err := s.kkDeliveries.Aggregate(ctx, pipeline, options.Aggregate().SetAllowDiskUse(true))
	if err != nil {
		return KKDeliveryListResult{}, err
	}
	defer cursor.Close(ctx)
	var results []struct {
		Items []KKDelivery `bson:"items"`
		Total []struct {
			Count int64 `bson:"total"`
		} `bson:"total"`
	}
	if err := cursor.All(ctx, &results); err != nil {
		return KKDeliveryListResult{}, err
	}
	items := make([]KKDelivery, 0, pageSize)
	var total int64
	if len(results) > 0 {
		items = results[0].Items
		if len(results[0].Total) > 0 {
			total = results[0].Total[0].Count
		}
	}

	collectionEmpty := false
	if total == 0 {
		err := s.kkDeliveries.FindOne(ctx, bson.D{}, options.FindOne().SetProjection(bson.M{"_id": 1})).Err()
		if errors.Is(err, mongo.ErrNoDocuments) {
			collectionEmpty = true
		} else if err != nil {
			return KKDeliveryListResult{}, err
		}
	}
	return KKDeliveryListResult{Items: items, Page: page, PageSize: pageSize, Total: total, CollectionEmpty: collectionEmpty}, nil
}

func (s *Store) KKDeliveryStream(ctx context.Context, filters KKDeliveryFilters, out io.Writer) error {
	pipeline := kkDeliveryDedupPipeline(kkDeliveryFilter(filters))
	cursor, err := s.kkDeliveries.Aggregate(ctx, pipeline, options.Aggregate().SetAllowDiskUse(true).SetBatchSize(1000))
	if err != nil {
		return err
	}
	defer cursor.Close(ctx)

	writer := csv.NewWriter(out)
	writer.UseCRLF = true
	if err := writer.Write([]string{"销售订单号", "物料号", "交付日期", "数据时间（DATUM）", "客户号", "工厂", "设备序列号（SERNR）", "SN"}); err != nil {
		return err
	}
	for cursor.Next(ctx) {
		var doc bson.M
		if err := cursor.Decode(&doc); err != nil {
			return err
		}
		if err := writer.Write([]string{
			kkDeliveryCSVValue(doc["VBELN"]), kkDeliveryCSVValue(doc["MATNR"]),
			kkDeliveryCSVDate(doc["WADAT"]), kkDeliveryCSVDate(doc["DATUM"]), kkDeliveryCSVValue(doc["KUNNR"]),
			kkDeliveryCSVValue(doc["WERKS"]), kkDeliveryCSVValue(doc["SERNR"]),
			kkDeliveryCSVValue(doc["SN"]),
		}); err != nil {
			return err
		}
	}
	if err := cursor.Err(); err != nil {
		return err
	}
	writer.Flush()
	return writer.Error()
}

func kkDeliveryDedupPipeline(filter bson.M) mongo.Pipeline {
	pipeline := mongo.Pipeline{}
	if len(filter) > 0 {
		pipeline = append(pipeline, bson.D{{Key: "$match", Value: filter}})
	}
	return append(pipeline,
		bson.D{{Key: "$sort", Value: bson.D{{Key: "DATUM", Value: -1}, {Key: "_id", Value: -1}}}},
		// Narrow the documents before grouping. The source documents carry the
		// full SAP payload; projecting here cuts aggregation memory and network
		// traffic substantially on the large delivery collection.
		kkDeliveryProjection(),
		bson.D{{Key: "$group", Value: bson.D{
			{Key: "_id", Value: kkDeliveryGroupKey()},
			{Key: "record", Value: bson.D{{Key: "$first", Value: "$$ROOT"}}},
		}}},
		bson.D{{Key: "$replaceRoot", Value: bson.D{{Key: "newRoot", Value: "$record"}}}},
		bson.D{{Key: "$sort", Value: bson.D{{Key: "DATUM", Value: -1}, {Key: "_id", Value: -1}}}},
	)
}

// kkDeliveryPagePipeline computes one latest record per SN with $max instead
// of sorting every source document. It then sorts only the small group keys
// and fetches full documents for the requested page through a bounded
// $lookup. This keeps unfiltered pages materially cheaper than the old
// sort-everything pipeline while preserving the latest-DATUM tie rule.
func kkDeliveryPagePipeline(filter bson.M, collection string, page, pageSize int) mongo.Pipeline {
	pipeline := mongo.Pipeline{}
	if len(filter) > 0 {
		pipeline = append(pipeline, bson.D{{Key: "$match", Value: filter}})
	}
	return append(pipeline,
		bson.D{{Key: "$group", Value: bson.D{
			{Key: "_id", Value: kkDeliveryGroupKey()},
			{Key: "latest", Value: bson.D{{Key: "$max", Value: bson.D{
				{Key: "DATUM", Value: "$DATUM"},
				{Key: "_id", Value: "$_id"},
			}}}},
		}}},
		bson.D{{Key: "$facet", Value: bson.D{
			{Key: "items", Value: mongo.Pipeline{
				{{Key: "$sort", Value: bson.D{{Key: "latest.DATUM", Value: -1}, {Key: "latest._id", Value: -1}}}},
				{{Key: "$skip", Value: int64((page - 1) * pageSize)}},
				{{Key: "$limit", Value: int64(pageSize)}},
				{{Key: "$lookup", Value: bson.D{
					{Key: "from", Value: collection},
					{Key: "localField", Value: "latest._id"},
					{Key: "foreignField", Value: "_id"},
					{Key: "as", Value: "doc"},
				}}},
				{{Key: "$unwind", Value: "$doc"}},
				{{Key: "$replaceRoot", Value: bson.D{{Key: "newRoot", Value: "$doc"}}}},
			}},
			{Key: "total", Value: mongo.Pipeline{{{Key: "$count", Value: "total"}}}},
		}}},
	)
}

func kkDeliveryGroupKey() bson.M {
	sn := bson.M{"$ifNull": bson.A{"$SN", ""}}
	return bson.M{
		"sn":  sn,
		"row": bson.M{"$cond": bson.A{bson.M{"$eq": bson.A{sn, ""}}, "$_id", nil}},
	}
}

func kkDeliveryProjection() bson.D {
	return bson.D{{Key: "$project", Value: bson.M{
		"_id": 1, "VBELN": 1, "MATNR": 1, "WADAT": 1, "DATUM": 1,
		"KUNNR": 1, "WERKS": 1, "SERNR": 1, "SN": 1,
	}}}
}

func kkDeliveryCSVValue(value any) string {
	if value == nil {
		return ""
	}
	text := fmt.Sprint(value)
	// Neutralize spreadsheet formulas. A leading minus is only safe when the
	// whole value is a plain number; values such as "-1+1" are formulas.
	if strings.HasPrefix(text, "=") || strings.HasPrefix(text, "+") || strings.HasPrefix(text, "@") {
		return "'" + text
	}
	if strings.HasPrefix(text, "-") {
		if _, err := strconv.ParseFloat(text, 64); err != nil {
			return "'" + text
		}
	}
	return text
}

func kkDeliveryCSVDate(value any) string {
	text := kkDeliveryCSVValue(value)
	if len(text) == 8 && strings.Trim(text, "0123456789") == "" {
		return text[:4] + "-" + text[4:6] + "-" + text[6:]
	}
	return text
}

func (s *Store) KKDeliveryDetail(ctx context.Context, id string) (bson.M, error) {
	var doc bson.M
	err := s.kkDeliveries.FindOne(ctx, bson.M{"_id": id}).Decode(&doc)
	return doc, err
}

func kkDeliveryFilter(filters KKDeliveryFilters) bson.M {
	conditions := make(bson.A, 0, 7)
	dateRange := bson.M{}
	if filters.DateFrom != "" {
		dateRange["$gte"] = strings.ReplaceAll(filters.DateFrom, "-", "")
	}
	if filters.DateTo != "" {
		dateRange["$lte"] = strings.ReplaceAll(filters.DateTo, "-", "")
	}
	if len(dateRange) > 0 {
		conditions = append(conditions, bson.M{"WADAT": dateRange})
	}
	if filters.SalesOrder != "" {
		conditions = append(conditions, bson.M{"VBELN": filters.SalesOrder})
	}
	if filters.MaterialNumber != "" {
		conditions = append(conditions, bson.M{"MATNR": bson.M{"$in": kkMaterialCandidates(filters.MaterialNumber)}})
	}
	if filters.CustomerNumber != "" {
		conditions = append(conditions, bson.M{"KUNNR": filters.CustomerNumber})
	}
	if filters.Plant != "" {
		conditions = append(conditions, bson.M{"WERKS": filters.Plant})
	}
	if filters.SN != "" {
		conditions = append(conditions, bson.M{"SN": filters.SN})
	}
	if len(conditions) == 0 {
		return bson.M{}
	}
	return bson.M{"$and": conditions}
}

// kkMaterialCandidates returns the exact MATNR representations that may be
// stored for a user-supplied material number. SAP MATNR values are normally
// 18 characters with leading zeros; users often type the short form. Using an
// $in list lets MongoDB use the MATNR index instead of scanning with ^0* regex.
func kkMaterialCandidates(input string) []string {
	trimmed := strings.TrimSpace(input)
	if trimmed == "" {
		return nil
	}
	normalized := strings.TrimLeft(trimmed, "0")
	if normalized == "" {
		normalized = "0"
	}
	seen := make(map[string]struct{}, 4)
	result := make([]string, 0, 4)
	add := func(value string) {
		if value == "" || len(value) > 18 {
			return
		}
		if _, ok := seen[value]; ok {
			return
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	add(trimmed)
	add(normalized)
	add(fmt.Sprintf("%018s", trimmed))
	add(fmt.Sprintf("%018s", normalized))
	return result
}
