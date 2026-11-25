package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"production-fault-gateway/internal/store"
)

const (
	machineStationViewID = "Z_V_ZMES_T_001"
	machineBOMViewID     = "ZSGV_ZSD124"
	machineBOMSeedSize   = 100
	machineBOMMaxPages   = 100
	machineConfigTimeout = 15 * time.Second
)

// machineConfigNonComponentGroups are packaging, label and screw groups. They are
// posted to the production order but do not belong to the machine configuration.
var machineConfigNonComponentGroups = map[string]bool{
	"520001": true, // 包装材料
	"520002": true, // 标签标牌
	"030005": true, // 机箱附件（金属螺钉）
	"030011": true, // 机箱附件（非金属类）
}

var machineComponentTypes = map[string]string{
	"620001": "CPU",
	"620002": "CPU",
	"500001": "MEM",
	"540009": "YP",
	"540010": "YP",
	"240001": "MB",
	"240023": "NETC",
	"240024": "NETC",
	"240007": "NETC",
	"240008": "XK",
	"330002": "DYMK",
	"570001": "JX",
	"030002": "FS",
	"030001": "QTBK",
	"030015": "QTBK",
	"240010": "QTBK",
	"240025": "QTBK",
	"370001": "XL",
	"370002": "XL",
	"370003": "XL",
}

// machineModelGroups limit heuristic 型号 extraction to descriptions whose
// second token is reliably the model. Boards and cables keep only 库位.
var machineModelGroups = map[string]bool{
	"620001": true,
	"620002": true,
	"500001": true,
	"540009": true,
	"540010": true,
	"330002": true,
	"030002": true,
	"570001": true,
	"240023": true,
	"240024": true,
	"240007": true,
}

type machineConfigParam struct {
	ID               string `json:"id"`
	OrderID          int64  `json:"orderId"`
	RelationID       string `json:"relationId"`
	MaterialSN       string `json:"materialSn"`
	ComparionValue   string `json:"comparionValue"`
	Name             string `json:"name"`
	PartsDescription string `json:"partsDescription"`
	MaterialGroup    string `json:"materialGroup"`
}

type machineConfigItem struct {
	NeedNum       *int                 `json:"needNum"`
	Num           float64              `json:"num"`
	TotalNum      float64              `json:"totalNum"`
	Name          string               `json:"name"`
	MaterialGroup string               `json:"materialgroup"`
	DN            string               `json:"dn"`
	RelationID    string               `json:"relationId"`
	ID            string               `json:"id"`
	Type          string               `json:"type"`
	Params        []machineConfigParam `json:"params"`
}

type machineMaterial struct {
	dn        string
	name      string
	group     string
	groupName string
	location  string
	psmng     float64
	total     float64
}

// machineConfig resolves one SN through the station view and returns the
// material configuration of its production order in the steps-compatible shape.
func (s *server) machineConfig(w http.ResponseWriter, r *http.Request) {
	sn := strings.TrimSpace(r.URL.Query().Get("sn"))
	if sn == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "缺少 sn 参数"})
		return
	}
	if len(sn) > 40 || strings.ContainsAny(sn, " \t\r\n") {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sn 参数无效"})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), machineConfigTimeout)
	defer cancel()

	station, err := s.store.ViewList(ctx, machineStationViewID, store.ViewFilters{SN: sn}, 1, 100, false, true)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	orderID := machineProductionOrder(station.Items)
	if orderID == "" {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "未找到 SN " + sn + " 对应的生产订单"})
		return
	}

	rows, truncated, err := s.machineBOMRows(ctx, orderID)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	if truncated {
		// Keep the steps-compatible array response unchanged, but make the
		// truncation observable to clients and operations.
		w.Header().Set("X-Machine-BOM-Truncated", "true")
	}
	writeJSON(w, http.StatusOK, buildMachineConfigItems(orderID, rows))
}

func (s *server) machineBOMRows(ctx context.Context, orderID string) ([]bson.M, bool, error) {
	rows := make([]bson.M, 0)
	for page := 1; page <= machineBOMMaxPages; page++ {
		result, err := s.store.ViewList(ctx, machineBOMViewID, store.ViewFilters{ProductionOrder: orderID}, page, machineBOMSeedSize, false, page == 1)
		if err != nil {
			return nil, false, err
		}
		rows = append(rows, result.Items...)
		if len(result.Items) < machineBOMSeedSize {
			return rows, false, nil
		}
	}
	return rows, true, nil
}

// machineProductionOrder returns the latest non-empty AUFNR among station rows.
func machineProductionOrder(rows []bson.M) string {
	best, bestDate := "", ""
	for _, row := range rows {
		order := normalizeMachineOrder(bsonText(row["AUFNR"]))
		if order == "" || order == "0" {
			continue
		}
		date := bsonText(row["GSTRS"])
		if best == "" || date >= bestDate {
			best, bestDate = order, date
		}
	}
	return best
}

func normalizeMachineOrder(value string) string {
	value = strings.TrimSpace(value)
	trimmed := strings.TrimLeft(value, "0")
	if trimmed == "" && value != "" {
		return "0"
	}
	return trimmed
}

func buildMachineConfigItems(orderID string, rows []bson.M) []machineConfigItem {
	materials := map[string]*machineMaterial{}
	orderNumber, _ := strconv.ParseInt(orderID, 10, 64)
	for _, row := range rows {
		group := bsonText(row["MATKL"])
		if machineConfigNonComponentGroups[group] {
			continue
		}
		dn := normalizeMachineOrder(bsonText(row["MATNR_BI"]))
		if dn == "" {
			dn = normalizeMachineOrder(bsonText(row["MATNR"]))
		}
		if dn == "" {
			continue
		}
		item, ok := materials[dn]
		if !ok {
			item = &machineMaterial{
				dn:        dn,
				name:      bsonText(row["MAKTX"]),
				group:     group,
				groupName: bsonText(row["WGBEZ"]),
				location:  bsonText(row["LGORT"]),
				psmng:     bsonNumber(row["PSMNG"]),
			}
			materials[dn] = item
		}
		item.total += signedMachineQuantity(row)
		if item.psmng == 0 {
			item.psmng = bsonNumber(row["PSMNG"])
		}
	}

	items := make([]machineConfigItem, 0, len(materials))
	for _, material := range materials {
		if material.total <= 0 {
			continue
		}
		relationID := "local-" + material.dn
		id := "local-" + orderID + "-" + material.dn
		item := machineConfigItem{
			NeedNum:       nil,
			Num:           machineUnitQuantity(material.total, material.psmng),
			TotalNum:      material.total,
			Name:          material.name,
			MaterialGroup: material.group,
			DN:            material.dn,
			RelationID:    relationID,
			ID:            id,
			Type:          machineComponentTypes[material.group],
			Params:        machineConfigParams(id, relationID, orderNumber, material),
		}
		items = append(items, item)
	}
	sortMachineConfigItems(items)
	return items
}

func machineConfigParams(id, relationID string, orderID int64, material *machineMaterial) []machineConfigParam {
	params := make([]machineConfigParam, 0, 2)
	add := func(suffix, name, value string) {
		if value == "" {
			return
		}
		params = append(params, machineConfigParam{
			ID:               id + suffix,
			OrderID:          orderID,
			RelationID:       relationID,
			MaterialSN:       material.dn,
			ComparionValue:   value,
			Name:             name,
			PartsDescription: material.groupName,
			MaterialGroup:    material.group,
		})
	}
	if machineModelGroups[material.group] {
		add("-model", "型号", machineModelName(material.name))
	}
	add("-location", "库位", material.location)
	return params
}

func machineUnitQuantity(total, psmng float64) float64 {
	if psmng <= 0 {
		return 0
	}
	value := total / psmng
	return math.Round(value*10000) / 10000
}

func signedMachineQuantity(row bson.M) float64 {
	if value, ok := parseBsonNumber(row["MENGE"]); ok {
		return value
	}
	value, _ := parseBsonNumber(row["MENGE_A"])
	if bsonText(row["BWART"]) == "262" {
		return -value
	}
	return value
}

var (
	machineSpecToken  = regexp.MustCompile(`(?i)^\d+(\.\d+)?(CM|MM|MHZ|GHZ|GB|TB|G|T|M|W)$|^\d{1,2}(C|R|H|U)$|^\d+U$|^[BT]\d+$`)
	machineModelToken = regexp.MustCompile(`[A-Za-z0-9][A-Za-z0-9._+/\-]*`)
	machineTokenNoise = map[string]bool{
		"A": true, "B": true, "C": true, "T": true, "X": true,
		"DDR4": true, "DDR5": true, "CRPS": true, "CPU": true, "GPU": true,
		"SSD": true, "HDD": true, "PCIE": true, "OCP": true, "GEN4": true, "GEN5": true,
		"E5X4": true, "E4X4": true, "M2": true, "NVME": true, "USB": true,
	}
)

// machineModelName picks the most model-like ASCII token from a description.
// It is deliberately conservative: short or spec-like tokens are ignored.
func machineModelName(description string) string {
	best := ""
	for _, token := range machineModelToken.FindAllString(description, -1) {
		upper := strings.ToUpper(token)
		if len(token) < 4 || machineTokenNoise[upper] || machineSpecToken.MatchString(token) || !hasDigit(upper) || !hasLetter(upper) {
			continue
		}
		if len(token) > len(best) {
			best = token
		}
	}
	return best
}

func hasDigit(value string) bool {
	for _, r := range value {
		if r >= '0' && r <= '9' {
			return true
		}
	}
	return false
}

func hasLetter(value string) bool {
	for _, r := range value {
		if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') {
			return true
		}
	}
	return false
}

func sortMachineConfigItems(items []machineConfigItem) {
	sort.Slice(items, func(i, j int) bool {
		if items[i].Type != items[j].Type {
			return items[i].Type < items[j].Type
		}
		if items[i].Name != items[j].Name {
			return items[i].Name < items[j].Name
		}
		return items[i].DN < items[j].DN
	})
}

func bsonText(value any) string {
	switch typed := value.(type) {
	case string:
		return strings.TrimSpace(typed)
	case nil:
		return ""
	case []byte:
		return strings.TrimSpace(string(typed))
	default:
		return strings.TrimSpace(fmt.Sprint(typed))
	}
}

func bsonNumber(value any) float64 {
	number, _ := parseBsonNumber(value)
	return number
}

func parseBsonNumber(value any) (float64, bool) {
	switch typed := value.(type) {
	case float64:
		return typed, true
	case float32:
		return float64(typed), true
	case int:
		return float64(typed), true
	case int32:
		return float64(typed), true
	case int64:
		return float64(typed), true
	case json.Number:
		parsed, err := typed.Float64()
		return parsed, err == nil
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(typed), 64)
		return parsed, err == nil
	case nil:
		return 0, false
	default:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(fmt.Sprint(typed)), 64)
		return parsed, err == nil
	}
}
