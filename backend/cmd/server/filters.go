package main

import (
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"production-fault-gateway/internal/store"
)

func pagination(r *http.Request) (int, int) {
	page, _ := strconv.Atoi(r.URL.Query().Get("page"))
	if page < 1 {
		page = 1
	}
	pageSize, _ := strconv.Atoi(r.URL.Query().Get("pageSize"))
	if pageSize < 1 || pageSize > 100 {
		pageSize = 20
	}
	return page, pageSize
}

func query(q url.Values, key string) string { return strings.TrimSpace(q.Get(key)) }

func repairFilters(q url.Values) store.Filters {
	return store.Filters{
		Keyword: query(q, "keyword"), HostBarcode: query(q, "hostBarcode"), DefectResponsibility: query(q, "defectResponsibility"), NGStation: query(q, "ngStation"),
		SalesOrder: query(q, "salesOrder"), ProductionOrder: query(q, "productionOrder"), SNS: query(q, "sns"), ProductionOrders: query(q, "productionOrders"), SalesOrders: query(q, "salesOrders"),
		DateFrom: query(q, "dateFrom"), DateTo: query(q, "dateTo"), Station: query(q, "station"), ProductModel: query(q, "productModel"), TimeField: query(q, "timeField"),
	}
}

func orderFilters(q url.Values) store.OrderFilters {
	return store.OrderFilters{
		Keyword: query(q, "keyword"), Source: query(q, "source"), GSTRSFrom: query(q, "gstrsFrom"), GSTRSTo: query(q, "gstrsTo"),
		SalesOrder: query(q, "salesOrder"), ProductionOrder: query(q, "productionOrder"), SerialNumber: query(q, "serialNumber"), ProductModel: query(q, "productModel"),
		Customer: query(q, "customer"), Base: query(q, "base"), DateFrom: query(q, "dateFrom"), DateTo: query(q, "dateTo"), OrderScope: query(q, "orderScope"),
	}
}

func viewFilters(q url.Values) store.ViewFilters {
	return store.ViewFilters{
		Keyword: query(q, "keyword"), From: query(q, "from"), To: query(q, "to"), DateFrom: query(q, "dateFrom"), DateTo: query(q, "dateTo"), StationCode: query(q, "stationCode"),
		SN: query(q, "sn"), ProductionOrder: query(q, "productionOrder"), SalesOrder: query(q, "salesOrder"), Base: query(q, "base"), ProductModel: query(q, "productModel"),
		HeadOrder: query(q, "headOrder"), ItemOrder: query(q, "itemOrder"), HeadSN: query(q, "headSn"), ItemSN: query(q, "itemSn"), MaterialCode: query(q, "materialCode"), MissingSalesOrder: parseBool(q.Get("missingSalesOrder")),
		DOACode: query(q, "doaCode"), DOAType: query(q, "doaType"), Status: query(q, "status"), Customer: query(q, "customer"), ServiceOrder: query(q, "serviceOrder"),
		ChangeType: query(q, "changeType"), PartNumber: query(q, "partNumber"), PartSN: query(q, "partSn"), Operator: query(q, "operator"), NeedReturn: query(q, "needReturn"), Revoked: query(q, "revoked"),
		Company5000: query(q, "company5000"),
		Judgment:    query(q, "judgment"), EnrichmentStatus: query(q, "enrichmentStatus"),
		AcceptedDateFrom: query(q, "acceptedDateFrom"), AcceptedDateTo: query(q, "acceptedDateTo"),
	}
}

func parseBool(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "y":
		return true
	default:
		return false
	}
}
