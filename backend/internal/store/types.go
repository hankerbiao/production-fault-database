package store

import (
	"strings"
	"sync"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
)

type Store struct {
	client          *mongo.Client
	repairs         *mongo.Collection
	orders          *mongo.Collection
	views           map[string]*mongo.Collection
	bomCacheMu      sync.RWMutex
	bomCache        map[string]bomStreamCacheEntry
	bomWarmDone     chan struct{}
	stationCacheMu  sync.RWMutex
	stationCache    map[string]bomStreamCacheEntry
	stationWarmDone chan struct{}
}

type bomStreamCacheEntry struct {
	data      []byte
	expiresAt time.Time
}

type Filters struct {
	Keyword, HostBarcode, DefectResponsibility, NGStation, SalesOrder, ProductionOrder string
	SNS, ProductionOrders, SalesOrders, DateFrom, DateTo, Station, ProductModel        string
	TimeField                                                                          string
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

type FaultSN struct {
	ProductionOrder string `json:"productionOrder"`
	SerialNumber    string `json:"serialNumber"`
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
	HostBarcodes           int64  `json:"hostBarcodes"`
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

const MaxBulkQueryRows = 10000
const MaxOrderQueryRows = MaxBulkQueryRows
const MaxViewQueryRows = MaxBulkQueryRows

var bomStreamFields = []string{"id", "AUFNR_1", "VBELN_EX", "MENGE_A", "MATNR", "LGORT", "BUDAT_MKPF"}
var stationStreamFields = []string{"id", "PCODE", "AUFNR", "KDAUF", "SPEC", "SPEC_DESC", "OPERATION", "ACTUAL_START_TIME", "ACTUAL_END_TIME", "MAKTX_TH", "LGORT", "LINE_CODE"}
var tsvSanitizer = strings.NewReplacer("\t", " ", "\r", " ", "\n", " ")

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
	MissingSalesOrder                                                                  bool
}
type ViewListResult struct {
	Items    []bson.M `json:"items"`
	Page     int      `json:"page"`
	PageSize int      `json:"pageSize"`
	Total    int64    `json:"total"`
}
type ViewStatsResult struct {
	Total                          int64  `json:"total"`
	SalesOrders                    int64  `json:"salesOrders"`
	ProductionOrders               int64  `json:"productionOrders"`
	MissingSalesOrder              int64  `json:"missingSalesOrder"`
	MissingProductionOrder         int64  `json:"missingProductionOrder"`
	MissingProductionOrderDistinct int64  `json:"missingProductionOrderDistinct"`
	DataStartDate                  string `json:"dataStartDate"`
	DataEndDate                    string `json:"dataEndDate"`
	LatestSyncedAt                 string `json:"latestSyncedAt"`
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

var viewAllProjections = map[string]bson.M{
	"ZSGV_ZSD124": {
		"_id": 1, "_source_key": 1, "AUFNR_1": 1, "VBELN_EX": 1, "MENGE_A": 1,
		"MATNR": 1, "LGORT": 1, "WERKS": 1, "BUDAT_MKPF": 1, "KUNNR": 1, "NAME1": 1,
	},
	"Z_V_ZMES_T_001": {
		"_id": 1, "_source_key": 1, "PCODE": 1, "AUFNR": 1, "KDAUF": 1,
		"SPEC": 1, "SPEC_DESC": 1, "OPERATION": 1, "ACTUAL_START_TIME": 1,
		"ACTUAL_END_TIME": 1, "MAKTX_TH": 1, "LGORT": 1, "LINE_CODE": 1,
	},
}

const viewListIndexName = "api_view_list_order"
const bomStreamCacheTTL = 60 * time.Second
const bomStreamCacheMaxBytes = 64 << 20
const stationStreamCacheTTL = 60 * time.Second
const stationStreamCacheMaxBytes = 96 << 20

// Bulk and board queries use these narrow projections; detail endpoints read
// the complete source document separately.
var repairListProjection = bson.M{
	"_source_key": 1, "PCODE": 1, "ZMCOD1": 1, "ERROR_CODE": 1, "ZGZMS": 1, "ERROR_MSG": 1, "RPDESC": 1, "ZZRFL": 1,
	"ZWXDT": 1, "ZDATE_WX": 1, "ZDATE": 1, "ZTIME": 1, "ZUSER": 1, "U_FIX": 1, "VBELN": 1, "AUFNR": 1, "MATNR": 1, "MAKTX": 1, "ZNGGZ": 1, "RETEST_STATION": 1, "FIX_REMARKS": 1, "_synced_at": 1,
}

var orderListProjection = bson.M{
	"source": 1, "aufnr": 1, "record_count": 1, "order_quantity": 1, "storage_quantity": 1,
	"data.VBELN": 1, "data.KID": 1, "data.NAME1_ZU": 1, "data.MAKTX": 1, "data.MAKTX_TH": 1, "data.LGORT": 1, "data.GSTRS": 1, "data.GAMNG": 1, "data.WMENG": 1,
	"data.IF_L6": 1, "data.ZSTAT": 1, "data.AUART": 1,
	"data.出厂日期": 1, "data.生产日期": 1, "data.FACTORY_DATE": 1, "data.保修结束日期": 1, "data.质保到期日": 1, "data.WARRANTY_END_DATE": 1,
	"records.GAMNG": 1, "records.WMENG": 1,
}
