package main

import (
	"context"
	"io"

	"go.mongodb.org/mongo-driver/bson"
	"production-fault-gateway/internal/store"
)

// storeAPI is the HTTP-facing store contract. Keeping the complete contract
// here makes unsupported endpoints compile-time errors instead of runtime
// type assertions in individual handlers.
type storeAPI interface {
	Ping(context.Context) error
	DataStatus(context.Context) (store.DataStatus, error)
	List(context.Context, store.Filters, int, int) (store.ListResult, error)
	FaultSNs(context.Context, store.Filters) ([]store.FaultSN, error)
	FaultRowsBySNS(context.Context, []string, string, string, string) ([]bson.M, error)
	FaultDetail(context.Context, string) (store.FaultDetail, error)
	Stats(context.Context, store.Filters) (store.StatsResult, error)
	Orders(context.Context, store.OrderFilters, int, int) (store.OrderListResult, error)
	OrdersAll(context.Context, store.OrderFilters) (store.OrderListResult, error)
	OrderDetail(context.Context, string) (store.OrderDetail, error)
	OrderStats(context.Context, store.OrderFilters) (store.OrderStatsResult, error)
	OrderModels(context.Context, string) (store.OrderModelsResult, error)
	ViewList(context.Context, string, store.ViewFilters, int, int) (store.ViewListResult, error)
	ViewListAll(context.Context, string, store.ViewFilters) (store.ViewListResult, error)
	ViewBOMStream(context.Context, store.ViewFilters, io.Writer) error
	ViewStationStream(context.Context, store.ViewFilters, io.Writer) error
	ViewDetail(context.Context, string, string) (store.ViewDetailResult, error)
	ViewStats(context.Context, string, store.ViewFilters) (store.ViewStatsResult, error)
}
