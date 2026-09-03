package store

import (
	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
)

func repairStatsPipeline(filter bson.M, timeField string) mongo.Pipeline {
	nonEmpty := func(field string) bson.M {
		return bson.M{"$ne": bson.A{bson.M{"$ifNull": bson.A{"$" + field, ""}}, ""}}
	}
	missing := func(field string) bson.M {
		return bson.M{"$eq": bson.A{bson.M{"$ifNull": bson.A{"$" + field, ""}}, ""}}
	}
	dateField := "$ZDATE_WX"
	if timeField == "planned" {
		dateField = "$GSTRS"
	}
	return mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$group", Value: bson.M{
			"_id": nil, "total": bson.M{"$sum": 1},
			"withError":              bson.M{"$sum": bson.M{"$cond": bson.A{bson.M{"$or": bson.A{nonEmpty("ERROR_CODE"), nonEmpty("ERROR_MSG")}}, 1, 0}}},
			"withRepairPerson":       bson.M{"$sum": bson.M{"$cond": bson.A{nonEmpty("U_FIX"), 1, 0}}},
			"missingSalesOrder":      bson.M{"$sum": bson.M{"$cond": bson.A{missing("VBELN"), 1, 0}}},
			"missingProductionOrder": bson.M{"$sum": bson.M{"$cond": bson.A{missing("AUFNR"), 1, 0}}},
			"salesOrderValues":       bson.M{"$addToSet": "$VBELN"}, "productionOrderValues": bson.M{"$addToSet": "$AUFNR"}, "hostBarcodeValues": bson.M{"$addToSet": "$PCODE"},
			"dataStartDate": bson.M{"$min": dateField}, "dataEndDate": bson.M{"$max": dateField}, "latestSyncedAt": bson.M{"$max": "$_synced_at"},
		}}},
		{{Key: "$project", Value: bson.M{
			"_id": 0, "total": 1, "withError": 1, "withRepairPerson": 1, "missingSalesOrder": 1, "missingProductionOrder": 1, "dataStartDate": 1, "dataEndDate": 1,
			"salesOrders":      bson.M{"$size": bson.M{"$setDifference": bson.A{"$salesOrderValues", bson.A{"", nil}}}},
			"productionOrders": bson.M{"$size": bson.M{"$setDifference": bson.A{"$productionOrderValues", bson.A{"", nil}}}},
			"hostBarcodes":     bson.M{"$size": bson.M{"$setDifference": bson.A{"$hostBarcodeValues", bson.A{"", nil}}}},
			"latestSyncedAt":   bson.M{"$dateToString": bson.M{"date": "$latestSyncedAt", "format": "%Y-%m-%dT%H:%M:%SZ", "timezone": "UTC"}},
		}}},
	}
}

func orderStatsPipeline(filter bson.M) mongo.Pipeline {
	normalizedVBELN := bson.M{"$trim": bson.M{"input": bson.M{"$convert": bson.M{"input": "$data.VBELN", "to": "string", "onError": "", "onNull": ""}}}}
	return mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$group", Value: bson.M{
			"_id": nil, "total": bson.M{"$sum": 1}, "salesOrderValues": bson.M{"$addToSet": normalizedVBELN},
			"sg":              bson.M{"$sum": bson.M{"$cond": bson.A{bson.M{"$eq": bson.A{"$source", "SG"}}, 1, 0}}},
			"kk":              bson.M{"$sum": bson.M{"$cond": bson.A{bson.M{"$eq": bson.A{"$source", "KK"}}, 1, 0}}},
			"orderQuantity":   bson.M{"$sum": bson.M{"$ifNull": bson.A{"$order_quantity", bson.M{"$ifNull": bson.A{"$data.GAMNG", 0}}}}},
			"storageQuantity": bson.M{"$sum": bson.M{"$ifNull": bson.A{"$storage_quantity", bson.M{"$ifNull": bson.A{"$data.WMENG", 0}}}}},
			"dataStartDate":   bson.M{"$min": "$data.GSTRS"}, "dataEndDate": bson.M{"$max": "$data.GSTRS"}, "latestSyncedAt": bson.M{"$max": "$last_synced_at"},
		}}},
		{{Key: "$project", Value: bson.M{
			"_id": 0, "total": 1, "sg": 1, "kk": 1, "orderQuantity": 1, "machineQuantity": "$orderQuantity", "storageQuantity": 1, "dataStartDate": 1, "dataEndDate": 1,
			"salesOrders":    bson.M{"$size": bson.M{"$setDifference": bson.A{"$salesOrderValues", bson.A{""}}}},
			"latestSyncedAt": bson.M{"$dateToString": bson.M{"date": "$latestSyncedAt", "format": "%Y-%m-%dT%H:%M:%SZ", "timezone": "UTC"}},
		}}},
	}
}
