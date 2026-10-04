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
