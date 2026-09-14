package main

// Minimal RESP2 client for the disposable demo: AUTH and one MGET at a time.
import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"time"
)

type respClient struct {
	conn   net.Conn
	reader *bufio.Reader
	wire   []byte
}

func (c *respClient) send(args []string) error {
	if err := c.conn.SetDeadline(time.Now().Add(10 * time.Second)); err != nil {
		return err
	}
	c.wire = append(c.wire[:0], '*')
	c.wire = strconv.AppendInt(c.wire, int64(len(args)), 10)
	c.wire = append(c.wire, '\r', '\n')
	for _, arg := range args {
		c.wire = append(c.wire, '$')
		c.wire = strconv.AppendInt(c.wire, int64(len(arg)), 10)
		c.wire = append(c.wire, '\r', '\n')
		c.wire = append(c.wire, arg...)
		c.wire = append(c.wire, '\r', '\n')
	}
	for remaining := c.wire; len(remaining) > 0; {
		n, err := c.conn.Write(remaining)
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		remaining = remaining[n:]
	}
	return nil
}

func (c *respClient) line() (string, error) {
	bytes, err := c.reader.ReadSlice('\n')
	if err != nil {
		return "", err
	}
	line := string(bytes)
	if !strings.HasSuffix(line, "\r\n") || len(line) > 1024 {
		return "", fmt.Errorf("invalid RESP header")
	}
	line = strings.TrimSuffix(line, "\r\n")
	if strings.HasPrefix(line, "-") {
		return "", fmt.Errorf("RESP: %s", line[1:])
	}
	return line, nil
}

func openRESP(cfg inputConfig) (*respClient, error) {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(cfg.RespPort)), 5*time.Second)
	if err != nil {
		return nil, err
	}
	c := &respClient{conn: conn, reader: bufio.NewReaderSize(conn, 64<<10)}
	err = c.send([]string{"AUTH", cfg.RespToken})
	if err == nil {
		var response string
		response, err = c.line()
		if err == nil && response != "+OK" {
			err = fmt.Errorf("unexpected AUTH response")
		}
	}
	if err != nil {
		conn.Close()
		return nil, err
	}
	return c, nil
}

func (c *respClient) query(ctx context.Context, keys []*int64) ([]map[string]any, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	args := []string{"MGET"}
	for _, key := range keys {
		if key != nil {
			args = append(args, `CRUD:pglc_demo.public.items:{"id":`+strconv.FormatInt(*key, 10)+`}`)
		}
	}
	if len(args) == 1 {
		return make([]map[string]any, len(keys)), nil
	}
	if err := c.send(args); err != nil {
		return nil, err
	}
	header, err := c.line()
	if err != nil {
		return nil, err
	}
	if header != "*"+strconv.Itoa(len(args)-1) {
		return nil, fmt.Errorf("unexpected RESP array: %q", header)
	}
	raw := make([]*string, len(keys))
	for i, key := range keys {
		if key == nil {
			continue
		}
		header, err = c.line()
		if err != nil {
			return nil, err
		}
		if !strings.HasPrefix(header, "$") {
			return nil, fmt.Errorf("expected RESP bulk string")
		}
		length, err := strconv.Atoi(header[1:])
		if err != nil || length < -1 || length > 1<<20 {
			return nil, fmt.Errorf("invalid RESP bulk length")
		}
		if length == -1 {
			continue
		}
		value := make([]byte, length+2)
		if _, err := io.ReadFull(c.reader, value); err != nil {
			return nil, err
		}
		if string(value[length:]) != "\r\n" {
			return nil, fmt.Errorf("invalid RESP bulk terminator")
		}
		text := string(value[:length])
		raw[i] = &text
	}
	return decodeJSONRows(raw)
}

func runRESP(reader *bufio.Reader, writer *bufio.Writer, cfg inputConfig, ctx context.Context) error {
	clients := make([]*respClient, 0, cfg.Clients)
	defer func() {
		for _, c := range clients {
			c.conn.Close()
		}
	}()
	for i := 0; i < cfg.Clients; i++ {
		c, err := openRESP(cfg)
		if err != nil {
			return err
		}
		clients = append(clients, c)
	}
	// Verify decoded results against SQL, then disconnect SQL before timing.
	check := cfg
	check.Clients = 1
	conns, err := connectAll(ctx, check)
	if err != nil {
		return err
	}
	defer closeConnections(conns)
	if err := prepareAll(ctx, conns, cfg); err != nil {
		return err
	}
	for _, keys := range [][]*int64{edgeKeys(), {}} {
		expected, _, err := queryAny(ctx, conns[0], keys)
		if err != nil {
			return err
		}
		actual, err := clients[0].query(ctx, keys)
		if err != nil {
			return err
		}
		if !reflect.DeepEqual(actual, expected) {
			return fmt.Errorf("RESP/SQL result mismatch")
		}
	}
	closeConnections(conns)
	keys := fixedKeys(cfg.Batch)
	requests := make([]func(context.Context) error, len(clients))
	for i, c := range clients {
		if _, err := c.query(ctx, keys); err != nil {
			return err
		}
		requests[i] = func(ctx context.Context) error { _, err := c.query(ctx, keys); return err }
	}
	if err := writeJSON(writer, readyMessage{Ready: true, ResultFormats: map[string][]string{"resp-mget": {"RESP2 bulk JSON strings"}}, Runtime: runtime.Version(), GOMAXPROCS: runtime.GOMAXPROCS(0)}); err != nil {
		return err
	}
	line, err := reader.ReadString('\n')
	if err != nil {
		return err
	}
	if strings.TrimSpace(line) != "go" {
		return fmt.Errorf("expected go command")
	}
	result, err := runTimed(cfg, requests)
	if err != nil {
		return err
	}
	if err := writeJSON(writer, resultMessage{Result: result}); err != nil {
		return err
	}
	_, err = reader.ReadString('\n')
	return err
}
