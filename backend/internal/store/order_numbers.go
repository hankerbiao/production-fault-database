package store

import (
	"fmt"
	"strings"

	"go.mongodb.org/mongo-driver/bson"
)

const (
	salesOrderWidth      = 10
	productionOrderWidth = 12
)

// normalizeOrderNumber removes padding only from numeric SAP order numbers.
// Mixed or non-numeric identifiers are business values and must be preserved.
func normalizeOrderNumber(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || !isNumericOrder(value) {
		return value
	}
	trimmed := strings.TrimLeft(value, "0")
	if trimmed == "" {
		return "0"
	}
	return trimmed
}

func isNumericOrder(value string) bool {
	if value == "" {
		return false
	}
	for _, char := range value {
		if char < '0' || char > '9' {
			return false
		}
	}
	return true
}

func orderCandidates(value string, width int) []string {
	value = strings.TrimSpace(value)
	if value == "" {
		return nil
	}

	values := []string{value}
	normalized := normalizeOrderNumber(value)
	if normalized != value {
		values = append(values, normalized)
	}
	if isNumericOrder(value) {
		padded := strings.Repeat("0", max(0, width-len(normalized))) + normalized
		if padded != value && padded != normalized {
			values = append(values, padded)
		}
	}
	return values
}

func normalizedOrderCandidates(items []any) []string {
	values := make([]string, 0, len(items)*3)
	seen := make(map[string]struct{}, len(items)*2)
	for _, item := range items {
		value := strings.TrimSpace(fmt.Sprint(item))
		if value == "" || value == "<nil>" {
			continue
		}
		for _, candidate := range orderCandidates(value, productionOrderWidth) {
			if _, ok := seen[candidate]; ok {
				continue
			}
			seen[candidate] = struct{}{}
			values = append(values, candidate)
		}
	}
	return values
}

// normalizedOrderExpression produces one comparable order value inside a
// Mongo aggregation. Numeric strings are converted through int64 so leading
// zeroes collapse, while non-numeric values remain unchanged.
func normalizedOrderExpression(field string) bson.M {
	trimmed := bson.M{"$trim": bson.M{"input": bson.M{"$convert": bson.M{
		"input": fieldExpression(field), "to": "string", "onError": "", "onNull": "",
	}}}}
	numeric := bson.M{"$convert": bson.M{"input": trimmed, "to": "long", "onError": nil, "onNull": nil}}
	return bson.M{"$let": bson.M{
		"vars": bson.M{"value": trimmed, "number": numeric},
		"in": bson.M{"$cond": bson.A{
			bson.M{"$regexMatch": bson.M{"input": "$$value", "regex": "^[0-9]+$"}},
			bson.M{"$cond": bson.A{
				bson.M{"$ne": bson.A{"$$number", nil}},
				bson.M{"$toString": "$$number"},
				"$$value",
			}},
			"$$value",
		}},
	}}
}

func fieldExpression(field string) string {
	if strings.HasPrefix(field, "$") {
		return field
	}
	return "$" + field
}
