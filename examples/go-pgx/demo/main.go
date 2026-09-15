package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
)

const mgetSQL = `SELECT local_cache.mget('public.items'::regclass, $1::bigint[])`

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "go-pgx demo: %v\n", err)
		os.Exit(1)
	}
}

func run() (err error) {
	port := 55432
	if raw := os.Getenv("PGLC_DEMO_PORT"); raw != "" {
		port, err = strconv.Atoi(raw)
		if err != nil || port < 1 || port > 65535 {
			return fmt.Errorf("PGLC_DEMO_PORT must be a TCP port")
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	dsn := fmt.Sprintf("postgres://demo:demo-only@127.0.0.1:%d/pglc_demo", port)
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return fmt.Errorf("connect to %s: %w", dsn, err)
	}
	defer func() {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer closeCancel()
		if closeErr := conn.Close(closeCtx); err == nil && closeErr != nil {
			err = fmt.Errorf("close connection: %w", closeErr)
		}
	}()

	k42, k7, missing := int64(42), int64(7), int64(999999)
	keys := []*int64{&k42, &k7, &k42, nil, &missing}
	var raw []*string
	if err := conn.QueryRow(ctx, mgetSQL, keys).Scan(&raw); err != nil {
		return fmt.Errorf("query mget: %w", err)
	}
	if len(raw) != len(keys) {
		return fmt.Errorf("mget returned %d rows, want %d", len(raw), len(keys))
	}
	rows := make([]json.RawMessage, len(raw))
	for i, value := range raw {
		if value == nil {
			continue
		}
		rows[i] = json.RawMessage(*value)
		if !json.Valid(rows[i]) {
			return fmt.Errorf("decode row %d: invalid JSON", i)
		}
	}
	encoded, err := json.MarshalIndent(rows, "", "  ")
	if err != nil {
		return fmt.Errorf("encode result: %w", err)
	}
	fmt.Printf("keys: [42, 7, 42, null, 999999]\nrows:\n%s\n", encoded)
	return nil
}
