package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"reflect"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const (
	mgetStatement = "pglc-go-mget"
	anyStatement  = "pglc-go-any"
)

type inputConfig struct {
	Clients int    `json:"clients"`
	Batch   int    `json:"batch"`
	Seconds int    `json:"seconds"`
	Mode    string `json:"mode"`
	MgetSQL string `json:"mget_sql"`
	AnySQL  string `json:"any_sql"`
	Port    int    `json:"port"`
}

type readyMessage struct {
	Ready         bool                `json:"ready"`
	ResultFormats map[string][]string `json:"result_formats"`
	Runtime       string              `json:"runtime"`
	GOMAXPROCS    int                 `json:"gomaxprocs"`
}

type latencyResult struct {
	Samples int     `json:"samples"`
	P50MS   float64 `json:"p50_ms"`
	P95MS   float64 `json:"p95_ms"`
	P99MS   float64 `json:"p99_ms"`
}

type timedResult struct {
	Requests  int           `json:"requests"`
	Seconds   float64       `json:"seconds"`
	RequestsS float64       `json:"requests_s"`
	Latency   latencyResult `json:"latency"`
	ClientCPU float64       `json:"client_cpu_cores"`
	Threads   int           `json:"threads"`
}

type resultMessage struct {
	Result timedResult `json:"result"`
}

type workerResult struct {
	latencies []float64
}

func validateConfig(cfg inputConfig) error {
	if cfg.Clients < 1 || cfg.Clients > 256 {
		return fmt.Errorf("clients must be between 1 and 256")
	}
	if cfg.Batch != 1 && cfg.Batch != 16 && cfg.Batch != 64 {
		return fmt.Errorf("batch must be one of 1, 16, or 64")
	}
	if cfg.Seconds < 1 || cfg.Seconds > 120 {
		return fmt.Errorf("seconds must be between 1 and 120")
	}
	if cfg.Mode != "mget" && cfg.Mode != "postgres-any" {
		return fmt.Errorf("mode must be mget or postgres-any")
	}
	if cfg.Port < 1 || cfg.Port > 65535 {
		return fmt.Errorf("port must be between 1 and 65535")
	}
	for name, query := range map[string]string{"mget_sql": cfg.MgetSQL, "any_sql": cfg.AnySQL} {
		if strings.TrimSpace(query) == "" {
			return fmt.Errorf("%s must be non-empty", name)
		}
		if strings.IndexByte(query, 0) >= 0 {
			return fmt.Errorf("%s contains NUL", name)
		}
		if !strings.Contains(query, "$1") {
			return fmt.Errorf("%s must contain $1", name)
		}
	}
	return nil
}

func connectionConfig(port int) (*pgx.ConnConfig, error) {
	connString := fmt.Sprintf(
		"host=127.0.0.1 port=%d dbname=pglc_demo user=demo password=demo-only sslmode=disable",
		port,
	)
	cfg, err := pgx.ParseConfig(connString)
	if err != nil {
		return nil, err
	}
	if cfg.RuntimeParams == nil {
		cfg.RuntimeParams = make(map[string]string)
	}
	cfg.RuntimeParams["statement_timeout"] = "10000"
	cfg.RuntimeParams["application_name"] = "pglc-go-benchmark"
	cfg.ConnectTimeout = 5 * time.Second
	return cfg, nil
}

func connectAll(ctx context.Context, cfg inputConfig) ([]*pgx.Conn, error) {
	connections := make([]*pgx.Conn, 0, cfg.Clients)
	for i := 0; i < cfg.Clients; i++ {
		connCfg, err := connectionConfig(cfg.Port)
		if err != nil {
			closeConnections(connections)
			return nil, err
		}
		conn, err := pgx.ConnectConfig(ctx, connCfg)
		if err != nil {
			closeConnections(connections)
			return nil, err
		}
		connections = append(connections, conn)
	}
	return connections, nil
}

func closeConnections(connections []*pgx.Conn) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for _, conn := range connections {
		_ = conn.Close(ctx)
	}
}

func prepareAll(ctx context.Context, connections []*pgx.Conn, cfg inputConfig) error {
	for _, conn := range connections {
		if _, err := conn.Prepare(ctx, mgetStatement, cfg.MgetSQL); err != nil {
			return fmt.Errorf("prepare mget: %w", err)
		}
		if _, err := conn.Prepare(ctx, anyStatement, cfg.AnySQL); err != nil {
			return fmt.Errorf("prepare postgres-any: %w", err)
		}
	}
	return nil
}

func formatNames(descriptions []pgconn.FieldDescription) []string {
	formats := make([]string, len(descriptions))
	for i, description := range descriptions {
		switch description.Format {
		case 0:
			formats[i] = "text"
		case 1:
			formats[i] = "binary"
		default:
			formats[i] = "format-" + strconv.FormatUint(uint64(description.Format), 10)
		}
	}
	return formats
}

func decodeJSONRows(raw []*string) ([]map[string]any, error) {
	rows := make([]map[string]any, len(raw))
	for i, value := range raw {
		if value == nil {
			continue
		}
		var row map[string]any
		if err := json.Unmarshal([]byte(*value), &row); err != nil {
			return nil, fmt.Errorf("decode row %d: %w", i, err)
		}
		rows[i] = row
	}
	return rows, nil
}

func queryMget(ctx context.Context, conn *pgx.Conn, keys []*int64) ([]map[string]any, []string, error) {
	rows, err := conn.Query(ctx, mgetStatement, keys)
	if err != nil {
		return nil, nil, err
	}
	formats := formatNames(rows.FieldDescriptions())
	defer rows.Close()

	if !rows.Next() {
		if err := rows.Err(); err != nil {
			return nil, formats, err
		}
		return nil, formats, fmt.Errorf("mget returned no row")
	}
	var raw []*string
	if err := rows.Scan(&raw); err != nil {
		return nil, formats, err
	}
	if rows.Next() {
		return nil, formats, fmt.Errorf("mget returned multiple rows")
	}
	if err := rows.Err(); err != nil {
		return nil, formats, err
	}
	decoded, err := decodeJSONRows(raw)
	return decoded, formats, err
}

func queryAny(ctx context.Context, conn *pgx.Conn, keys []*int64) ([]map[string]any, []string, error) {
	rows, err := conn.Query(ctx, anyStatement, keys)
	if err != nil {
		return nil, nil, err
	}
	formats := formatNames(rows.FieldDescriptions())
	defer rows.Close()

	byKey := make(map[string]string, len(keys))
	for rows.Next() {
		var key string
		var value string
		if err := rows.Scan(&key, &value); err != nil {
			return nil, formats, err
		}
		byKey[key] = value
	}
	if err := rows.Err(); err != nil {
		return nil, formats, err
	}

	decoded := make([]map[string]any, len(keys))
	for i, key := range keys {
		if key == nil {
			continue
		}
		value, ok := byKey[strconv.FormatInt(*key, 10)]
		if !ok {
			continue
		}
		var row map[string]any
		if err := json.Unmarshal([]byte(value), &row); err != nil {
			return nil, formats, fmt.Errorf("decode row for key %s: %w", value, err)
		}
		decoded[i] = row
	}
	return decoded, formats, nil
}

func fixedKeys(batch int) []*int64 {
	keys := make([]*int64, batch)
	for i := range keys {
		key := int64(i + 1)
		keys[i] = &key
	}
	return keys
}

func edgeKeys() []*int64 {
	values := []int64{42, 7, 42, 999999}
	return []*int64{&values[0], &values[1], &values[2], nil, &values[3]}
}

func verifyParity(ctx context.Context, conn *pgx.Conn) (map[string][]string, error) {
	formats := make(map[string][]string, 2)
	mgetRows, mgetFormats, err := queryMget(ctx, conn, edgeKeys())
	if err != nil {
		return nil, fmt.Errorf("mget edge query: %w", err)
	}
	anyRows, anyFormats, err := queryAny(ctx, conn, edgeKeys())
	if err != nil {
		return nil, fmt.Errorf("postgres-any edge query: %w", err)
	}
	if !reflect.DeepEqual(mgetRows, anyRows) {
		return nil, fmt.Errorf("edge result mismatch")
	}
	empty := []*int64{}
	mgetRows, _, err = queryMget(ctx, conn, empty)
	if err != nil {
		return nil, fmt.Errorf("mget empty query: %w", err)
	}
	anyRows, _, err = queryAny(ctx, conn, empty)
	if err != nil {
		return nil, fmt.Errorf("postgres-any empty query: %w", err)
	}
	if !reflect.DeepEqual(mgetRows, anyRows) {
		return nil, fmt.Errorf("empty result mismatch")
	}
	formats["mget"] = mgetFormats
	formats["postgres-any"] = anyFormats
	return formats, nil
}

func warm(ctx context.Context, connections []*pgx.Conn, cfg inputConfig, keys []*int64) error {
	for _, conn := range connections {
		var err error
		if cfg.Mode == "mget" {
			_, _, err = queryMget(ctx, conn, keys)
		} else {
			_, _, err = queryAny(ctx, conn, keys)
		}
		if err != nil {
			return fmt.Errorf("warm %s: %w", cfg.Mode, err)
		}
	}
	return nil
}

func timedRequest(ctx context.Context, conn *pgx.Conn, cfg inputConfig, keys []*int64) error {
	if cfg.Mode == "mget" {
		_, _, err := queryMget(ctx, conn, keys)
		return err
	}
	_, _, err := queryAny(ctx, conn, keys)
	return err
}

func timevalSeconds(value syscall.Timeval) float64 {
	return float64(value.Sec) + float64(value.Usec)/1e6
}

func cpuSeconds(usage syscall.Rusage) float64 {
	return timevalSeconds(usage.Utime) + timevalSeconds(usage.Stime)
}

func percentile(values []float64, p float64) float64 {
	index := int(math.Ceil(float64(len(values))*p)) - 1
	if index < 0 {
		index = 0
	}
	if index >= len(values) {
		index = len(values) - 1
	}
	return values[index]
}

func latency(values []float64) (latencyResult, error) {
	if len(values) == 0 {
		return latencyResult{}, fmt.Errorf("no requests completed")
	}
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	return latencyResult{
		Samples: len(sorted),
		P50MS:   percentile(sorted, 0.50),
		P95MS:   percentile(sorted, 0.95),
		P99MS:   percentile(sorted, 0.99),
	}, nil
}

func runTimed(cfg inputConfig, connections []*pgx.Conn, keys []*int64) (timedResult, error) {
	var cpuStart syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &cpuStart); err != nil {
		return timedResult{}, fmt.Errorf("get starting CPU usage: %w", err)
	}
	started := time.Now()
	deadline := started.Add(time.Duration(cfg.Seconds) * time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	results := make(chan workerResult, len(connections))
	var waitGroup sync.WaitGroup
	var errorMu sync.Mutex
	var firstError error
	fail := func(err error) {
		errorMu.Lock()
		defer errorMu.Unlock()
		if firstError == nil {
			firstError = err
			cancel()
		}
	}
	for _, conn := range connections {
		waitGroup.Add(1)
		go func(conn *pgx.Conn) {
			defer waitGroup.Done()
			local := workerResult{latencies: make([]float64, 0, 128)}
			for time.Now().Before(deadline) {
				requestStarted := time.Now()
				err := timedRequest(ctx, conn, cfg, keys)
				if err != nil {
					errorMu.Lock()
					hasFailure := firstError != nil
					errorMu.Unlock()
					if !hasFailure {
						fail(err)
					}
					return
				}
				local.latencies = append(local.latencies, float64(time.Since(requestStarted))/float64(time.Millisecond))
			}
			results <- local
		}(conn)
	}
	waitGroup.Wait()
	finished := time.Now()
	var cpuEnd syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &cpuEnd); err != nil {
		return timedResult{}, fmt.Errorf("get ending CPU usage: %w", err)
	}
	close(results)

	errorMu.Lock()
	err := firstError
	errorMu.Unlock()
	if err != nil {
		return timedResult{}, fmt.Errorf("timed query: %w", err)
	}

	allLatencies := make([]float64, 0)
	for result := range results {
		allLatencies = append(allLatencies, result.latencies...)
	}
	latencies, err := latency(allLatencies)
	if err != nil {
		return timedResult{}, err
	}
	seconds := finished.Sub(started).Seconds()
	if seconds <= 0 {
		return timedResult{}, fmt.Errorf("invalid elapsed time")
	}
	return timedResult{
		Requests:  latencies.Samples,
		Seconds:   seconds,
		RequestsS: float64(latencies.Samples) / seconds,
		Latency:   latencies,
		ClientCPU: (cpuSeconds(cpuEnd) - cpuSeconds(cpuStart)) / seconds,
		Threads:   runtime.GOMAXPROCS(0),
	}, nil
}

func readInput(reader *bufio.Reader) (inputConfig, error) {
	line, err := reader.ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return inputConfig{}, err
	}
	if strings.TrimSpace(line) == "" {
		return inputConfig{}, fmt.Errorf("missing config JSON line")
	}
	var cfg inputConfig
	if err := json.Unmarshal([]byte(line), &cfg); err != nil {
		return inputConfig{}, fmt.Errorf("parse config JSON: %w", err)
	}
	if err := validateConfig(cfg); err != nil {
		return inputConfig{}, err
	}
	return cfg, nil
}

func writeJSON(writer *bufio.Writer, value any) error {
	encoder := json.NewEncoder(writer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return err
	}
	return writer.Flush()
}

func run(reader *bufio.Reader, writer *bufio.Writer) error {
	cfg, err := readInput(reader)
	if err != nil {
		return err
	}
	setupCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	connections, err := connectAll(setupCtx, cfg)
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer closeConnections(connections)
	if err := prepareAll(setupCtx, connections, cfg); err != nil {
		return err
	}
	formats, err := verifyParity(setupCtx, connections[0])
	if err != nil {
		return err
	}
	if err := warm(setupCtx, connections, cfg, fixedKeys(cfg.Batch)); err != nil {
		return err
	}
	if err := writeJSON(writer, readyMessage{
		Ready:         true,
		ResultFormats: formats,
		Runtime:       runtime.Version(),
		GOMAXPROCS:    runtime.GOMAXPROCS(0),
	}); err != nil {
		return fmt.Errorf("write ready message: %w", err)
	}
	goLine, err := reader.ReadString('\n')
	if err != nil {
		return fmt.Errorf("read go command: %w", err)
	}
	if strings.TrimSpace(goLine) != "go" {
		return fmt.Errorf("expected go command")
	}
	result, err := runTimed(cfg, connections, fixedKeys(cfg.Batch))
	if err != nil {
		return err
	}
	if err := writeJSON(writer, resultMessage{Result: result}); err != nil {
		return fmt.Errorf("write result message: %w", err)
	}
	if _, err := reader.ReadString('\n'); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("read release command: %w", err)
	}
	return nil
}

func main() {
	reader := bufio.NewReader(os.Stdin)
	writer := bufio.NewWriter(os.Stdout)
	if err := run(reader, writer); err != nil {
		fmt.Fprintf(os.Stderr, "go-pgx benchmark: %v\n", err)
		os.Exit(1)
	}
}
