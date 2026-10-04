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

const anySQL = `SELECT id::text AS key, row_to_json(i)::text AS row FROM public.items AS i WHERE id = ANY($1::bigint[])`

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
	rows, err := conn.Query(ctx, anySQL, keys)
	if err != nil {
		return fmt.Errorf("query postgres-any: %w", err)
	}
	defer rows.Close()
	byKey := make(map[string]json.RawMessage)
	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			return fmt.Errorf("scan postgres-any row: %w", err)
		}
		if !json.Valid([]byte(value)) {
			return fmt.Errorf("decode row %s: invalid JSON", key)
		}
		byKey[key] = json.RawMessage(value)
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("read postgres-any rows: %w", err)
	}
	ordered := make([]json.RawMessage, len(keys))
	for i, key := range keys {
		if key != nil {
			ordered[i] = byKey[strconv.FormatInt(*key, 10)]
		}
	}
	encoded, err := json.MarshalIndent(ordered, "", "  ")
	if err != nil {
		return fmt.Errorf("encode result: %w", err)
	}
	fmt.Printf("keys: [42, 7, 42, null, 999999]\nrows:\n%s\n", encoded)
	return nil
}
