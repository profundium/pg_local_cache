package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"runtime"
	"strconv"
	"sync/atomic"

	"github.com/jackc/pgx/v5"
)

func readValkeyAside(ctx context.Context, conn *pgx.Conn, cache *respClient, id int64, hits, misses *atomic.Int64) error {
	key := "item:" + strconv.FormatInt(id, 10)
	value, found, err := cache.get(key)
	if err != nil {
		return err
	}
	if found {
		if !json.Valid(value) {
			return fmt.Errorf("invalid JSON in Valkey key %s", key)
		}
		hits.Add(1)
		return nil
	}
	misses.Add(1)
	var row string
	err = conn.QueryRow(ctx, "SELECT row_to_json(i)::text FROM public.direct_items AS i WHERE id = $1", id).Scan(&row)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if !json.Valid([]byte(row)) {
		return fmt.Errorf("database returned invalid JSON for id %d", id)
	}
	return cache.set(key, []byte(row))
}

func runValkeyAside(reader *bufio.Reader, writer *bufio.Writer, cfg inputConfig, setupCtx context.Context) error {
	connections, err := connectAll(setupCtx, cfg)
	if err != nil {
		return fmt.Errorf("connect Valkey-aside readers: %w", err)
	}
	defer closeConnections(connections)
	caches := make([]*respClient, 0, cfg.Clients)
	for range cfg.Clients {
		cache, err := openRESP(cfg)
		if err != nil {
			for _, opened := range caches {
				_ = opened.conn.Close()
			}
			return fmt.Errorf("connect Valkey: %w", err)
		}
		caches = append(caches, cache)
	}
	defer func() {
		for _, cache := range caches {
			_ = cache.conn.Close()
		}
	}()
	var sample string
	if err := connections[0].QueryRow(setupCtx, "SELECT row_to_json(i)::text FROM public.direct_items AS i WHERE id = 1").Scan(&sample); err != nil {
		return fmt.Errorf("verify direct_items: %w", err)
	}
	var hits, misses atomic.Int64
	if err := waitGo(reader, writer, readyMessage{
		Ready:         true,
		ResultFormats: map[string][]string{"valkey-aside": {"GET item:<id>", "direct_items row JSON on miss"}},
		Runtime:       runtime.Version(),
		GOMAXPROCS:    runtime.GOMAXPROCS(0),
	}); err != nil {
		return err
	}
	requests := make([]func(context.Context) error, cfg.Clients)
	for i, conn := range connections {
		cache := caches[i]
		nextKey := keySource(cfg, uint64(i))
		requests[i] = func(ctx context.Context) error {
			return readValkeyAside(ctx, conn, cache, *nextKey()[0], &hits, &misses)
		}
	}
	result, err := runTimed(cfg, requests)
	if err != nil {
		return err
	}
	resultWithCounts := map[string]any{
		"requests": result.Requests, "seconds": result.Seconds, "requests_s": result.RequestsS,
		"latency": result.Latency, "client_cpu_cores": result.ClientCPU, "threads": result.Threads,
		"cache_hits": hits.Load(), "cache_misses": misses.Load(),
	}
	if err := writeJSON(writer, resultMessage{Result: resultWithCounts}); err != nil {
		return fmt.Errorf("write result: %w", err)
	}
	if _, err := reader.ReadString('\n'); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("read release command: %w", err)
	}
	return nil
}
