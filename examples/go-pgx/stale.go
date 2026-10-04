package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5"
	"io"
	"reflect"
	"runtime"
)

type staleResult struct {
	Target   string  `json:"target"`
	Checked  int     `json:"checked"`
	Cached   int     `json:"cached"`
	Stale    int     `json:"stale"`
	Examples []int64 `json:"examples"`
}

func runStaleCheck(reader *bufio.Reader, writer *bufio.Writer, cfg inputConfig, setupCtx context.Context) error {
	cache, err := openRESP(cfg)
	if err != nil {
		return fmt.Errorf("connect stale-check cache: %w", err)
	}
	defer cache.conn.Close()
	connCfg, err := connectionConfig(cfg.Port)
	if err != nil {
		return err
	}
	conn, err := pgx.ConnectConfig(setupCtx, connCfg)
	if err != nil {
		return fmt.Errorf("connect stale-check database: %w", err)
	}
	defer closeConnections([]*pgx.Conn{conn})
	table := "items"
	if cfg.Target == "valkey" {
		table = "direct_items"
	}
	statement := "SELECT id::text AS key, row_to_json(i)::text AS row FROM public." + table + " AS i WHERE id = ANY($1::bigint[])"
	if _, err := conn.Prepare(setupCtx, "stale-check", statement); err != nil {
		return fmt.Errorf("prepare stale-check query: %w", err)
	}
	if err := waitGo(reader, writer, readyMessage{
		Ready:         true,
		ResultFormats: map[string][]string{"stale-check": {"checked", "cached", "stale", "examples"}},
		Runtime:       runtime.Version(),
		GOMAXPROCS:    runtime.GOMAXPROCS(0),
	}); err != nil {
		return err
	}
	result := staleResult{Target: cfg.Target, Examples: make([]int64, 0, 5)}
	for start := 1; start <= cfg.KeySpace; start += 64 {
		end := start + 64
		if end > cfg.KeySpace+1 {
			end = cfg.KeySpace + 1
		}
		keys := make([]*int64, end-start)
		for i := range keys {
			key := int64(start + i)
			keys[i] = &key
		}
		var cachedRows []map[string]any
		if cfg.Target == "valkey" {
			cachedRows, err = cache.queryValkey(setupCtx, keys)
		} else {
			cachedRows, err = cache.query(setupCtx, keys)
		}
		if err != nil {
			return fmt.Errorf("read cache batch at id %d: %w", start, err)
		}
		databaseRows, _, err := queryNamed(setupCtx, conn, "stale-check", keys)
		if err != nil {
			return fmt.Errorf("read database batch at id %d: %w", start, err)
		}
		result.Checked += len(keys)
		for i, cachedRow := range cachedRows {
			if cachedRow == nil {
				continue
			}
			result.Cached++
			if reflect.DeepEqual(cachedRow, databaseRows[i]) {
				continue
			}
			result.Stale++
			if len(result.Examples) < 5 {
				result.Examples = append(result.Examples, int64(start+i))
			}
		}
	}
	if err := writeJSON(writer, resultMessage{Result: result}); err != nil {
		return fmt.Errorf("write stale-check result: %w", err)
	}
	if _, err := reader.ReadString('\n'); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("read release command: %w", err)
	}
	return nil
}
