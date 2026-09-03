package store

import (
	"regexp"
	"strings"

	"go.mongodb.org/mongo-driver/bson"
)

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
	if f.DateFrom != "" || f.DateTo != "" {
		if repairTimeField(f) == "planned" {
			conditions = append(conditions, repairPlannedDateRangeFilter(f.DateFrom, f.DateTo))
		} else {
			bounds := bson.M{}
			if f.DateFrom != "" {
				bounds["$gte"] = strings.ReplaceAll(f.DateFrom, "-", "")
			}
			if f.DateTo != "" {
				bounds["$lte"] = strings.ReplaceAll(f.DateTo, "-", "")
			}
			conditions = append(conditions, bson.M{"ZDATE_WX": bounds})
		}
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

// repairTimeField defaults to repair time to preserve existing API callers.
// The board sends planned explicitly so repaired rows with GSTRS are listed first.
func repairTimeField(f Filters) string {
	if f.TimeField == "planned" {
		return "planned"
	}
	return "repair"
}

// repairPlannedDateRangeFilter accepts both the ISO dates written by the repair
// cleanup script and historical SAP values without separators.
func repairPlannedDateRangeFilter(dateFrom, dateTo string) bson.M {
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
		bson.M{"GSTRS": bounds(isoFrom, isoTo)},
		bson.M{"GSTRS": bounds(compactFrom, compactTo)},
	}}
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
