package store

import (
	"context"
	"strings"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func (s *Store) List(ctx context.Context, f Filters, page, pageSize int) (ListResult, error) {
	filter := repairFilter(f)
	total, err := s.repairs.CountDocuments(ctx, filter)
	if err != nil {
		return ListResult{}, err
	}
	sortFields := bson.D{{Key: "ZDATE_WX", Value: -1}, {Key: "ZTIME", Value: -1}}
	if repairTimeField(f) == "planned" {
		// Descending string order places populated GSTRS values before empty or missing ones.
		sortFields = bson.D{{Key: "GSTRS", Value: -1}, {Key: "ZDATE_WX", Value: -1}, {Key: "ZTIME", Value: -1}}
	}
	cur, err := s.repairs.Find(ctx, filter, options.Find().SetSort(sortFields).SetSkip(int64((page-1)*pageSize)).SetLimit(int64(pageSize)))
	if err != nil {
		return ListResult{}, err
	}
	defer cur.Close(ctx)
	docs := make([]bson.M, 0)
	if err = cur.All(ctx, &docs); err != nil {
		return ListResult{}, err
	}
	items := make([]Fault, 0, len(docs))
	for _, doc := range docs {
		items = append(items, normalizeFault(doc))
	}
	return ListResult{Items: items, Page: page, PageSize: pageSize, Total: total}, nil
}

// FaultSNs returns only the order/SN relationship needed by RTY joins.
// Keeping this projection at the gateway avoids transferring full repair rows.
func (s *Store) FaultSNs(ctx context.Context, f Filters) ([]FaultSN, error) {
	filter := faultLookupFilter(f)
	cur, err := s.repairs.Find(ctx, filter, options.Find().SetProjection(bson.M{"AUFNR": 1, "PCODE": 1}).SetLimit(MaxBulkQueryRows).SetBatchSize(10000))
	if err != nil {
		return nil, err
	}
	defer cur.Close(ctx)
	seen := make(map[string]struct{})
	result := make([]FaultSN, 0)
	for cur.Next(ctx) {
		var row struct {
			AUFNR string `bson:"AUFNR"`
			PCODE string `bson:"PCODE"`
		}
		if err := cur.Decode(&row); err != nil {
			return nil, err
		}
		if row.AUFNR == "" || row.PCODE == "" {
			continue
		}
		key := row.AUFNR + "\x00" + row.PCODE
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		result = append(result, FaultSN{ProductionOrder: row.AUFNR, SerialNumber: row.PCODE})
	}
	if err := cur.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

// FaultRowsBySNS returns the projected repair rows needed by station RTY.
// A POST body avoids the URL-size limit of the generic list endpoint.
func (s *Store) FaultRowsBySNS(ctx context.Context, sns []string, dateFrom, dateTo, station string) ([]bson.M, error) {
	values := make([]string, 0, len(sns))
	seen := make(map[string]struct{}, len(sns))
	for _, value := range sns {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		values = append(values, value)
	}
	if len(values) == 0 {
		return []bson.M{}, nil
	}
	filter := bson.M{"PCODE": bson.M{"$in": values}}
	dateBounds := bson.M{}
	if dateFrom != "" {
		dateBounds["$gte"] = strings.ReplaceAll(dateFrom, "-", "")
	}
	if dateTo != "" {
		dateBounds["$lte"] = strings.ReplaceAll(dateTo, "-", "")
	}
	if len(dateBounds) > 0 {
		filter["ZDATE_WX"] = dateBounds
	}
	if strings.TrimSpace(station) != "" {
		filter["ZNGGZ"] = strings.TrimSpace(station)
	}
	cur, err := s.repairs.Find(ctx, filter, options.Find().SetProjection(repairListProjection).SetLimit(MaxBulkQueryRows).SetBatchSize(10000))
	if err != nil {
		return nil, err
	}
	defer cur.Close(ctx)
	rows := make([]bson.M, 0)
	if err := cur.All(ctx, &rows); err != nil {
		return nil, err
	}
	return rows, nil
}

// faultLookupFilter is optimized for RTY's batch join. SAP production orders
// are commonly stored as 12-digit zero-padded strings while the order gateway
// returns the trimmed value, so the candidate set includes both forms.
func faultLookupFilter(f Filters) bson.M {
	conditions := make(bson.A, 0, 3)
	if f.ProductionOrders != "" {
		values := make([]string, 0)
		seen := make(map[string]struct{})
		for _, value := range splitValues(f.ProductionOrders) {
			trimmed := strings.TrimLeft(value, "0")
			if trimmed == "" {
				trimmed = "0"
			}
			padded := strings.Repeat("0", max(0, 12-len(trimmed))) + trimmed
			for _, candidate := range []string{value, trimmed, padded} {
				if _, ok := seen[candidate]; !ok {
					seen[candidate] = struct{}{}
					values = append(values, candidate)
				}
			}
		}
		if len(values) > 0 {
			conditions = append(conditions, bson.M{"AUFNR": bson.M{"$in": values}})
		}
	}
	if f.SalesOrders != "" {
		values := splitValues(f.SalesOrders)
		if len(values) > 0 {
			conditions = append(conditions, bson.M{"VBELN": bson.M{"$in": values}})
		}
	}
	if f.DateFrom != "" || f.DateTo != "" {
		bounds := bson.M{}
		if f.DateFrom != "" {
			bounds["$gte"] = strings.ReplaceAll(f.DateFrom, "-", "")
		}
		if f.DateTo != "" {
			bounds["$lte"] = strings.ReplaceAll(f.DateTo, "-", "")
		}
		conditions = append(conditions, bson.M{"ZDATE_WX": bounds})
	}
	if len(conditions) == 0 {
		return bson.M{"_id": bson.M{"$exists": false}}
	}
	return bson.M{"$and": conditions}
}

func (s *Store) FaultDetail(ctx context.Context, id string) (FaultDetail, error) {
	var doc bson.M
	err := s.repairs.FindOne(ctx, bson.M{"$or": bson.A{bson.M{"_source_key": id}, bson.M{"_id": id}}}).Decode(&doc)
	if err != nil {
		return FaultDetail{}, err
	}
	return FaultDetail{Fault: normalizeFault(doc), Fields: detailFields(doc), Raw: doc}, nil
}
