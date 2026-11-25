package store

import (
	"context"
	"math"
	"strings"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

type KKCumulativeDelivery struct {
	SalesOrder string  `json:"salesOrder"`
	Month      string  `json:"month"`
	Quantity   float64 `json:"quantity"`
	Devices    int     `json:"devices"`
}

type KKCumulativeResult struct {
	DateTo                 string                 `json:"dateTo"`
	Items                  []KKCumulativeDelivery `json:"items"`
	Quantity               float64                `json:"quantity"`
	BatchCount             int                    `json:"batchCount"`
	MissingQuantityBatches int                    `json:"missingQuantityBatches"`
	InvalidDateBatches     int                    `json:"invalidDateBatches"`
}

func (s *Store) KKCumulativeDeliveries(ctx context.Context, dateTo string) (KKCumulativeResult, error) {
	return s.kkCumulativeDeliveries(ctx, bson.M{"$or": bson.A{
		bson.M{"WADAT": bson.M{"$lte": strings.ReplaceAll(dateTo, "-", "")}},
		bson.M{"WADAT": nil}, // Count missing dates alongside invalid date strings in the range.
	}}, dateTo)
}

func (s *Store) kkCumulativeDeliveries(ctx context.Context, filter bson.M, dateTo string) (KKCumulativeResult, error) {
	result := KKCumulativeResult{DateTo: dateTo, Items: []KKCumulativeDelivery{}}
	cursor, err := s.kkDeliveries.Aggregate(ctx, kkCumulativePipeline(filter), options.Aggregate().SetAllowDiskUse(true))
	if err != nil {
		return result, err
	}
	defer cursor.Close(ctx)

	var totalTenths float64
	for cursor.Next(ctx) {
		var row struct {
			ID struct {
				SalesOrder string `bson:"salesOrder"`
				Month      string `bson:"month"`
			} `bson:"_id"`
			Devices                int     `bson:"devices"`
			Quantity               float64 `bson:"quantity"`
			BatchCount             int     `bson:"batchCount"`
			MissingQuantityBatches int     `bson:"missingQuantityBatches"`
		}
		if err := cursor.Decode(&row); err != nil {
			return result, err
		}
		if row.ID.Month == "" {
			result.InvalidDateBatches += row.BatchCount
			continue
		}
		result.BatchCount += row.BatchCount
		result.MissingQuantityBatches += row.MissingQuantityBatches
		quantityTenths := math.Round(row.Quantity * 10)
		totalTenths += quantityTenths
		// Omit groups with neither a shipment quantity nor a serial count.
		if row.Devices != 0 || quantityTenths != 0 {
			result.Items = append(result.Items, KKCumulativeDelivery{
				SalesOrder: row.ID.SalesOrder, Month: row.ID.Month, Quantity: quantityTenths / 10, Devices: row.Devices,
			})
		}
	}
	result.Quantity = totalTenths / 10
	return result, cursor.Err()
}

func kkCumulativePipeline(filter bson.M) mongo.Pipeline {
	quantity := bson.M{"$convert": bson.M{"input": "$LFIMG", "to": "double", "onError": 0, "onNull": 0}}
	serial := bson.M{"$trim": bson.M{"input": bson.M{"$ifNull": bson.A{"$SN", ""}}}}
	parsedDate := bson.M{"$dateFromString": bson.M{
		"dateString": "$WADAT", "format": "%Y%m%d", "onError": nil, "onNull": nil,
	}}
	validDate := bson.M{"$let": bson.M{
		"vars": bson.M{"parsed": parsedDate},
		"in": bson.M{"$cond": bson.A{bson.M{"$eq": bson.A{
			bson.M{"$dateToString": bson.M{"date": "$$parsed", "format": "%Y%m%d", "onNull": ""}}, "$WADAT",
		}}, "$$parsed", nil}},
	}}
	zeroQty := bson.M{"$eq": bson.A{"$qty", 0}}
	return mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$group", Value: bson.M{
			"_id": bson.M{
				"delivery": "$VBELN_L", "salesOrder": bson.M{"$ifNull": bson.A{"$VBELN", ""}},
				"item": "$POSNR", "quantity": bson.M{"$toString": "$LFIMG"},
			},
			"qty": bson.M{"$first": quantity}, "serials": bson.M{"$addToSet": serial},
			// Choose the earliest valid WADAT if a batch spans more than one date.
			"date": bson.M{"$min": validDate},
		}}},
		{{Key: "$set", Value: bson.M{"serials": bson.M{"$setDifference": bson.A{"$serials", bson.A{""}}}}}},
		{{Key: "$group", Value: bson.M{
			"_id": bson.M{"salesOrder": "$_id.salesOrder", "month": bson.M{"$dateToString": bson.M{
				"date": "$date", "format": "%Y-%m", "onNull": "",
			}}},
			"quantity":   bson.M{"$sum": "$qty"},
			"serialSets": bson.M{"$push": "$serials"}, "batchCount": bson.M{"$sum": 1},
			"missingQuantityBatches": bson.M{"$sum": bson.M{"$cond": bson.A{zeroQty, 1, 0}}},
		}}},
		{{Key: "$project", Value: bson.M{
			"quantity": 1, "batchCount": 1, "missingQuantityBatches": 1,
			"devices": bson.M{"$size": bson.M{"$reduce": bson.M{
				"input": "$serialSets", "initialValue": bson.A{}, "in": bson.M{"$setUnion": bson.A{"$$value", "$$this"}},
			}}},
		}}},
		{{Key: "$sort", Value: bson.D{{Key: "_id.month", Value: 1}, {Key: "_id.salesOrder", Value: 1}}}},
	}
}
