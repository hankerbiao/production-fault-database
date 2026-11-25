package store

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo/options"
)

// SCSDeviceFilters select devices after grouping all accessible DOA evidence.
type SCSDeviceFilters struct {
	Company5000  string
	SalesOrder   string
	ServiceOrder string
	SN           string
}
type SCSValue struct {
	Value      string   `json:"value"`
	Status     string   `json:"status"`
	Candidates []string `json:"candidates"`
	Invalid    []string `json:"invalid,omitempty"`
}
type SCSDeviceEvidence struct {
	DOACode             string   `json:"doaCode"`
	Judgment            string   `json:"judgment"`
	DOAStatus           string   `json:"doaStatus"`
	ServiceOrder        string   `json:"serviceOrder"`
	ServiceOrderAliases []string `json:"serviceOrderAliases"`
	DOASalesOrder       string   `json:"doaSalesOrder"`
	SalesOrder          string   `json:"salesOrder"`
	SalesOrderRelation  string   `json:"salesOrderRelation"`
	ProductionOrder     string   `json:"productionOrder"`
	ProductBarcode      string   `json:"productBarcode"`
	MaterialSN          string   `json:"materialSn"`
	SNRelation          string   `json:"snRelation"`
	ContextID           string   `json:"contextId"`
	Model               string   `json:"model"`
	Product             string   `json:"product"`
	FactoryProduct      string   `json:"factoryProduct"`
	FactoryEquipment    string   `json:"factoryEquipment"`
	WarrantyProduct     string   `json:"warrantyProduct"`
	WarrantyEquipment   string   `json:"warrantyEquipment"`
	ShipmentDate        string   `json:"shipmentDate"`
	Company5000         *bool    `json:"company5000"`
	Source              string   `json:"source"`
	DetailPending       bool     `json:"detailPending"`
}
type SCSDevice struct {
	DeviceKey       string              `json:"deviceKey"`
	IdentityBasis   string              `json:"identityBasis"`
	DeviceSN        string              `json:"deviceSn"`
	SalesOrder      SCSValue            `json:"salesOrder"`
	ProductionOrder SCSValue            `json:"productionOrder"`
	FactoryDate     SCSValue            `json:"factoryDate"`
	WarrantyEndDate SCSValue            `json:"warrantyEndDate"`
	Evidence        []SCSDeviceEvidence `json:"evidence"`
}
type SCSDevicePage struct {
	Items    []SCSDevice `json:"items"`
	Page     int         `json:"page"`
	PageSize int         `json:"pageSize"`
	Total    int         `json:"total"`
	HasMore  bool        `json:"hasMore"`
	Scope    string      `json:"scope"`
}

func mapDoc(v any) bson.M {
	if x, ok := v.(bson.M); ok {
		return x
	}
	if x, ok := v.(map[string]any); ok {
		return bson.M(x)
	}
	return bson.M{}
}

func scsRelation(a, b string) string {
	if a == "" || b == "" {
		return "missing"
	}
	if a == b {
		return "same"
	}
	return "different"
}
func resolveSCSValues(values []string, dates bool) SCSValue {
	unique := map[string]bool{}
	invalid := map[string]bool{}
	for _, raw := range values {
		v := strings.TrimSpace(raw)
		if v == "" {
			continue
		}
		if dates {
			var parsed time.Time
			for _, layout := range []string{"2006-01-02", "20060102", "2006/01/02", "2006-01-02 15:04", "2006-01-02 15:04:05", "2006/01/02 15:04", "2006/01/02 15:04:05", time.RFC3339} {
				if value, err := time.Parse(layout, v); err == nil {
					parsed = value
					break
				}
			}
			if parsed.IsZero() {
				invalid[v] = true
				continue
			}
			v = parsed.Format("2006-01-02")
		}
		unique[v] = true
	}
	result := SCSValue{Candidates: []string{}}
	for v := range unique {
		result.Candidates = append(result.Candidates, v)
	}
	sort.Strings(result.Candidates)
	for v := range invalid {
		result.Invalid = append(result.Invalid, v)
	}
	sort.Strings(result.Invalid)
	switch {
	case len(result.Candidates) > 1:
		result.Status = "conflict"
	case len(result.Invalid) > 0:
		result.Status = "invalid"
	case len(result.Candidates) == 0:
		result.Status = "missing"
	default:
		result.Status = "valid"
		result.Value = result.Candidates[0]
	}
	return result
}

// groupSCSDevices retains all DOA associations and never inherits a parent order as a device order.
func groupSCSDevices(docs []bson.M, f SCSDeviceFilters) []SCSDevice {
	groups := map[string]*SCSDevice{}
	for _, doc := range docs {
		details := mapDoc(doc["details"])
		service := mapDoc(details["service_fields"])
		equipment, ok := details["equipment"].(bson.A)
		if !ok {
			if a, yes := details["equipment"].([]any); yes {
				equipment = bson.A(a)
			}
		}
		parentID := firstText(doc, "doa_code", "_source_key", "_id")
		company, hasCompany := doc["is_5000_company"].(bool)
		var cp *bool
		if hasCompany {
			v := company
			cp = &v
		}
		for index, item := range equipment {
			e := mapDoc(item)
			p := mapDoc(e["product_fields"])
			barcode := firstText(p, "产品条码")
			material := firstText(e, "material_sn", "mtr_sn")
			contextID := firstText(e, "context_value", "_context_value")
			key, basis := "", ""
			switch {
			case barcode != "":
				key, basis = "product:"+barcode, "product_barcode"
			case contextID != "":
				key, basis = "context:"+contextID, "product_context"
			case material != "":
				key, basis = "material:"+material, "material_sn"
			default:
				key, basis = fmt.Sprintf("unidentified:%s:%d", parentID, index), "missing"
			}
			g := groups[key]
			if g == nil {
				sn := barcode
				if basis == "material_sn" {
					sn = material
				}
				g = &SCSDevice{DeviceKey: key, IdentityBasis: basis, DeviceSN: sn, Evidence: []SCSDeviceEvidence{}}
				groups[key] = g
			}
			aliases := map[string]bool{}
			for _, v := range []string{firstText(doc, "service_uid"), firstText(service, "服务单号"), firstText(service, "服务订单号"), firstText(service, "服务单")} {
				if v != "" {
					aliases[v] = true
				}
			}
			aliasList := []string{}
			for v := range aliases {
				aliasList = append(aliasList, v)
			}
			sort.Strings(aliasList)
			pending, _ := doc["_detail_pending"].(bool)
			parentOrder, deviceOrder := firstText(doc, "sales_order"), firstText(p, "销售订单")
			model, product := firstText(p, "主机型号"), firstText(p, "产品名称")
			if model == "" {
				model = firstText(e, "host_model")
			}
			if product == "" {
				product = firstText(e, "mtr_name")
			}
			source := firstText(doc, "_source")
			if source == "" {
				source = "scs"
			}
			g.Evidence = append(g.Evidence, SCSDeviceEvidence{DOACode: parentID, Judgment: firstText(doc, "doa_judge"), DOAStatus: firstText(doc, "status"), ServiceOrder: firstText(doc, "service_uid"), ServiceOrderAliases: aliasList, DOASalesOrder: parentOrder, SalesOrder: deviceOrder, SalesOrderRelation: scsRelation(parentOrder, deviceOrder), ProductionOrder: firstText(p, "生产订单"), ProductBarcode: barcode, MaterialSN: material, SNRelation: scsRelation(barcode, material), ContextID: contextID, Model: model, Product: product, FactoryProduct: firstText(p, "出厂日期"), FactoryEquipment: firstText(e, "leave_factory_date"), WarrantyProduct: firstText(p, "质保到期日"), WarrantyEquipment: firstText(e, "warranty_deadline"), ShipmentDate: firstText(doc, "shipment_date"), Company5000: cp, Source: source, DetailPending: pending})
		}
	}
	out := make([]SCSDevice, 0, len(groups))
	for _, g := range groups {
		var sales, prod, factory, warranty []string
		selected := f.Company5000 == "" && f.SalesOrder == "" && f.ServiceOrder == "" && f.SN == ""
		for _, e := range g.Evidence {
			sales = append(sales, e.SalesOrder)
			prod = append(prod, e.ProductionOrder)
			factory = append(factory, e.FactoryProduct, e.FactoryEquipment)
			warranty = append(warranty, e.WarrantyProduct, e.WarrantyEquipment)
			companyMatch := f.Company5000 == "" || (e.Company5000 != nil && *e.Company5000 == parseBoolFilter(f.Company5000))
			salesMatch := f.SalesOrder == "" || e.SalesOrder == f.SalesOrder || e.DOASalesOrder == f.SalesOrder
			serviceMatch := f.ServiceOrder == ""
			for _, a := range e.ServiceOrderAliases {
				if a == f.ServiceOrder {
					serviceMatch = true
				}
			}
			snMatch := f.SN == "" || e.ProductBarcode == f.SN || e.MaterialSN == f.SN
			if companyMatch && salesMatch && serviceMatch && snMatch {
				selected = true
			}
		}
		if !selected {
			continue
		}
		sort.Slice(g.Evidence, func(i, j int) bool {
			a, b := g.Evidence[i], g.Evidence[j]
			if a.DOACode != b.DOACode {
				return a.DOACode < b.DOACode
			}
			return a.ContextID < b.ContextID
		})
		g.SalesOrder = resolveSCSValues(sales, false)
		g.ProductionOrder = resolveSCSValues(prod, false)
		g.FactoryDate = resolveSCSValues(factory, true)
		g.WarrantyEndDate = resolveSCSValues(warranty, true)
		out = append(out, *g)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].DeviceKey < out[j].DeviceKey })
	return out
}

func scsDeviceProjection() bson.M {
	projection := bson.M{"_id": 1, "_source_key": 1, "doa_code": 1, "doa_judge": 1, "status": 1, "service_uid": 1, "sales_order": 1, "shipment_date": 1, "is_5000_company": 1, "_source": 1, "_detail_pending": 1, "details.service_fields.服务单号": 1, "details.service_fields.服务订单号": 1, "details.service_fields.服务单": 1}
	for _, field := range []string{"material_sn", "mtr_sn", "context_value", "_context_value", "host_model", "mtr_name", "leave_factory_date", "warranty_deadline", "product_fields.产品条码", "product_fields.销售订单", "product_fields.生产订单", "product_fields.主机型号", "product_fields.产品名称", "product_fields.出厂日期", "product_fields.质保到期日"} {
		projection["details.equipment."+field] = 1
	}
	return projection
}

func scsDeviceFilter(f SCSDeviceFilters) bson.M {
	conditions := bson.A{}
	if strings.TrimSpace(f.Company5000) != "" {
		conditions = append(conditions, bson.M{"is_5000_company": parseBoolFilter(f.Company5000)})
	}
	if value := strings.TrimSpace(f.SalesOrder); value != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{
			bson.M{"sales_order": value},
			bson.M{"details.equipment.product_fields.销售订单": value},
		}})
	}
	if value := strings.TrimSpace(f.ServiceOrder); value != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{
			bson.M{"service_uid": value},
			bson.M{"details.service_fields.服务单号": value},
			bson.M{"details.service_fields.服务订单号": value},
			bson.M{"details.service_fields.服务单": value},
		}})
	}
	if value := strings.TrimSpace(f.SN); value != "" {
		conditions = append(conditions, bson.M{"$or": bson.A{
			bson.M{"details.equipment.product_fields.产品条码": value},
			bson.M{"details.equipment.material_sn": value},
			bson.M{"details.equipment.mtr_sn": value},
		}})
	}
	if len(conditions) == 0 {
		return bson.M{}
	}
	return bson.M{"$and": conditions}
}

func (s *Store) SCSDevices(ctx context.Context, f SCSDeviceFilters, page, pageSize int) (SCSDevicePage, error) {
	result := SCSDevicePage{Items: []SCSDevice{}, Page: page, PageSize: pageSize, Scope: "all"}
	devices, err := s.scsDevices(ctx, f)
	if err != nil {
		return result, err
	}
	result.Total = len(devices)
	start := (page - 1) * pageSize
	if start >= len(devices) {
		return result, nil
	}
	end := start + pageSize
	if end > len(devices) {
		end = len(devices)
	}
	result.Items = devices[start:end]
	result.HasMore = end < len(devices)
	return result, nil
}

// The unfiltered dataset is grouped before paging. Reuse it across pages.
func (s *Store) scsDevices(ctx context.Context, f SCSDeviceFilters) ([]SCSDevice, error) {
	if f != (SCSDeviceFilters{}) {
		return s.loadSCSDevices(ctx, f)
	}
	s.scsDevicesMu.Lock()
	defer s.scsDevicesMu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if time.Now().Before(s.scsDevicesExpires) {
		return s.scsDevicesSnapshot, nil
	}
	devices, err := s.loadSCSDevices(ctx, f)
	if err == nil {
		s.scsDevicesSnapshot = devices
		s.scsDevicesExpires = time.Now().Add(30 * time.Second)
	}
	return devices, err
}

func (s *Store) loadSCSDevices(ctx context.Context, f SCSDeviceFilters) ([]SCSDevice, error) {
	col := s.views["SCS_DOA"]
	read := func(filter bson.M) ([]bson.M, error) {
		cur, err := col.Find(ctx, filter, options.Find().SetProjection(scsDeviceProjection()).SetBatchSize(500))
		if err != nil {
			return nil, err
		}
		defer cur.Close(ctx)
		docs := []bson.M{}
		for cur.Next(ctx) {
			var doc bson.M
			if err := cur.Decode(&doc); err != nil {
				return nil, err
			}
			docs = append(docs, doc)
		}
		if err := cur.Err(); err != nil {
			return nil, err
		}
		return docs, nil
	}
	docs, err := read(scsDeviceFilter(f))
	if err != nil {
		return nil, err
	}
	if len(docs) > 0 && (f.Company5000 != "" || f.SalesOrder != "" || f.ServiceOrder != "" || f.SN != "") {
		selected := groupSCSDevices(docs, f)
		identities := map[string]map[string]struct{}{
			"product": {}, "context": {}, "material": {},
		}
		for _, device := range selected {
			kind, value, ok := strings.Cut(device.DeviceKey, ":")
			if !ok || identities[kind] == nil {
				continue
			}
			identities[kind][value] = struct{}{}
		}
		seen := make(map[string]struct{}, len(docs))
		for _, doc := range docs {
			seen[fmt.Sprint(doc["_id"])] = struct{}{}
		}
		paths := map[string][]string{
			"product":  {"details.equipment.product_fields.产品条码"},
			"context":  {"details.equipment.context_value", "details.equipment._context_value"},
			"material": {"details.equipment.material_sn", "details.equipment.mtr_sn"},
		}
		const identityBatchSize = 500
		for kind, values := range identities {
			batch := make([]string, 0, len(values))
			for value := range values {
				batch = append(batch, value)
			}
			for start := 0; start < len(batch); start += identityBatchSize {
				end := min(start+identityBatchSize, len(batch))
				for _, path := range paths[kind] {
					related, err := read(bson.M{path: bson.M{"$in": batch[start:end]}})
					if err != nil {
						return nil, err
					}
					for _, doc := range related {
						id := fmt.Sprint(doc["_id"])
						if _, ok := seen[id]; ok {
							continue
						}
						seen[id] = struct{}{}
						docs = append(docs, doc)
					}
				}
			}
		}
	}
	return groupSCSDevices(docs, f), nil
}
