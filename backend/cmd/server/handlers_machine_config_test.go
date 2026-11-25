package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.mongodb.org/mongo-driver/bson"
)

func TestMachineConfigAggregatesLocalBOM(t *testing.T) {
	f := &fakeStore{viewItems: map[string][]bson.M{
		machineStationViewID: {
			{"PCODE": "6102310604796170", "AUFNR": "000030240235", "GSTRS": "20261123"},
			{"PCODE": "6102310604796170", "AUFNR": "000030240235", "GSTRS": "20261124"},
		},
		machineBOMViewID: {
			{"MATNR_BI": "33000348", "MAKTX": "云电AP-CA3200F54B1钛金3200W CRPS电源模块（TX）", "MATKL": "330002", "WGBEZ": "电源模块", "LGORT": "1N00", "MENGE": "60", "MENGE_A": "60", "PSMNG": "10", "BWART": "261"},
			{"MATNR_BI": "B6200004", "MAKTX": "Intel 8457C 2.6G 97.5M 48C 350W LJS(B02)", "MATKL": "620002", "WGBEZ": "Intel CPU", "LGORT": "1N00", "MENGE": "26", "MENGE_A": "26", "PSMNG": "10", "BWART": "261"},
			{"MATNR_BI": "52001052", "MAKTX": "干燥剂50 克袋装", "MATKL": "520001", "WGBEZ": "包装材料", "LGORT": "1N00", "MENGE": "39", "MENGE_A": "39", "PSMNG": "10", "BWART": "261"},
		},
	}}
	router := registerRoutes(newTestServer(f))
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/machines/config/5000?sn=6102310604796170", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("unexpected status: %d %s", response.Code, response.Body.String())
	}
	var items []machineConfigItem
	if err := json.Unmarshal(response.Body.Bytes(), &items); err != nil {
		t.Fatal(err)
	}
	if len(items) != 2 {
		t.Fatalf("packaging should be excluded, got %d items: %s", len(items), response.Body.String())
	}

	var psu *machineConfigItem
	for i := range items {
		if items[i].DN == "33000348" {
			psu = &items[i]
		}
	}
	if psu == nil {
		t.Fatalf("missing PSU item: %s", response.Body.String())
	}
	if psu.Num != 6 || psu.TotalNum != 60 || psu.Type != "DYMK" || psu.MaterialGroup != "330002" {
		t.Fatalf("unexpected PSU item: %+v", psu)
	}
	if psu.ID != "local-30240235-33000348" || psu.RelationID != "local-33000348" {
		t.Fatalf("unexpected ids: %s %s", psu.ID, psu.RelationID)
	}
	if len(psu.Params) != 2 {
		t.Fatalf("expected 型号+库位 params, got %+v", psu.Params)
	}
	if psu.Params[0].Name != "型号" || psu.Params[0].ComparionValue != "AP-CA3200F54B1" {
		t.Fatalf("unexpected model param: %+v", psu.Params[0])
	}
	if psu.Params[1].Name != "库位" || psu.Params[1].ComparionValue != "1N00" || psu.Params[1].OrderID != 30240235 {
		t.Fatalf("unexpected location param: %+v", psu.Params[1])
	}
}

func TestMachineConfigRejectsMissingSNAndOrder(t *testing.T) {
	for name, tc := range map[string]struct {
		url      string
		store    *fakeStore
		expected int
	}{
		"missing sn": {"/api/machines/config/5000", &fakeStore{}, http.StatusBadRequest},
		"no station": {"/api/machines/config/5000?sn=UNKNOWN", &fakeStore{}, http.StatusNotFound},
		"empty order": {"/api/machines/config/5000?sn=UNKNOWN", &fakeStore{viewItems: map[string][]bson.M{
			machineStationViewID: {{"PCODE": "UNKNOWN", "AUFNR": "000000000000"}},
		}}, http.StatusNotFound},
	} {
		router := registerRoutes(newTestServer(tc.store))
		response := httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, tc.url, nil))
		if response.Code != tc.expected {
			t.Fatalf("%s: expected %d, got %d %s", name, tc.expected, response.Code, response.Body.String())
		}
	}
}

func TestMachineConfigUsesSignedQuantitiesAndNetReturns(t *testing.T) {
	rows := []bson.M{
		{"MATNR_BI": "B5000123", "MAKTX": "SA M321RYGA0PB2-CCP DDR5 64R 96G内存-T02", "MATKL": "500001", "LGORT": "1N00", "MENGE": "312", "MENGE_A": "312", "PSMNG": "13", "BWART": "261"},
		{"MATNR_BI": "B5000123", "MAKTX": "SA M321RYGA0PB2-CCP DDR5 64R 96G内存-T02", "MATKL": "500001", "LGORT": "1N00", "MENGE": "-312", "MENGE_A": "312", "PSMNG": "13", "BWART": "262"},
		{"MATNR_BI": "B5000130", "MAKTX": "HY HMCGM4MHBRB DDR5 64R 96G内存-T02", "MATKL": "500001", "LGORT": "1N00", "MENGE": "312", "MENGE_A": "312", "PSMNG": "13", "BWART": "261"},
		{"MATNR_BI": "3003384", "MAKTX": "丰衡E20机箱SUNON6056风扇模组（B02定制）R1", "MATKL": "030002", "LGORT": "9N00", "MENGE": "", "MENGE_A": "438", "PSMNG": "80", "BWART": "261"},
	}
	items := buildMachineConfigItems("10350750", rows)
	if len(items) != 2 {
		t.Fatalf("returned material should net to zero, got %+v", items)
	}
	var memory, fan machineConfigItem
	for _, item := range items {
		switch item.DN {
		case "B5000130":
			memory = item
		case "3003384":
			fan = item
		}
	}
	if memory.Num != 24 || memory.TotalNum != 312 {
		t.Fatalf("unexpected memory quantities: %+v", memory)
	}
	if fan.TotalNum != 438 || fan.Num != 5.475 {
		t.Fatalf("partial posting should stay fractional: %+v", fan)
	}
	if len(fan.Params) != 2 || fan.Params[0].ComparionValue != "SUNON6056" {
		t.Fatalf("unexpected fan params: %+v", fan.Params)
	}
}

func TestMachineConfigModelNameHeuristics(t *testing.T) {
	for description, expected := range map[string]string{
		"云电AP-CA3200F54B1钛金3200W CRPS电源模块（TX）":     "AP-CA3200F54B1",
		"Intel 8457C 2.6G 97.5M 48C 350W LJS(B02)": "8457C",
		"HY HMCT04MHBRC DDR5 64R 128G内存-B02":       "HMCT04MHBRC",
		"丰衡E20-D216 标准机箱(B02定制)R2":                 "E20-D216",
		"SDM SSDPF2KX038T1 3.84T 2.5-E4x4 RSSDB02": "SSDPF2KX038T1",
		"BCM957508-N2100GD 双口100G O3S网卡-B02":       "BCM957508-N2100GD",
	} {
		if got := machineModelName(description); got != expected {
			t.Errorf("machineModelName(%q) = %q, want %q", description, got, expected)
		}
	}
}

func TestMachineConfigCableGroupKeepsOnlyLocation(t *testing.T) {
	items := buildMachineConfigItems("10350845", []bson.M{{
		"MATNR_BI": "3700001",
		"MAKTX":    "金信诺27CM Micro Fit4P转MicroFit4P供电Yichun线缆",
		"MATKL":    "370001",
		"LGORT":    "1N00",
		"MENGE":    "10",
		"PSMNG":    "1",
		"BWART":    "261",
	}})
	if len(items) != 1 {
		t.Fatalf("expected 1 cable item, got %d", len(items))
	}
	if len(items[0].Params) != 1 || items[0].Params[0].Name != "库位" {
		t.Fatalf("cable group should keep only location param, got %+v", items[0].Params)
	}
}

func TestMachineConfigPagesThroughBOM(t *testing.T) {
	rows := make([]bson.M, 0, 250)
	for i := 0; i < 250; i++ {
		rows = append(rows, bson.M{
			"MATNR_BI": fmt.Sprintf("B%07d", i),
			"MAKTX":    "测试物料",
			"MATKL":    "240001",
			"LGORT":    "1N00",
			"MENGE":    "1",
			"PSMNG":    "1",
			"BWART":    "261",
		})
	}
	s := &server{store: &fakeStore{viewItems: map[string][]bson.M{machineBOMViewID: rows}}}
	got, truncated, err := s.machineBOMRows(context.Background(), "10350845")
	if err != nil {
		t.Fatal(err)
	}
	if truncated {
		t.Fatalf("250 rows should fit in the configured page limit")
	}
	if len(got) != 250 {
		t.Fatalf("expected 250 rows from paginated BOM, got %d", len(got))
	}
	items := buildMachineConfigItems("10350845", got)
	if len(items) != 250 {
		t.Fatalf("expected 250 items, got %d", len(items))
	}
}
