package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
)

type writeLatency struct {
	Samples int64   `json:"samples"`
	P50MS   float64 `json:"p50_ms"`
	P99MS   float64 `json:"p99_ms"`
	MaxMS   float64 `json:"max_ms"`
}

type writeResult struct {
	Transactions int64        `json:"transactions"`
	Seconds      float64      `json:"seconds"`
	TxS          float64      `json:"tx_s"`
	Latency      writeLatency `json:"latency"`
	Errors       int64        `json:"errors"`
	Error        string       `json:"error,omitempty"`
	ClientCPU    float64      `json:"client_cpu_cores"`
	Threads      int          `json:"threads"`
}

type writeWorkerResult struct {
	histogram latencyHistogram
	count     int64
	errors    int64
	err       error
}

type pacingSchedule struct {
	start   time.Time
	rate    int64
	clients int64
}

func newPacingSchedule(start time.Time, rate, clients int) pacingSchedule {
	if rate <= 0 {
		return pacingSchedule{start: start}
	}
	return pacingSchedule{start: start, rate: int64(rate), clients: int64(clients)}
}

func (p pacingSchedule) at(index int64) time.Time {
	if p.rate == 0 {
		return p.start
	}
	offset := index * int64(time.Second) * p.clients / p.rate
	return p.start.Add(time.Duration(offset))
}

func (p pacingSchedule) wait(ctx context.Context, index int64) error {
	delay := time.Until(p.at(index))
	if delay <= 0 {
		return ctx.Err()
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func waitGo(reader *bufio.Reader, writer *bufio.Writer, ready readyMessage) error {
	if err := writeJSON(writer, ready); err != nil {
		return fmt.Errorf("write ready message: %w", err)
	}
	line, err := reader.ReadString('\n')
	if err != nil {
		return fmt.Errorf("read go command: %w", err)
	}
	if strings.TrimSpace(line) != "go" {
		return fmt.Errorf("expected go command")
	}
	return nil
}

func runWrite(reader *bufio.Reader, writer *bufio.Writer, cfg inputConfig, setupCtx context.Context) error {
	connections, err := connectAll(setupCtx, cfg)
	if err != nil {
		return fmt.Errorf("connect write clients: %w", err)
	}
	defer closeConnections(connections)
	for _, conn := range connections {
		if _, err := conn.Exec(setupCtx, "SET synchronous_commit = "+cfg.SyncCommit); err != nil {
			return fmt.Errorf("set synchronous_commit: %w", err)
		}
	}
	var valkey []*respClient
	if cfg.ValkeyDel {
		valkey = make([]*respClient, 0, cfg.Clients)
		for range cfg.Clients {
			client, err := openRESP(cfg)
			if err != nil {
				for _, opened := range valkey {
					opened.conn.Close()
				}
				return fmt.Errorf("connect Valkey clients: %w", err)
			}
			valkey = append(valkey, client)
		}
		defer func() {
			for _, client := range valkey {
				_ = client.conn.Close()
			}
		}()
	}
	statement := "INSERT INTO public." + cfg.Table + "(id, value) VALUES ($1, 'x')"
	keysCfg := cfg
	keysCfg.Batch = 1
	if cfg.Op == "update" {
		statement = "UPDATE public." + cfg.Table + " SET revision = revision + 1 WHERE id = $1"
	} else if cfg.Op == "update_value" {
		statement = "UPDATE public." + cfg.Table + " SET revision = revision + 1, value = md5(random()::text) WHERE id = $1"
	}
	var insertID atomic.Int64
	insertID.Store(cfg.InsertStart - 1)
	if err := waitGo(reader, writer, readyMessage{
		Ready:         true,
		ResultFormats: map[string][]string{"write": {"transactions", "tx_s", "latency.p50_ms", "latency.p99_ms", "latency.max_ms", "errors"}},
		Runtime:       runtime.Version(),
		GOMAXPROCS:    runtime.GOMAXPROCS(0),
	}); err != nil {
		return err
	}
	var cpuStart syscall.Rusage
	_ = syscall.Getrusage(syscall.RUSAGE_SELF, &cpuStart)
	started := time.Now()
	deadline := started.Add(time.Duration(cfg.Seconds) * time.Second)
	ctx, cancel := context.WithDeadline(context.Background(), deadline.Add(15*time.Second))
	defer cancel()
	results := make(chan writeWorkerResult, len(connections))
	var workers sync.WaitGroup
	for i, conn := range connections {
		var nextKey func() []*int64
		if cfg.Op == "update" || cfg.Op == "update_value" {
			nextKey = keySource(keysCfg, uint64(i))
		}
		var cache *respClient
		if cfg.ValkeyDel {
			cache = valkey[i]
		}
		schedule := newPacingSchedule(started, cfg.Rate, cfg.Clients)
		workers.Add(1)
		go func(conn *pgx.Conn, nextKey func() []*int64, cache *respClient, schedule pacingSchedule) {
			defer workers.Done()
			local := writeWorkerResult{}
			for index := int64(0); time.Now().Before(deadline); index++ {
				if cfg.Rate > 0 {
					if err := schedule.wait(ctx, index); err != nil {
						break
					}
					if !time.Now().Before(deadline) {
						break
					}
				}
				var id int64
				if cfg.Op == "insert" {
					id = insertID.Add(1)
				} else {
					id = *nextKey()[0]
				}
				transactionStarted := time.Now()
				if _, err := conn.Exec(ctx, statement, id); err != nil {
					local.errors++
					local.err = fmt.Errorf("write %s id %d: %w", cfg.Op, id, err)
					cancel()
					break
				}
				local.count++
				if cache != nil {
					if err := cache.del("item:" + strconv.FormatInt(id, 10)); err != nil {
						local.histogram.observe(time.Since(transactionStarted))
						local.errors++
						local.err = fmt.Errorf("invalidate item:%d: %w", id, err)
						cancel()
						break
					}
				}
				local.histogram.observe(time.Since(transactionStarted))
			}
			results <- local
		}(conn, nextKey, cache, schedule)
	}
	workers.Wait()
	finished := time.Now()
	close(results)
	var allLatencies latencyHistogram
	result := writeResult{Threads: runtime.GOMAXPROCS(0)}
	for worker := range results {
		result.Transactions += worker.count
		result.Errors += worker.errors
		allLatencies.merge(&worker.histogram)
		if result.Error == "" && worker.err != nil {
			result.Error = worker.err.Error()
		}
	}
	result.Seconds = finished.Sub(started).Seconds()
	if result.Seconds > 0 {
		result.TxS = float64(result.Transactions) / result.Seconds
	}
	if allLatencies.samples > 0 {
		result.Latency = writeLatency{
			Samples: int64(allLatencies.samples),
			P50MS:   allLatencies.percentile(.50),
			P99MS:   allLatencies.percentile(.99),
			MaxMS:   float64(allLatencies.max) / float64(time.Millisecond),
		}
	}
	var cpuEnd syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &cpuEnd); err == nil && result.Seconds > 0 {
		result.ClientCPU = (cpuSeconds(cpuEnd) - cpuSeconds(cpuStart)) / result.Seconds
	}
	if err := writeJSON(writer, resultMessage{Result: result}); err != nil {
		return fmt.Errorf("write result: %w", err)
	}
	if _, err := reader.ReadString('\n'); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("read release command: %w", err)
	}
	return nil
}
