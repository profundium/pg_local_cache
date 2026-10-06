package main

import (
	"context"
	"testing"
	"time"
)

func TestKeySourceBounds(t *testing.T) {
	for _, distribution := range []string{"uniform", "zipf"} {
		t.Run(distribution, func(t *testing.T) {
			source := keySource(inputConfig{Batch: 1, KeySpace: 1000, KeyDist: distribution}, 7)
			for i := 0; i < 10_000; i++ {
				key := *source()[0]
				if key < 1 || key > 1000 {
					t.Fatalf("%s key out of bounds: %d", distribution, key)
				}
			}
		})
	}
}

func TestPacingScheduleAbsoluteOffsets(t *testing.T) {
	start := time.Unix(100, 0)
	schedule := newPacingSchedule(start, 100_000, 64)
	for index, offset := range []time.Duration{0, 640 * time.Microsecond, 1280 * time.Microsecond, 1920 * time.Microsecond} {
		if got := schedule.at(int64(index)); !got.Equal(start.Add(offset)) {
			t.Fatalf("schedule.at(%d) = %s, want %s", index, got, start.Add(offset))
		}
	}
	if err := schedule.wait(context.Background(), 0); err != nil {
		t.Fatalf("first scheduled operation should be immediate: %v", err)
	}
	closedLoop := newPacingSchedule(start, 0, 64)
	if closedLoop.rate != 0 || !closedLoop.at(50).Equal(start) {
		t.Fatal("zero rate should disable pacing")
	}
	fractional := newPacingSchedule(start, 30_000, 64)
	if got, want := fractional.at(3), start.Add(6400*time.Microsecond); !got.Equal(want) {
		t.Fatalf("fractional schedule.at(3) = %s, want %s", got, want)
	}
}

func TestValidateConfigAllowsTwoHourRun(t *testing.T) {
	cfg := inputConfig{
		Clients: 1, Batch: 1, Seconds: 7200, Mode: "postgres-any",
		AnySQL: "SELECT $1", Port: 5432, KeyDist: "uniform",
	}
	if err := validateConfig(cfg); err != nil {
		t.Fatalf("7200-second run rejected: %v", err)
	}
	cfg.Seconds++
	if err := validateConfig(cfg); err == nil {
		t.Fatal("7201-second run accepted")
	}
}

func TestLatencyHistogramUsesBoundedBuckets(t *testing.T) {
	var histogram latencyHistogram
	for i := 1; i <= 100_000; i++ {
		histogram.observe(time.Duration(i%1000+1) * time.Millisecond)
	}
	if histogram.samples != 100_000 || len(histogram.buckets) != latencyHistogramBuckets {
		t.Fatalf("histogram has %d samples and %d buckets", histogram.samples, len(histogram.buckets))
	}
	if got := histogram.percentile(.50); got < 490 || got > 510 {
		t.Fatalf("p50 = %.3f ms, want about 500 ms", got)
	}
	if got := histogram.percentile(.99); got < 980 || got > 1010 {
		t.Fatalf("p99 = %.3f ms, want about 990 ms", got)
	}
	if got := float64(histogram.max) / float64(time.Millisecond); got != 1000 {
		t.Fatalf("max = %.3f ms, want 1000 ms", got)
	}
}
