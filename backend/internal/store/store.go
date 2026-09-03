package store

import (
	"context"
	"regexp"
	"strings"
	"sync"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func New(ctx context.Context, uri, database, repairCollection, orderCollection string) (*Store, error) {
	client, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		return nil, err
	}
	if err = client.Ping(ctx, nil); err != nil {
		_ = client.Disconnect(context.Background())
		return nil, err
	}
	db := client.Database(database)
	// RTY joins orders to repairs by production order and date. Build the
	// supporting indexes asynchronously so startup remains fast while large
	// collections become queryable without full scans.
	go func() {
		indexCtx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		defer cancel()
		_, _ = db.Collection(repairCollection).Indexes().CreateMany(indexCtx, []mongo.IndexModel{
			{Keys: bson.D{{Key: "AUFNR", Value: 1}, {Key: "ZDATE_WX", Value: 1}}},
			{Keys: bson.D{{Key: "PCODE", Value: 1}}},
			{Keys: bson.D{{Key: "VBELN", Value: 1}}},
		})
		_, _ = db.Collection(orderCollection).Indexes().CreateMany(indexCtx, []mongo.IndexModel{
			{Keys: bson.D{{Key: "gstrs_date", Value: 1}, {Key: "data.IF_L6", Value: 1}}},
			{Keys: bson.D{{Key: "data.VBELN", Value: 1}}},
			{Keys: bson.D{{Key: "aufnr", Value: 1}}},
		})
		_, _ = db.Collection("order_bom_postings_sap").Indexes().CreateMany(indexCtx, []mongo.IndexModel{
			{Keys: bson.D{{Key: "BUDAT_MKPF", Value: 1}}},
			{Keys: bson.D{{Key: "AUFNR_1", Value: 1}, {Key: "BUDAT_MKPF", Value: 1}}},
			{Keys: bson.D{{Key: "VBELN_EX", Value: 1}, {Key: "BUDAT_MKPF", Value: 1}}},
		})
	}()
	views := make(map[string]*mongo.Collection, len(documentedViews))
	for id, config := range documentedViews {
		collection := db.Collection(config.collection)
		views[id] = collection
		keys := bson.D{}
		for _, field := range config.orderFields {
			keys = append(keys, bson.E{Key: field, Value: -1})
		}
		// Index creation can take minutes on a large remote collection. Keep it
		// out of the startup critical path; MongoDB will use it once complete.
		go func(collection *mongo.Collection, keys bson.D) {
			indexCtx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
			defer cancel()
			_, _ = collection.Indexes().CreateOne(indexCtx, mongo.IndexModel{Keys: keys, Options: options.Index().SetName(viewListIndexName)})
		}(collection, keys)
	}
	result := &Store{
		client: client, repairs: db.Collection(repairCollection), orders: db.Collection(orderCollection), views: views,
		bomCache: make(map[string]bomStreamCacheEntry), bomWarmDone: make(chan struct{}),
		stationCache: make(map[string]bomStreamCacheEntry), stationWarmDone: make(chan struct{}),
	}
	// Warm the broad LRR raw snapshot after startup so the first dashboard read
	// does not pay the full Mongo scan latency.
	go result.prewarmBOMStream()
	go result.prewarmStationStream()
	return result, nil
}

func (s *Store) Close(ctx context.Context) error { return s.client.Disconnect(ctx) }
func (s *Store) Ping(ctx context.Context) error  { return s.client.Ping(ctx, nil) }

func (s *Store) DataStatus(ctx context.Context) (DataStatus, error) {
	type source struct {
		collection *mongo.Collection
		field      string
	}
	sources := []source{
		{s.orders, "last_synced_at"}, {s.repairs, "_synced_at"}, {s.views["Z_V_ZMES_T_001"], "_synced_at"},
		{s.views["ZSGV_ZPP_SERNOLIST"], "_synced_at"}, {s.views["ZSGV_ZSD124"], "_synced_at"},
	}
	values := make([]string, len(sources))
	var wg sync.WaitGroup
	var once sync.Once
	var resultErr error
	for index, item := range sources {
		wg.Add(1)
		go func(index int, item source) {
			defer wg.Done()
			var doc bson.M
			err := item.collection.FindOne(ctx, bson.M{}, options.FindOne().SetSort(bson.D{{Key: item.field, Value: -1}}).SetProjection(bson.M{item.field: 1})).Decode(&doc)
			if err == mongo.ErrNoDocuments {
				return
			}
			if err != nil {
				once.Do(func() { resultErr = err })
				return
			}
			values[index] = timeText(doc[item.field])
		}(index, item)
	}
	wg.Wait()
	if resultErr != nil {
		return DataStatus{}, resultErr
	}
	return DataStatus{SalesOrdersLastSyncedAt: values[0], FaultsLastSyncedAt: values[1], StationRecordsLastSyncedAt: values[2], SerialBindingsLastSyncedAt: values[3], BOMPostingsLastSyncedAt: values[4]}, nil
}

func (s *Store) Stats(ctx context.Context, f Filters) (StatsResult, error) {
	cur, err := s.repairs.Aggregate(ctx, repairStatsPipeline(repairFilter(f), repairTimeField(f)))
	if err != nil {
		return StatsResult{}, err
	}
	defer cur.Close(ctx)
	var rows []bson.M
	if err = cur.All(ctx, &rows); err != nil {
		return StatsResult{}, err
	}
	if len(rows) == 0 {
		return StatsResult{}, nil
	}
	row := rows[0]
	return StatsResult{Total: int64(number(row["total"])), WithError: int64(number(row["withError"])), WithRepairPerson: int64(number(row["withRepairPerson"])), SalesOrders: int64(number(row["salesOrders"])), ProductionOrders: int64(number(row["productionOrders"])), HostBarcodes: int64(number(row["hostBarcodes"])), MissingSalesOrder: int64(number(row["missingSalesOrder"])), MissingProductionOrder: int64(number(row["missingProductionOrder"])), DataStartDate: firstText(row, "dataStartDate"), DataEndDate: firstText(row, "dataEndDate"), LatestSyncedAt: firstText(row, "latestSyncedAt")}, nil
}

func (s *Store) OrderStats(ctx context.Context, f OrderFilters) (OrderStatsResult, error) {
	cur, err := s.orders.Aggregate(ctx, orderStatsPipeline(orderFilter(f)))
	if err != nil {
		return OrderStatsResult{}, err
	}
	defer cur.Close(ctx)
	var rows []bson.M
	if err = cur.All(ctx, &rows); err != nil {
		return OrderStatsResult{}, err
	}
	if len(rows) == 0 {
		return OrderStatsResult{}, nil
	}
	row := rows[0]
	orderQuantity := number(row["orderQuantity"])
	machineQuantity := number(row["machineQuantity"])
	if machineQuantity == 0 {
		machineQuantity = orderQuantity
	}
	return OrderStatsResult{Total: int64(number(row["total"])), SalesOrders: int64(number(row["salesOrders"])), DataStartDate: firstText(row, "dataStartDate"), DataEndDate: firstText(row, "dataEndDate"), LatestSyncedAt: firstText(row, "latestSyncedAt"), SG: int64(number(row["sg"])), KK: int64(number(row["kk"])), OrderQuantity: orderQuantity, MachineQuantity: machineQuantity, StorageQuantity: number(row["storageQuantity"])}, nil
}

func viewFilter(viewID string, f ViewFilters, searchFields []string, dateField string) bson.M {
	conditions := bson.A{}
	if f.Keyword != "" {
		re := regexp.QuoteMeta(f.Keyword)
		or := bson.A{}
		for _, field := range searchFields {
			or = append(or, bson.M{field: bson.M{"$regex": re, "$options": "i"}})
		}
		conditions = append(conditions, bson.M{"$or": or})
	}
	if dateField != "" {
		from, to := f.From, f.To
		if f.DateFrom != "" {
			from = f.DateFrom
		}
		if f.DateTo != "" {
			to = f.DateTo
		}
		if dateField == "BUDAT_MKPF" {
			from, to = strings.ReplaceAll(from, "-", ""), strings.ReplaceAll(to, "-", "")
		} else if dateField == "ACTUAL_START_TIME" {
			// Datetime values are commonly stored as `YYYY-MM-DD HH:MM:SS`; include the full end day.
			to = inclusiveDateTimeEnd(to)
		}
		if from != "" {
			conditions = append(conditions, bson.M{dateField: bson.M{"$gte": from}})
		}
		if to != "" {
			conditions = append(conditions, bson.M{dateField: bson.M{"$lte": to}})
		}
	}
	addExact := func(field, value string, zeroCompat bool) {
		if strings.TrimSpace(value) != "" {
			conditions = append(conditions, exactBatch(field, value, zeroCompat))
		}
	}
	addExact("PCODE", f.SN, false)
	switch viewID {
	case "Z_V_ZMES_T_001":
		addExact("AUFNR", f.ProductionOrder, true)
		addExact("KDAUF", f.SalesOrder, true)
		if f.ProductModel != "" {
			conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"MAKTX_TH": f.ProductModel}, bson.M{"PRODH": f.ProductModel}, bson.M{"CPXH": f.ProductModel}}})
		}
	case "ZSGV_ZSD124":
		addExact("AUFNR_1", f.ProductionOrder, true)
		addExact("VBELN_EX", f.SalesOrder, true)
		addExact("MATNR", f.ProductModel, false)
		if f.MissingSalesOrder {
			conditions = append(conditions, bson.M{"$expr": emptyViewField("VBELN_EX")})
		}
	case "ZSGV_ZPP_SERNOLIST":
		if f.ProductionOrder != "" {
			conditions = append(conditions, bson.M{"$or": bson.A{exactBatch("AUFNR_HEAD", f.ProductionOrder, true), exactBatch("AUFNR_ITEM", f.ProductionOrder, true)}})
		}
		if f.ProductModel != "" {
			conditions = append(conditions, bson.M{"PRODH": f.ProductModel})
		}
	}
	addExact("MATNR", f.MaterialCode, false)
	if f.StationCode != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"PCODE": f.StationCode}, bson.M{"LINE_CODE": f.StationCode}, bson.M{"SPEC": f.StationCode}, bson.M{"OPERATION": f.StationCode}}})
	}
	if f.Base != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"LGORT": f.Base}, bson.M{"WERKS": f.Base}}})
	}
	if f.HeadOrder != "" {
		addExact("AUFNR_HEAD", f.HeadOrder, true)
	}
	if f.ItemOrder != "" {
		addExact("AUFNR_ITEM", f.ItemOrder, true)
	}
	if f.HeadSN != "" {
		addExact("ZCODE_HEAD", f.HeadSN, false)
	}
	if f.ItemSN != "" {
		addExact("ZCODE_ITEM", f.ItemSN, false)
	}
	if len(conditions) == 0 {
		return bson.M{}
	}
	return bson.M{"$and": conditions}
}

func inclusiveDateTimeEnd(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return value
	}
	if len(value) > 10 && strings.ContainsAny(value, " T") {
		return value
	}
	if len(value) == 10 {
		return value + " 23:59:59"
	}
	return value
}

func normalizeViewID(doc bson.M) {
	if value := firstText(doc, "_source_key", "_id"); value != "" {
		doc["id"] = value
	}
	if _, ok := doc["stationCode"]; !ok {
		if value := firstText(doc, "LINE_CODE", "SPEC", "OPERATION"); value != "" {
			doc["stationCode"] = value
		}
	}
}
