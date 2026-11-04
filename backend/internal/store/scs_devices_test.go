package store

import (
	"go.mongodb.org/mongo-driver/bson"
	"testing"
)

func TestSCSDevicesGroupEvidenceAndRejectConflictingDates(t *testing.T) {
	makeDoc := func(code, barcode, material, order, prod, factory, warranty string) bson.M {
		return bson.M{"doa_code": code, "service_uid": "SERVICE-1", "sales_order": "PARENT", "is_5000_company": true, "_source": "scs", "details": bson.M{"equipment": bson.A{bson.M{"material_sn": material, "leave_factory_date": factory, "warranty_deadline": warranty, "context_value": "context-" + barcode, "product_fields": bson.M{"产品条码": barcode, "销售订单": order, "生产订单": prod, "出厂日期": factory, "质保到期日": warranty}}}}}
	}
	docs := []bson.M{makeDoc("D1", "HOST-1", "PART-1", "SO-1", "PO-1", "2026-01-01", "2029-01-01"), makeDoc("D2", "HOST-1", "PART-2", "SO-1", "PO-1", "2026-01-01", "2030-01-01"), makeDoc("D1", "HOST-2", "PART-3", "SO-2", "PO-2", "2026-02-01", "2029-02-01")}
	got := groupSCSDevices(docs, SCSDeviceFilters{})
	if len(got) != 2 {
		t.Fatalf("got %d devices", len(got))
	}
	one := got[0]
	if one.DeviceKey != "product:HOST-1" || len(one.Evidence) != 2 || one.WarrantyEndDate.Status != "conflict" || one.WarrantyEndDate.Value != "" || one.FactoryDate.Value != "2026-01-01" || one.SalesOrder.Value != "SO-1" {
		t.Fatalf("unexpected device: %+v", one)
	}
	if one.Evidence[0].DOASalesOrder != "PARENT" || one.Evidence[0].MaterialSN != "PART-1" {
		t.Fatal("lost association")
	}
	if one.Evidence[0].SalesOrderRelation != "different" || one.Evidence[0].SNRelation != "different" {
		t.Fatal("conflicting source associations must remain visible")
	}
	filtered := groupSCSDevices(docs, SCSDeviceFilters{ServiceOrder: "SERVICE-1", SalesOrder: "SO-1"})
	if len(filtered) != 1 || len(filtered[0].Evidence) != 2 {
		t.Fatal("filter lost or duplicated device evidence")
	}
}

func TestSCSDateInvalidMissingAndConflict(t *testing.T) {
	for _, tc := range []struct {
		in            []string
		status, value string
	}{{nil, "missing", ""}, {[]string{"2026-02-30"}, "invalid", ""}, {[]string{"2026-01-02 00:00", "20260102"}, "valid", "2026-01-02"}, {[]string{"2026-01-02", "2026-01-03"}, "conflict", ""}} {
		got := resolveSCSValues(tc.in, true)
		if got.Status != tc.status || got.Value != tc.value {
			t.Fatalf("%v: %+v", tc.in, got)
		}
	}
}
