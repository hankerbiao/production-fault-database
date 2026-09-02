package store

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/bson/primitive"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

type Store struct {
	client  *mongo.Client
	repairs *mongo.Collection
	orders  *mongo.Collection
	views   map[string]*mongo.Collection
}

type Filters struct {
	Keyword, HostBarcode, DefectResponsibility, NGStation, SalesOrder, ProductionOrder string
	SNS, ProductionOrders, SalesOrders, DateFrom, DateTo, Station, ProductModel        string
}
type OrderFilters struct {
	Keyword, Source, GSTRSFrom, GSTRSTo                                                                   string
	SalesOrder, ProductionOrder, SerialNumber, ProductModel, Customer, Base, DateFrom, DateTo, OrderScope string
}

type Fault struct {
	ID                   string    `json:"id"`
	HostBarcode          string    `json:"hostBarcode"`
	SerialNumber         string    `json:"serialNumber"`
	ErrorCode            string    `json:"errorCode"`
	FaultDescription     string    `json:"faultDescription"`
	ErrorDescription     string    `json:"errorDescription"`
	ReviewProblem        string    `json:"reviewProblem"`
	DefectResponsibility string    `json:"defectResponsibility"`
	RepairAt             time.Time `json:"repairAt"`
	ReadBy               string    `json:"readBy"`
	RepairPerson         string    `json:"repairPerson"`
	SalesOrder           string    `json:"salesOrder"`
	ProductionOrder      string    `json:"productionOrder"`
	PlannedStartDate     string    `json:"plannedStartDate"`
	MaterialCode         string    `json:"materialCode"`
	MaterialDescription  string    `json:"materialDescription"`
	NGStation            string    `json:"ngStation"`
	RetestStation        string    `json:"retestStation"`
	RepairRemarks        string    `json:"repairRemarks"`
	Raw                  bson.M    `json:"raw,omitempty"`
}

type Field struct {
	Key   string `json:"key"`
	Label string `json:"label"`
	Value string `json:"value"`
}
type FaultDetail struct {
	Fault  Fault   `json:"fault"`
	Fields []Field `json:"fields"`
	Raw    bson.M  `json:"raw"`
}
type ListResult struct {
	Items    []Fault `json:"items"`
	Page     int     `json:"page"`
	PageSize int     `json:"pageSize"`
	Total    int64   `json:"total"`
}
type StatsResult struct {
	Total                  int64  `json:"total"`
	WithError              int64  `json:"withError"`
	WithRepairPerson       int64  `json:"withRepairPerson"`
	SalesOrders            int64  `json:"salesOrders"`
	ProductionOrders       int64  `json:"productionOrders"`
	MissingSalesOrder      int64  `json:"missingSalesOrder"`
	MissingProductionOrder int64  `json:"missingProductionOrder"`
	DataStartDate          string `json:"dataStartDate"`
	DataEndDate            string `json:"dataEndDate"`
	LatestSyncedAt         string `json:"latestSyncedAt"`
}

type Order struct {
	ID                  string  `json:"id"`
	Source              string  `json:"source"`
	AUFNR               string  `json:"aufnr"`
	SalesOrder          string  `json:"salesOrder"`
	CustomerID          string  `json:"customerId"`
	FinalUser           string  `json:"finalUser"`
	MaterialDescription string  `json:"materialDescription"`
	ProductionModel     string  `json:"productionModel"`
	InventoryLocation   string  `json:"inventoryLocation"`
	PlannedStartDate    string  `json:"plannedStartDate"`
	OrderQuantity       float64 `json:"orderQuantity"`
	StorageQuantity     float64 `json:"storageQuantity"`
	RecordCount         int     `json:"recordCount"`
	Raw                 bson.M  `json:"raw,omitempty"`
}
type OrderDetail struct {
	Order  Order   `json:"order"`
	Fields []Field `json:"fields"`
	Raw    bson.M  `json:"raw"`
}
type OrderListResult struct {
	Items    []Order `json:"items"`
	Page     int     `json:"page"`
	PageSize int     `json:"pageSize"`
	Total    int64   `json:"total"`
}

const MaxOrderQueryRows = 10000

type OrderStatsResult struct {
	Total           int64   `json:"total"`
	SalesOrders     int64   `json:"salesOrders"`
	DataStartDate   string  `json:"dataStartDate"`
	DataEndDate     string  `json:"dataEndDate"`
	LatestSyncedAt  string  `json:"latestSyncedAt"`
	SG              int64   `json:"sg"`
	KK              int64   `json:"kk"`
	OrderQuantity   float64 `json:"orderQuantity"`
	MachineQuantity float64 `json:"machineQuantity"`
	StorageQuantity float64 `json:"storageQuantity"`
}
type OrderModelsResult struct {
	Items []string `json:"items"`
}

type ViewFilters struct {
	Keyword, From, To                                                                  string
	DateFrom, DateTo, StationCode, SN, ProductionOrder, SalesOrder, Base, ProductModel string
	HeadOrder, ItemOrder, HeadSN, ItemSN, MaterialCode                                 string
}
type ViewListResult struct {
	Items    []bson.M `json:"items"`
	Page     int      `json:"page"`
	PageSize int      `json:"pageSize"`
	Total    int64    `json:"total"`
}
type ViewStatsResult struct {
	Total                  int64  `json:"total"`
	MissingSalesOrder      int64  `json:"missingSalesOrder"`
	MissingProductionOrder int64  `json:"missingProductionOrder"`
	DataStartDate          string `json:"dataStartDate"`
	DataEndDate            string `json:"dataEndDate"`
	LatestSyncedAt         string `json:"latestSyncedAt"`
}
type ViewDetailResult struct {
	Item   bson.M  `json:"item"`
	Fields []Field `json:"fields"`
}
type DataStatus struct {
	SalesOrdersLastSyncedAt    string `json:"salesOrdersLastSyncedAt"`
	FaultsLastSyncedAt         string `json:"faultsLastSyncedAt"`
	StationRecordsLastSyncedAt string `json:"stationRecordsLastSyncedAt"`
	SerialBindingsLastSyncedAt string `json:"serialBindingsLastSyncedAt"`
	BOMPostingsLastSyncedAt    string `json:"bomPostingsLastSyncedAt"`
}

var documentedViews = map[string]struct {
	collection   string
	dateField    string
	searchFields []string
	orderFields  []string
}{
	"ZSGV_ZSD124":        {"order_bom_postings_sap", "BUDAT_MKPF", []string{"MBLNR", "MJAHR", "ZEILE", "MATNR", "AUFNR_1", "VBELN_EX", "KUNNR", "NAME1"}, []string{"BUDAT_MKPF", "MBLNR", "MJAHR", "ZEILE"}},
	"ZSGV_ZPP_SERNOLIST": {"serial_bindings_sap", "", []string{"ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM", "PRODH"}, []string{"ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM"}},
	"Z_V_ZMES_T_001":     {"station_records_sap", "ACTUAL_START_TIME", []string{"HISTROYID", "PCODE", "OCODE", "AUFNR", "SPEC", "OPERATION", "GSTRS", "ACTUAL_START_TIME", "ACTUAL_END_TIME"}, []string{"ACTUAL_START_TIME", "HISTROYID", "SPEC_TIME"}},
}

const viewListIndexName = "api_view_list_order"

// Legacy projections are retained for callers that import these definitions; raw-data
// list endpoints intentionally read the complete source document.
var repairListProjection = bson.M{
	"_source_key": 1, "PCODE": 1, "ZMCOD1": 1, "ERROR_CODE": 1, "ZGZMS": 1, "ERROR_MSG": 1, "RPDESC": 1, "ZZRFL": 1,
	"ZWXDT": 1, "ZDATE_WX": 1, "ZDATE": 1, "ZTIME": 1, "ZUSER": 1, "U_FIX": 1, "VBELN": 1, "AUFNR": 1, "MATNR": 1, "MAKTX": 1, "ZNGGZ": 1, "RETEST_STATION": 1, "FIX_REMARKS": 1, "_synced_at": 1,
}

var orderListProjection = bson.M{
	"source": 1, "aufnr": 1, "record_count": 1, "order_quantity": 1, "storage_quantity": 1,
	"data.VBELN": 1, "data.KID": 1, "data.NAME1_ZU": 1, "data.MAKTX": 1, "data.MAKTX_TH": 1, "data.LGORT": 1, "data.GSTRS": 1, "data.GAMNG": 1, "data.WMENG": 1,
	"records.GAMNG": 1, "records.WMENG": 1,
}

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
	return &Store{client: client, repairs: db.Collection(repairCollection), orders: db.Collection(orderCollection), views: views}, nil
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
	cur, err := s.repairs.Aggregate(ctx, repairStatsPipeline(repairFilter(f)))
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
	return StatsResult{Total: int64(number(row["total"])), WithError: int64(number(row["withError"])), WithRepairPerson: int64(number(row["withRepairPerson"])), SalesOrders: int64(number(row["salesOrders"])), ProductionOrders: int64(number(row["productionOrders"])), MissingSalesOrder: int64(number(row["missingSalesOrder"])), MissingProductionOrder: int64(number(row["missingProductionOrder"])), DataStartDate: firstText(row, "dataStartDate"), DataEndDate: firstText(row, "dataEndDate"), LatestSyncedAt: firstText(row, "latestSyncedAt")}, nil
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

// OrderModels returns the distinct production-model values used by the sales-order board.
// The optional keyword keeps the endpoint useful when a deployment has many model values.
func (s *Store) OrderModels(ctx context.Context, keyword string) (OrderModelsResult, error) {
	filter := bson.M{"data.MAKTX_TH": bson.M{"$exists": true, "$nin": bson.A{"", nil}}}
	if keyword = strings.TrimSpace(keyword); keyword != "" {
		filter["data.MAKTX_TH"] = bson.M{"$regex": regexp.QuoteMeta(keyword), "$options": "i"}
	}
	values, err := s.orders.Distinct(ctx, "data.MAKTX_TH", filter)
	if err != nil {
		return OrderModelsResult{}, err
	}
	items := make([]string, 0, len(values))
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		text := strings.TrimSpace(fmt.Sprint(value))
		if text == "" || text == "<nil>" {
			continue
		}
		if _, ok := seen[text]; ok {
			continue
		}
		seen[text] = struct{}{}
		items = append(items, text)
	}
	sort.Strings(items)
	return OrderModelsResult{Items: items}, nil
}

func (s *Store) List(ctx context.Context, f Filters, page, pageSize int) (ListResult, error) {
	filter := repairFilter(f)
	total, err := s.repairs.CountDocuments(ctx, filter)
	if err != nil {
		return ListResult{}, err
	}
	cur, err := s.repairs.Find(ctx, filter, options.Find().SetSort(bson.D{{Key: "ZDATE_WX", Value: -1}, {Key: "ZTIME", Value: -1}}).SetSkip(int64((page-1)*pageSize)).SetLimit(int64(pageSize)))
	if err != nil {
		return ListResult{}, err
	}
	defer cur.Close(ctx)
	var docs []bson.M
	if err = cur.All(ctx, &docs); err != nil {
		return ListResult{}, err
	}
	items := make([]Fault, 0, len(docs))
	for _, doc := range docs {
		items = append(items, normalizeFault(doc))
	}
	return ListResult{Items: items, Page: page, PageSize: pageSize, Total: total}, nil
}

func (s *Store) FaultDetail(ctx context.Context, id string) (FaultDetail, error) {
	var doc bson.M
	err := s.repairs.FindOne(ctx, bson.M{"$or": bson.A{bson.M{"_source_key": id}, bson.M{"_id": id}}}).Decode(&doc)
	if err != nil {
		return FaultDetail{}, err
	}
	return FaultDetail{Fault: normalizeFault(doc), Fields: detailFields(doc), Raw: doc}, nil
}

// statsLegacy retains the original multi-query implementation for comparison while
// the public Stats method uses one aggregation over the filtered document set.
func (s *Store) statsLegacy(ctx context.Context, f Filters) (StatsResult, error) {
	filter := repairFilter(f)
	count := func(extra bson.M) (int64, error) { return s.repairs.CountDocuments(ctx, withFilter(filter, extra)) }
	total, err := count(nil)
	if err != nil {
		return StatsResult{}, err
	}
	withError, err := count(bson.M{"$or": bson.A{bson.M{"ERROR_CODE": bson.M{"$exists": true, "$ne": ""}}, bson.M{"ERROR_MSG": bson.M{"$exists": true, "$ne": ""}}}})
	if err != nil {
		return StatsResult{}, err
	}
	withRepairPerson, err := count(bson.M{"U_FIX": bson.M{"$exists": true, "$nin": bson.A{"", nil}}})
	if err != nil {
		return StatsResult{}, err
	}
	missingSalesOrder, err := count(missingField("VBELN"))
	if err != nil {
		return StatsResult{}, err
	}
	missingProductionOrder, err := count(missingField("AUFNR"))
	if err != nil {
		return StatsResult{}, err
	}
	salesOrders, err := s.distinctCount(ctx, s.repairs, filter, "VBELN")
	if err != nil {
		return StatsResult{}, err
	}
	productionOrders, err := s.distinctCount(ctx, s.repairs, filter, "AUFNR")
	if err != nil {
		return StatsResult{}, err
	}
	timelineProject := bson.M{
		"_id":            0,
		"dataStartDate":  1,
		"dataEndDate":    1,
		"latestSyncedAt": bson.M{"$dateToString": bson.M{"date": "$latestSyncedAt", "format": "%Y-%m-%dT%H:%M:%SZ", "timezone": "UTC"}},
	}
	timelineCursor, err := s.repairs.Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$group", Value: bson.M{
			"_id":            nil,
			"dataStartDate":  bson.M{"$min": "$ZDATE_WX"},
			"dataEndDate":    bson.M{"$max": "$ZDATE_WX"},
			"latestSyncedAt": bson.M{"$max": "$_synced_at"},
		}}},
		{{Key: "$project", Value: timelineProject}},
	})
	if err != nil {
		return StatsResult{}, err
	}
	defer timelineCursor.Close(ctx)
	var timelineRows []bson.M
	if err = timelineCursor.All(ctx, &timelineRows); err != nil {
		return StatsResult{}, err
	}
	dataStartDate, dataEndDate, latestSyncedAt := "", "", ""
	if len(timelineRows) > 0 {
		dataStartDate = firstText(timelineRows[0], "dataStartDate")
		dataEndDate = firstText(timelineRows[0], "dataEndDate")
		latestSyncedAt = firstText(timelineRows[0], "latestSyncedAt")
	}
	return StatsResult{Total: total, WithError: withError, WithRepairPerson: withRepairPerson, SalesOrders: salesOrders, ProductionOrders: productionOrders, MissingSalesOrder: missingSalesOrder, MissingProductionOrder: missingProductionOrder, DataStartDate: dataStartDate, DataEndDate: dataEndDate, LatestSyncedAt: latestSyncedAt}, nil
}

func (s *Store) distinctCount(ctx context.Context, collection *mongo.Collection, filter bson.M, field string) (int64, error) {
	values, err := collection.Distinct(ctx, field, withFilter(filter, bson.M{field: bson.M{"$exists": true, "$nin": bson.A{"", nil}}}))
	if err != nil {
		return 0, err
	}
	return int64(len(values)), nil
}

func (s *Store) Orders(ctx context.Context, f OrderFilters, page, pageSize int) (OrderListResult, error) {
	filter := orderFilter(f)
	total, err := s.orders.CountDocuments(ctx, filter)
	if err != nil {
		return OrderListResult{}, err
	}
	cur, err := s.orders.Find(ctx, filter, options.Find().SetSort(bson.D{{Key: "data.GSTRS", Value: -1}, {Key: "last_synced_at", Value: -1}}).SetSkip(int64((page-1)*pageSize)).SetLimit(int64(pageSize)))
	if err != nil {
		return OrderListResult{}, err
	}
	defer cur.Close(ctx)
	var docs []bson.M
	if err = cur.All(ctx, &docs); err != nil {
		return OrderListResult{}, err
	}
	items := make([]Order, 0, len(docs))
	for _, doc := range docs {
		items = append(items, normalizeOrder(doc))
	}
	return OrderListResult{Items: items, Page: page, PageSize: pageSize, Total: total}, nil
}

// OrdersAll returns every filtered order up to the API's bounded bulk-query limit.
// It intentionally reuses the same filter and ordering as the paginated board.
func (s *Store) OrdersAll(ctx context.Context, f OrderFilters) (OrderListResult, error) {
	return s.Orders(ctx, f, 1, MaxOrderQueryRows)
}

func (s *Store) OrderDetail(ctx context.Context, id string) (OrderDetail, error) {
	var doc bson.M
	if err := s.orders.FindOne(ctx, bson.M{"_id": id}).Decode(&doc); err != nil {
		return OrderDetail{}, err
	}
	return OrderDetail{Order: normalizeOrder(doc), Fields: orderDetailFields(doc), Raw: doc}, nil
}

// orderStatsLegacy retains the original multi-query implementation for comparison.
func (s *Store) orderStatsLegacy(ctx context.Context, f OrderFilters) (OrderStatsResult, error) {
	filter := orderFilter(f)
	total, err := s.orders.CountDocuments(ctx, filter)
	if err != nil {
		return OrderStatsResult{}, err
	}
	normalizedVBELN := bson.M{
		"$trim": bson.M{
			"input": bson.M{
				"$convert": bson.M{"input": "$data.VBELN", "to": "string", "onError": "", "onNull": ""},
			},
		},
	}
	salesOrderCursor, err := s.orders.Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$project", Value: bson.M{"vbeln": normalizedVBELN}}},
		{{Key: "$match", Value: bson.M{"vbeln": bson.M{"$ne": ""}}}},
		{{Key: "$group", Value: bson.M{"_id": "$vbeln"}}},
		{{Key: "$count", Value: "salesOrders"}},
	})
	if err != nil {
		return OrderStatsResult{}, err
	}
	defer salesOrderCursor.Close(ctx)
	var salesOrderRows []bson.M
	if err = salesOrderCursor.All(ctx, &salesOrderRows); err != nil {
		return OrderStatsResult{}, err
	}
	salesOrders := int64(0)
	if len(salesOrderRows) > 0 {
		salesOrders = int64(number(salesOrderRows[0]["salesOrders"]))
	}
	timelineProject := bson.M{
		"_id":            0,
		"dataStartDate":  1,
		"dataEndDate":    1,
		"latestSyncedAt": bson.M{"$dateToString": bson.M{"date": "$latestSyncedAt", "format": "%Y-%m-%dT%H:%M:%SZ", "timezone": "UTC"}},
	}
	timelineCursor, err := s.orders.Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$group", Value: bson.M{
			"_id":            nil,
			"dataStartDate":  bson.M{"$min": "$data.GSTRS"},
			"dataEndDate":    bson.M{"$max": "$data.GSTRS"},
			"latestSyncedAt": bson.M{"$max": "$last_synced_at"},
		}}},
		{{Key: "$project", Value: timelineProject}},
	})
	if err != nil {
		return OrderStatsResult{}, err
	}
	defer timelineCursor.Close(ctx)
	var timelineRows []bson.M
	if err = timelineCursor.All(ctx, &timelineRows); err != nil {
		return OrderStatsResult{}, err
	}
	dataStartDate, dataEndDate, latestSyncedAt := "", "", ""
	if len(timelineRows) > 0 {
		dataStartDate = firstText(timelineRows[0], "dataStartDate")
		dataEndDate = firstText(timelineRows[0], "dataEndDate")
		latestSyncedAt = firstText(timelineRows[0], "latestSyncedAt")
	}
	sg, err := s.orders.CountDocuments(ctx, withFilter(filter, bson.M{"source": "SG"}))
	if err != nil {
		return OrderStatsResult{}, err
	}
	kk, err := s.orders.CountDocuments(ctx, withFilter(filter, bson.M{"source": "KK"}))
	if err != nil {
		return OrderStatsResult{}, err
	}
	cur, err := s.orders.Aggregate(ctx, mongo.Pipeline{{{Key: "$match", Value: filter}}, {{Key: "$group", Value: bson.M{
		"_id":             nil,
		"orderQuantity":   bson.M{"$sum": bson.M{"$ifNull": bson.A{"$order_quantity", bson.M{"$ifNull": bson.A{"$data.GAMNG", 0}}}}},
		"storageQuantity": bson.M{"$sum": bson.M{"$ifNull": bson.A{"$storage_quantity", bson.M{"$ifNull": bson.A{"$data.WMENG", 0}}}}},
	}}}})
	if err != nil {
		return OrderStatsResult{}, err
	}
	defer cur.Close(ctx)
	var rows []bson.M
	if err = cur.All(ctx, &rows); err != nil {
		return OrderStatsResult{}, err
	}
	orderQuantity, storageQuantity := 0.0, 0.0
	if len(rows) > 0 {
		orderQuantity = number(rows[0]["orderQuantity"])
		storageQuantity = number(rows[0]["storageQuantity"])
	}
	return OrderStatsResult{Total: total, SalesOrders: salesOrders, DataStartDate: dataStartDate, DataEndDate: dataEndDate, LatestSyncedAt: latestSyncedAt, SG: sg, KK: kk, OrderQuantity: orderQuantity, MachineQuantity: orderQuantity, StorageQuantity: storageQuantity}, nil
}

func repairStatsPipeline(filter bson.M) mongo.Pipeline {
	nonEmpty := func(field string) bson.M {
		return bson.M{"$ne": bson.A{bson.M{"$ifNull": bson.A{"$" + field, ""}}, ""}}
	}
	missing := func(field string) bson.M {
		return bson.M{"$eq": bson.A{bson.M{"$ifNull": bson.A{"$" + field, ""}}, ""}}
	}
	return mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$group", Value: bson.M{
			"_id": nil, "total": bson.M{"$sum": 1},
			"withError":              bson.M{"$sum": bson.M{"$cond": bson.A{bson.M{"$or": bson.A{nonEmpty("ERROR_CODE"), nonEmpty("ERROR_MSG")}}, 1, 0}}},
			"withRepairPerson":       bson.M{"$sum": bson.M{"$cond": bson.A{nonEmpty("U_FIX"), 1, 0}}},
			"missingSalesOrder":      bson.M{"$sum": bson.M{"$cond": bson.A{missing("VBELN"), 1, 0}}},
			"missingProductionOrder": bson.M{"$sum": bson.M{"$cond": bson.A{missing("AUFNR"), 1, 0}}},
			"salesOrderValues":       bson.M{"$addToSet": "$VBELN"}, "productionOrderValues": bson.M{"$addToSet": "$AUFNR"},
			"dataStartDate": bson.M{"$min": "$ZDATE_WX"}, "dataEndDate": bson.M{"$max": "$ZDATE_WX"}, "latestSyncedAt": bson.M{"$max": "$_synced_at"},
		}}},
		{{Key: "$project", Value: bson.M{
			"_id": 0, "total": 1, "withError": 1, "withRepairPerson": 1, "missingSalesOrder": 1, "missingProductionOrder": 1, "dataStartDate": 1, "dataEndDate": 1,
			"salesOrders":      bson.M{"$size": bson.M{"$setDifference": bson.A{"$salesOrderValues", bson.A{"", nil}}}},
			"productionOrders": bson.M{"$size": bson.M{"$setDifference": bson.A{"$productionOrderValues", bson.A{"", nil}}}},
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

func (s *Store) ViewList(ctx context.Context, viewID string, f ViewFilters, page, pageSize int) (ViewListResult, error) {
	config, ok := documentedViews[viewID]
	if !ok {
		return ViewListResult{}, fmt.Errorf("unknown view: %s", viewID)
	}
	filter := viewFilter(viewID, f, config.searchFields, config.dateField)
	sortFields := bson.D{}
	for _, field := range config.orderFields {
		sortFields = append(sortFields, bson.E{Key: field, Value: -1})
	}

	// Count and page retrieval are independent MongoDB operations. Running
	// them together removes one full network/database round-trip from the
	// request latency, which matters for the unfiltered large station view.
	var total int64
	var docs []bson.M
	var countErr, findErr error
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		total, countErr = s.views[viewID].CountDocuments(ctx, filter)
	}()
	go func() {
		defer wg.Done()
		findOptions := options.Find().SetSort(sortFields).SetSkip(int64((page - 1) * pageSize)).SetLimit(int64(pageSize))
		cur, err := s.views[viewID].Find(ctx, filter, findOptions)
		if err != nil {
			findErr = err
			return
		}
		defer cur.Close(ctx)
		findErr = cur.All(ctx, &docs)
	}()
	wg.Wait()
	if countErr != nil {
		return ViewListResult{}, countErr
	}
	if findErr != nil {
		return ViewListResult{}, findErr
	}
	for _, doc := range docs {
		normalizeViewID(doc)
	}
	return ViewListResult{Items: docs, Page: page, PageSize: pageSize, Total: total}, nil
}

func (s *Store) ViewDetail(ctx context.Context, viewID, id string) (ViewDetailResult, error) {
	if _, ok := documentedViews[viewID]; !ok {
		return ViewDetailResult{}, fmt.Errorf("unknown view: %s", viewID)
	}
	var doc bson.M
	err := s.views[viewID].FindOne(ctx, bson.M{"$or": bson.A{bson.M{"_source_key": id}, bson.M{"_id": id}}}).Decode(&doc)
	if err != nil {
		return ViewDetailResult{}, err
	}
	normalizeViewID(doc)
	return ViewDetailResult{Item: doc, Fields: viewDetailFields(viewID, doc)}, nil
}

func (s *Store) ViewStats(ctx context.Context, viewID string, f ViewFilters) (ViewStatsResult, error) {
	config, ok := documentedViews[viewID]
	if !ok {
		return ViewStatsResult{}, fmt.Errorf("unknown view: %s", viewID)
	}
	filter := viewFilter(viewID, f, config.searchFields, config.dateField)
	total, err := s.views[viewID].CountDocuments(ctx, filter)
	if err != nil {
		return ViewStatsResult{}, err
	}
	result := ViewStatsResult{Total: total}
	if config.dateField == "" {
		return result, nil
	}
	cur, err := s.views[viewID].Aggregate(ctx, viewStatsPipeline(viewID, filter, config.dateField))
	if err != nil {
		return ViewStatsResult{}, err
	}
	defer cur.Close(ctx)
	var rows []bson.M
	if err := cur.All(ctx, &rows); err != nil {
		return ViewStatsResult{}, err
	}
	if len(rows) > 0 {
		result.DataStartDate, result.DataEndDate = firstText(rows[0], "dataStartDate"), firstText(rows[0], "dataEndDate")
		result.MissingSalesOrder = int64(number(rows[0]["missingSalesOrder"]))
		result.MissingProductionOrder = int64(number(rows[0]["missingProductionOrder"]))
		if value, ok := rows[0]["latestSyncedAt"]; ok && value != nil {
			result.LatestSyncedAt = fmt.Sprint(value)
		}
	}
	return result, nil
}

func viewStatsPipeline(viewID string, filter bson.M, dateField string) mongo.Pipeline {
	group := bson.M{
		"_id":            nil,
		"dataStartDate":  bson.M{"$min": "$" + dateField},
		"dataEndDate":    bson.M{"$max": "$" + dateField},
		"latestSyncedAt": bson.M{"$max": "$_synced_at"},
	}
	project := bson.M{
		"_id":           0,
		"dataStartDate": 1,
		"dataEndDate":   1,
		"latestSyncedAt": bson.M{"$dateToString": bson.M{
			"date": "$latestSyncedAt", "format": "%Y-%m-%dT%H:%M:%SZ", "timezone": "UTC",
		}},
	}
	if viewID == "Z_V_ZMES_T_001" {
		group["missingSalesOrder"] = bson.M{"$sum": bson.M{"$cond": bson.A{emptyViewField("KDAUF"), 1, 0}}}
		group["missingProductionOrder"] = bson.M{"$sum": bson.M{"$cond": bson.A{emptyViewField("AUFNR"), 1, 0}}}
		project["missingSalesOrder"] = 1
		project["missingProductionOrder"] = 1
	}
	return mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$group", Value: group}},
		{{Key: "$project", Value: project}},
	}
}

func emptyViewField(field string) bson.M {
	return bson.M{"$eq": bson.A{
		bson.M{"$trim": bson.M{"input": bson.M{"$convert": bson.M{
			"input": "$" + field, "to": "string", "onError": "", "onNull": "",
		}}}},
		"",
	}}
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

var stationFieldOrder = []string{
	"MANDT", "HISTROYID", "PCODE", "OCODE", "AUFNR", "KDAUF", "KDPOS", "LGORT", "MAKTX_TH",
	"LEAD_CYCLE", "AUFNR_CYCLE", "CUSTOMIZE_CYCLE", "OEMBZ", "SPEC", "OPERATION", "SPEC_DESC",
	"UNAME", "LASTSPEC_TIME", "SPEC_TIME", "GSTRS", "PLAN_END_TIME", "ACTUAL_START_TIME", "ACTUAL_END_TIME",
	"HADE1", "HADE2", "HADE3", "HADE4", "HADE5", "HADE6", "HADE7", "HADE8", "STATU", "MESS",
	"CLASSCODE", "EQUIPMENTNUMBER", "ERR_FLAG", "LINE_CODE", "NEXT_SECTION", "PASSCOUNT", "TEST_ID", "PRODH",
}

var stationFieldLabels = map[string]string{
	"MANDT": "集团", "HISTROYID": "主键", "PCODE": "主机序列号", "OCODE": "客户序列号", "AUFNR": "生产订单",
	"KDAUF": "销售订单", "KDPOS": "行项目", "LGORT": "基地", "MAKTX_TH": "生产机型", "LEAD_CYCLE": "生产周期（小时）",
	"AUFNR_CYCLE": "订单类型周期（小时）", "CUSTOMIZE_CYCLE": "定制化周期（小时）", "OEMBZ": "是否定制化", "SPEC": "操作工序",
	"OPERATION": "操作说明", "SPEC_DESC": "工序描述", "UNAME": "操作员", "LASTSPEC_TIME": "上一工序时间", "SPEC_TIME": "操作时间",
	"GSTRS": "计划开始日期", "PLAN_END_TIME": "计划结束时间", "ACTUAL_START_TIME": "实际开始时间", "ACTUAL_END_TIME": "实际结束时间",
	"HADE1": "预留字段1", "HADE2": "预留字段2", "HADE3": "预留字段3", "HADE4": "预留字段4", "HADE5": "预留字段5",
	"HADE6": "预留字段6", "HADE7": "预留字段7", "HADE8": "预留字段8", "STATU": "返回状态", "MESS": "返回值",
	"CLASSCODE": "班次编码", "EQUIPMENTNUMBER": "设备编号", "ERR_FLAG": "检验结果", "LINE_CODE": "生产线", "NEXT_SECTION": "下一工序",
	"PASSCOUNT": "通过次数", "TEST_ID": "检验ID", "PRODH": "产品层次",
}

var serialBindingFieldOrder = []string{"ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM", "PRODH"}
var serialBindingFieldLabels = map[string]string{
	"ZCODE_HEAD": "大刀/机头序列号（ZCODE_HEAD）",
	"ZCODE_ITEM": "小刀/BOX序列号（ZCODE_ITEM）",
	"AUFNR_HEAD": "大刀/机头生产订单号（AUFNR_HEAD）",
	"AUFNR_ITEM": "小刀/BOX生产订单号（AUFNR_ITEM）",
	"PRODH":      "产品层次（PRODH）",
}

func viewDetailFields(viewID string, doc bson.M) []Field {
	keys := make([]string, 0, len(doc))
	seen := make(map[string]bool, len(doc))
	if viewID == "Z_V_ZMES_T_001" {
		keys = append(keys, stationFieldOrder...)
		for _, key := range keys {
			seen[key] = true
		}
	} else if viewID == "ZSGV_ZPP_SERNOLIST" {
		keys = append(keys, serialBindingFieldOrder...)
		for _, key := range keys {
			seen[key] = true
		}
	}
	remaining := make([]string, 0, len(doc))
	for key := range doc {
		if key != "id" && key != "_id" && !seen[key] {
			remaining = append(remaining, key)
		}
	}
	sort.Strings(remaining)
	keys = append(keys, remaining...)
	result := make([]Field, 0, len(keys))
	for _, key := range keys {
		value := doc[key]
		if value != nil {
			label := "字段 / Field（" + key + "）"
			if viewID == "Z_V_ZMES_T_001" {
				if translated, ok := stationFieldLabels[key]; ok {
					label = translated
				}
			} else if viewID == "ZSGV_ZPP_SERNOLIST" {
				if translated, ok := serialBindingFieldLabels[key]; ok {
					label = translated
				}
			}
			result = append(result, Field{Key: key, Label: label, Value: fmt.Sprint(value)})
		}
	}
	return result
}

func repairFilter(f Filters) bson.M {
	conditions := make(bson.A, 0, 12)
	if f.HostBarcode != "" {
		conditions = append(conditions, exactBatch("PCODE", f.HostBarcode, false))
	}
	if f.SalesOrder != "" {
		conditions = append(conditions, exactBatch("VBELN", f.SalesOrder, true))
	}
	if f.ProductionOrder != "" {
		conditions = append(conditions, exactBatch("AUFNR", f.ProductionOrder, true))
	}
	if f.NGStation != "" {
		conditions = append(conditions, bson.M{"ZNGGZ": f.NGStation})
	}
	if f.SNS != "" {
		conditions = append(conditions, exactBatch("ZMCOD1", f.SNS, false))
	}
	if f.ProductionOrders != "" {
		conditions = append(conditions, exactBatch("AUFNR", f.ProductionOrders, true))
	}
	if f.SalesOrders != "" {
		conditions = append(conditions, exactBatch("VBELN", f.SalesOrders, true))
	}
	if f.DateFrom != "" {
		conditions = append(conditions, bson.M{"ZDATE_WX": bson.M{"$gte": strings.ReplaceAll(f.DateFrom, "-", "")}})
	}
	if f.DateTo != "" {
		conditions = append(conditions, bson.M{"ZDATE_WX": bson.M{"$lte": strings.ReplaceAll(f.DateTo, "-", "")}})
	}
	if f.Station != "" {
		conditions = append(conditions, bson.M{"ZNGGZ": f.Station})
	}
	if f.ProductModel != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"ZJXMC": f.ProductModel}, bson.M{"MAKTX": f.ProductModel}, bson.M{"MATNR": f.ProductModel}}})
	}
	if f.DefectResponsibility != "" {
		conditions = append(conditions, bson.M{"ZZRFL": f.DefectResponsibility})
	}
	if f.Keyword != "" {
		re := regexp.QuoteMeta(f.Keyword)
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"ERROR_CODE": bson.M{"$regex": re, "$options": "i"}}, bson.M{"ERROR_MSG": bson.M{"$regex": re, "$options": "i"}}, bson.M{"ZGZMS": bson.M{"$regex": re, "$options": "i"}}, bson.M{"RPDESC": bson.M{"$regex": re, "$options": "i"}}, bson.M{"ZMCOD1": bson.M{"$regex": re, "$options": "i"}}, bson.M{"PCODE": bson.M{"$regex": re, "$options": "i"}}, bson.M{"VBELN": bson.M{"$regex": re, "$options": "i"}}, bson.M{"AUFNR": bson.M{"$regex": re, "$options": "i"}}}})
	}
	if len(conditions) == 0 {
		return bson.M{}
	}
	return bson.M{"$and": conditions}
}

func orderFilter(f OrderFilters) bson.M {
	conditions := make(bson.A, 0, 12)
	if f.Source != "" {
		conditions = append(conditions, bson.M{"source": f.Source})
	}
	if f.OrderScope != "" {
		conditions = append(conditions, bson.M{"source": f.OrderScope})
	}
	if f.SalesOrder != "" {
		conditions = append(conditions, exactBatch("data.VBELN", f.SalesOrder, true))
	}
	if f.ProductionOrder != "" {
		conditions = append(conditions, exactBatch("aufnr", f.ProductionOrder, true))
	}
	if f.SerialNumber != "" {
		re := regexp.QuoteMeta(strings.TrimSpace(f.SerialNumber))
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"data.ZCODE": bson.M{"$regex": re, "$options": "i"}}, bson.M{"data.ZCODE_HEAD": bson.M{"$regex": re, "$options": "i"}}, bson.M{"data.ZCODE_ITEM": bson.M{"$regex": re, "$options": "i"}}, bson.M{"records.ZCODE": bson.M{"$regex": re, "$options": "i"}}}})
	}
	if f.ProductModel != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"data.MAKTX_TH": f.ProductModel}, bson.M{"data.CPXH": f.ProductModel}, bson.M{"data.MAKTX": f.ProductModel}}})
	}
	if f.Customer != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"data.KID": f.Customer}, bson.M{"data.NAME1_ZU": f.Customer}}})
	}
	if f.Base != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"data.LGORT": f.Base}, bson.M{"data.WERKS": f.Base}}})
	}
	dateFrom, dateTo := f.DateFrom, f.DateTo
	if dateFrom == "" {
		dateFrom = f.GSTRSFrom
	}
	if dateTo == "" {
		dateTo = f.GSTRSTo
	}
	if dateFrom != "" || dateTo != "" {
		conditions = append(conditions, orderDateRangeFilter(dateFrom, dateTo))
	}
	if f.Keyword != "" {
		re := regexp.QuoteMeta(f.Keyword)
		conditions = append(conditions, bson.M{"$or": bson.A{bson.M{"aufnr": bson.M{"$regex": re, "$options": "i"}}, bson.M{"data.VBELN": bson.M{"$regex": re, "$options": "i"}}, bson.M{"data.NAME1_ZU": bson.M{"$regex": re, "$options": "i"}}, bson.M{"data.MAKTX_TH": bson.M{"$regex": re, "$options": "i"}}, bson.M{"data.KID": bson.M{"$regex": re, "$options": "i"}}, bson.M{"data.GSTRS": bson.M{"$regex": re, "$options": "i"}}}})
	}
	if len(conditions) == 0 {
		return bson.M{}
	}
	return bson.M{"$and": conditions}
}

// orderDateRangeFilter supports both the current ISO `GSTRS` values and
// historical SAP values without separators. gstrs_date is the normalized field
// written by the sales-order synchronizer and is the preferred query branch.
func orderDateRangeFilter(dateFrom, dateTo string) bson.M {
	isoFrom, isoTo := isoDate(dateFrom), isoDate(dateTo)
	compactFrom, compactTo := strings.ReplaceAll(isoFrom, "-", ""), strings.ReplaceAll(isoTo, "-", "")
	bounds := func(from, to string) bson.M {
		result := bson.M{}
		if from != "" {
			result["$gte"] = from
		}
		if to != "" {
			result["$lte"] = to
		}
		return result
	}
	return bson.M{"$or": bson.A{
		bson.M{"gstrs_date": bounds(isoFrom, isoTo)},
		bson.M{"data.GSTRS": bounds(isoFrom, isoTo)},
		bson.M{"data.GSTRS": bounds(compactFrom, compactTo)},
	}}
}

func isoDate(value string) string {
	value = strings.TrimSpace(value)
	if len(value) == 8 && strings.IndexFunc(value, func(r rune) bool { return r < '0' || r > '9' }) == -1 {
		return value[:4] + "-" + value[4:6] + "-" + value[6:]
	}
	return value
}

// exactBatch accepts comma-separated values and matches SAP numbers with or without leading zeroes.
func exactBatch(field, input string, leadingZeroCompatible bool) bson.M {
	values := splitValues(input)
	if len(values) == 0 {
		return bson.M{}
	}
	patterns := bson.A{}
	for _, value := range values {
		if leadingZeroCompatible {
			trimmed := strings.TrimLeft(value, "0")
			if trimmed == "" {
				trimmed = "0"
			}
			patterns = append(patterns, "^0*"+regexp.QuoteMeta(trimmed)+"$")
		} else {
			patterns = append(patterns, "^"+regexp.QuoteMeta(value)+"$")
		}
	}
	if len(patterns) == 1 {
		return bson.M{field: bson.M{"$regex": patterns[0], "$options": "i"}}
	}
	or := bson.A{}
	for _, pattern := range patterns {
		or = append(or, bson.M{field: bson.M{"$regex": pattern, "$options": "i"}})
	}
	return bson.M{"$or": or}
}

func splitValues(input string) []string {
	parts := strings.FieldsFunc(input, func(r rune) bool { return r == ',' || r == '，' })
	values := make([]string, 0, len(parts))
	for _, part := range parts {
		if value := strings.TrimSpace(part); value != "" {
			values = append(values, value)
		}
	}
	return values
}

func withFilter(base, extra bson.M) bson.M {
	if len(extra) == 0 {
		return base
	}
	if len(base) == 0 {
		return extra
	}
	return bson.M{"$and": bson.A{base, extra}}
}

func missingField(name string) bson.M {
	return bson.M{"$or": bson.A{bson.M{name: bson.M{"$exists": false}}, bson.M{name: nil}, bson.M{name: ""}}}
}

func normalizeFault(doc bson.M) Fault {
	return Fault{
		ID:                   firstText(doc, "_source_key", "_id"),
		HostBarcode:          firstText(doc, "PCODE"),
		SerialNumber:         firstText(doc, "ZMCOD1"),
		ErrorCode:            firstText(doc, "ERROR_CODE"),
		FaultDescription:     firstText(doc, "ZGZMS"),
		ErrorDescription:     firstText(doc, "ERROR_MSG"),
		ReviewProblem:        firstText(doc, "RPDESC"),
		DefectResponsibility: firstText(doc, "ZZRFL"),
		RepairAt:             repairTime(doc),
		ReadBy:               firstText(doc, "ZUSER"),
		RepairPerson:         firstText(doc, "U_FIX"),
		SalesOrder:           firstText(doc, "VBELN"),
		ProductionOrder:      firstText(doc, "AUFNR"),
		PlannedStartDate:     firstText(doc, "GSTRS"),
		MaterialCode:         firstText(doc, "MATNR"),
		MaterialDescription:  firstText(doc, "MAKTX"),
		NGStation:            firstText(doc, "ZNGGZ"),
		RetestStation:        firstText(doc, "RETEST_STATION"),
		RepairRemarks:        firstText(doc, "FIX_REMARKS"),
		Raw:                  cloneDocument(doc),
	}
}

func normalizeOrder(doc bson.M) Order {
	data, _ := doc["data"].(bson.M)
	return Order{
		ID:                  firstText(doc, "_id"),
		Source:              firstText(doc, "source"),
		AUFNR:               firstText(doc, "aufnr"),
		SalesOrder:          firstText(data, "VBELN"),
		CustomerID:          firstText(data, "KID"),
		FinalUser:           firstText(data, "NAME1_ZU"),
		MaterialDescription: firstText(data, "MAKTX"),
		ProductionModel:     firstText(data, "MAKTX_TH"),
		InventoryLocation:   firstText(data, "LGORT"),
		PlannedStartDate:    firstText(data, "GSTRS"),
		OrderQuantity:       documentQuantity(doc, "order_quantity", "GAMNG"),
		StorageQuantity:     documentQuantity(doc, "storage_quantity", "WMENG"),
		RecordCount:         int(number(doc["record_count"])),
		Raw:                 cloneDocument(doc),
	}
}

func cloneDocument(doc bson.M) bson.M {
	copy := make(bson.M, len(doc))
	for key, value := range doc {
		copy[key] = value
	}
	return copy
}

func detailFields(doc bson.M) []Field {
	priority := []string{"PCODE", "ZMCOD1", "ERROR_CODE", "ZGZMS", "ERROR_MSG", "ZZRFL", "MATNR", "MAKTX", "ZNGGZ", "RETEST_STATION", "VBELN", "AUFNR", "GSTRS", "POSNR", "ZWXDT", "ZDATE_WX", "ZTIME", "ZUSER", "U_FIX", "FIX_REMARKS", "RPDESC", "_synced_at"}
	seen := map[string]bool{}
	fields := make([]Field, 0, len(doc))
	appendField := func(key string) {
		if seen[key] {
			return
		}
		value, ok := doc[key]
		if !ok || value == nil {
			return
		}
		seen[key] = true
		label := repairFieldLabels[key]
		if label == "" {
			label = "未定义字段（" + key + "）"
		}
		fields = append(fields, Field{Key: key, Label: label, Value: fmt.Sprint(value)})
	}
	for _, key := range priority {
		appendField(key)
	}
	keys := make([]string, 0, len(doc))
	for key := range doc {
		if !seen[key] && key != "_id" && key != "_source_key" {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	for _, key := range keys {
		appendField(key)
	}
	return fields
}

func orderDetailFields(doc bson.M) []Field {
	fields := make([]Field, 0, len(orderDocumentLabels)+len(orderDataFieldLabels))
	for _, key := range []string{"_id", "source", "source_aufnr", "aufnr", "record_count", "order_quantity", "storage_quantity", "gstrs_date", "first_seen_at", "last_synced_at", "sync_count"} {
		if value, ok := doc[key]; ok && value != nil {
			fields = append(fields, Field{Key: key, Label: orderDocumentLabels[key], Value: fmt.Sprint(value)})
		}
	}
	data, _ := doc["data"].(bson.M)
	for _, key := range orderDataFieldOrder {
		if value, ok := data[key]; ok && value != nil {
			fields = append(fields, Field{Key: "data." + key, Label: orderDataFieldLabels[key], Value: fmt.Sprint(value)})
		}
	}
	return fields
}

var repairFieldLabels = map[string]string{
	"MANDT": "集团", "PCODE": "主机条码", "ZWXDT": "维修日期时间", "ZMCOD1": "序列号", "ZRCOD1": "替换前原厂码", "MATNR": "物料号", "ZJXMC": "机型", "ZNGGZ": "NG工站", "ZNGWD": "不良现象维度", "ZWXWD": "维修维度", "ZBJ": "涉及部件", "ZZRFL": "缺陷责任分类", "ZCCLH": "重插部件料号", "ZGZMS": "故障描述", "MAKTX": "物料描述（短文本）", "ZMCOD2": "替换后曙光码", "ZRCOD2": "替换后原厂码", "ZDATE": "读取日期", "ZTIME": "读取时间", "ZUSER": "读取人", "ZSOURCE": "数据来源", "ZDATE_WX": "维修日期", "REJUDGE": "复判人员", "RET": "复判结论", "RPDESC": "复判问题描述", "RNOTE": "复判备注", "SECFLG": "二次物料标识", "FACTORY": "生产基地", "ZWXWD1": "维修维度1", "ZWXWD2": "维修维度2", "ZWXWD3": "维修维度3", "U_FIX": "维修人员", "FIX_REMARKS": "维修人员备注维修信息", "TESTID": "TESTID", "ZNGSPEC": "NG工序", "T_FIND": "NG时间", "ZNGWD1": "不良现象维度1", "ZNGWD2": "不良现象维度2", "ZNGWD3": "不良现象维度3", "ERROR_CODE": "错误码", "ERROR_MSG": "错误码描述", "RETEST_STATION": "重进产线站位名", "TEST_LOG_NAME": "测试日志名称", "SECOND_PART_NO": "重插物料序号", "RECORD01REPAIRM": "非关键件物料序号", "SLOT": "槽位", "AUFNR": "生产订单", "VBELN": "销售订单", "GSTRS": "计划开始时间", "POSNR": "行项目", "U_FIND": "NG报工人员", "U_RMA_NAME": "RMA复判人员", "RMA_RESULT": "RMA复判结论", "RMA_TYPE2": "故障部件二级",
	"_source_view": "源视图名", "_scope_run_id": "全量批次标识", "_sync_run_id": "同步批次标识", "_synced_at": "同步时间",
}

var orderDocumentLabels = map[string]string{
	"_id": "订单文档主键", "source": "SAP来源", "source_aufnr": "来源与生产订单组合键", "aufnr": "生产订单", "record_count": "订单明细行数", "order_quantity": "订单数量", "storage_quantity": "入库数量", "gstrs_date": "计划开始日期", "first_seen_at": "首次写入时间", "last_synced_at": "最近同步时间", "sync_count": "成功同步次数",
}

var orderDataFieldLabels = map[string]string{
	"MANDT": "集团", "AUFNR": "生产订单", "VBELN": "销售订单", "POSNR": "行项目", "ETENR": "分批行", "MATNR": "物料编号", "MAKTX": "物料描述", "CPXH": "产品型号", "ZSTAT": "MES状态回传", "WERKS": "下单工厂", "IF_L6": "L6标识", "IF_l6": "L6标识", "KID": "客户ID", "GAMNG": "订单数量", "AUART": "订单类型", "LGORT": "库存地点", "NAME1_ZU": "最终用户", "MAKTX_TH": "生产机型", "WMENG": "入库数量", "GSTRS": "计划开始时间", "ZDATE_STARTED": "订单实际开始时间",
}

var orderDataFieldOrder = []string{"MANDT", "AUFNR", "VBELN", "POSNR", "ETENR", "MATNR", "MAKTX", "CPXH", "ZSTAT", "WERKS", "IF_L6", "IF_l6", "KID", "GAMNG", "AUART", "LGORT", "NAME1_ZU", "MAKTX_TH", "WMENG", "GSTRS", "ZDATE_STARTED"}

func firstText(doc bson.M, keys ...string) string {
	for _, key := range keys {
		if value, ok := doc[key]; ok && value != nil {
			result := strings.TrimSpace(fmt.Sprint(value))
			if result != "" && result != "<nil>" {
				return result
			}
		}
	}
	return ""
}

func timeText(value any) string {
	if value == nil {
		return ""
	}
	switch typed := value.(type) {
	case time.Time:
		return typed.UTC().Format(time.RFC3339)
	case primitive.DateTime:
		return typed.Time().UTC().Format(time.RFC3339)
	default:
		return strings.TrimSpace(fmt.Sprint(value))
	}
}

func documentQuantity(doc bson.M, storedField, sourceField string) float64 {
	if value, ok := doc[storedField]; ok {
		return number(value)
	}
	if records, ok := doc["records"].(bson.A); ok {
		total := 0.0
		for _, record := range records {
			if row, ok := record.(bson.M); ok {
				total += number(row[sourceField])
			}
		}
		return total
	}
	if data, ok := doc["data"].(bson.M); ok {
		return number(data[sourceField])
	}
	return 0
}

func repairTime(doc bson.M) time.Time {
	if result := parseTime(doc, "ZWXDT"); !result.IsZero() {
		return result
	}
	if repairDate, repairClock := firstText(doc, "ZDATE_WX"), firstText(doc, "ZTIME"); repairDate != "" && repairClock != "" {
		if result := parseTimeText(repairDate + " " + repairClock); !result.IsZero() {
			return result
		}
	}
	if readDate, readClock := firstText(doc, "ZDATE"), firstText(doc, "ZTIME"); readDate != "" && readClock != "" {
		if result := parseTimeText(readDate + " " + readClock); !result.IsZero() {
			return result
		}
	}
	return parseTime(doc, "ZDATE_WX", "ZDATE", "_synced_at")
}

func parseTime(doc bson.M, keys ...string) time.Time {
	for _, key := range keys {
		value, ok := doc[key]
		if !ok {
			continue
		}
		if result, ok := value.(time.Time); ok {
			return result
		}
		if result, ok := value.(primitive.DateTime); ok {
			return result.Time()
		}
		raw, ok := value.(string)
		if !ok {
			continue
		}
		if result := parseTimeText(raw); !result.IsZero() {
			return result
		}
	}
	return time.Time{}
}

func parseTimeText(raw string) time.Time {
	for _, layout := range []string{"20060102 150405", "20060102150405", "2006-01-02 15:04:05", "2006/01/02 15:04:05", "20060102", time.RFC3339, "2006-01-02"} {
		if result, err := time.Parse(layout, strings.TrimSpace(raw)); err == nil {
			return result
		}
	}
	return time.Time{}
}
func number(value any) float64 {
	switch v := value.(type) {
	case float64:
		return v
	case float32:
		return float64(v)
	case int:
		return float64(v)
	case int32:
		return float64(v)
	case int64:
		return float64(v)
	default:
		var result float64
		_, _ = fmt.Sscan(fmt.Sprint(value), &result)
		return result
	}
}
