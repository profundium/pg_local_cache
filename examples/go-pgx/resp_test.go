package main

import (
	"bufio"
	"context"
	"io"
	"net"
	"reflect"
	"strings"
	"testing"
)

func TestRESPDecodedRowsAndErrors(t *testing.T) {
	key := int64(7)
	for _, sample := range []struct {
		response  string
		wantError bool
	}{
		{"*3\r\n$15\r\n{\"v\":\"雪\\n\\\"\"}\r\n$-1\r\n$15\r\n{\"v\":\"雪\\n\\\"\"}\r\n", false},
		{"-ERR deadline exceeded\r\n", true},
		{"*2\r\n", true},
		{"*3\r\n$1048577\r\n", true},
		{"*3\r\n$2\r\n{}XX", true},
		{"*3\r\n$2\r\n{", true},
		{strings.Repeat("x", 5000), true},
	} {
		t.Run(sample.response[:2], func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			done := make(chan struct{})
			go func() {
				defer close(done)
				defer server.Close()
				r := bufio.NewReader(server)
				// Four request elements: MGET and three non-null keys.
				for i := 0; i < 9; i++ {
					if _, err := r.ReadString('\n'); err != nil {
						return
					}
				}
				for _, fragment := range []byte(sample.response) {
					if _, err := server.Write([]byte{fragment}); err != nil {
						return
					}
				}
			}()
			c := &respClient{conn: client, reader: bufio.NewReader(client)}
			rows, err := c.query(context.Background(), []*int64{&key, nil, &key, &key})
			if (err != nil) != sample.wantError {
				t.Fatalf("unexpected error: %v", err)
			}
			if !sample.wantError {
				want := []map[string]any{{"v": "雪\n\""}, nil, nil, {"v": "雪\n\""}}
				if !reflect.DeepEqual(rows, want) {
					t.Fatalf("rows: %#v", rows)
				}
			}
			client.Close()
			<-done
		})
	}
	// No request is sent for an empty/all-null application key list.
	c := &respClient{reader: bufio.NewReader(strings.NewReader(""))}
	rows, err := c.query(context.Background(), []*int64{nil})
	if err != nil || len(rows) != 1 || rows[0] != nil {
		t.Fatal(rows, err)
	}
	_, err = (&respClient{reader: bufio.NewReader(strings.NewReader("$"))}).line()
	if err != io.EOF {
		t.Fatal(err)
	}
}
